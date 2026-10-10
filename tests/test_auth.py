"""Tests for user accounts and sessions."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.auth import (COOKIE_NAME, UserStore, hash_password,   # noqa: E402
                          verify_password)


@pytest.fixture
def users(tmp_path) -> UserStore:
    return UserStore(tmp_path / "auth")


class TestPasswordHashing:
    def test_round_trip(self):
        stored = hash_password("correct horse battery")
        assert verify_password("correct horse battery", stored)
        assert not verify_password("wrong", stored)

    def test_salted_so_identical_passwords_differ(self):
        assert hash_password("same") != hash_password("same")

    def test_never_stores_the_password(self):
        assert "correct horse" not in hash_password("correct horse")

    def test_rejects_garbage(self):
        assert not verify_password("x", "not-a-hash")
        assert not verify_password("x", "pbkdf2$abc$def$ghi")
        assert not verify_password("x", "")


class TestAccounts:
    def test_create_and_authenticate(self, users):
        users.create("operator", "hunter2hunter2", role="operator")
        assert users.authenticate("operator", "hunter2hunter2") is not None
        assert users.authenticate("operator", "wrong") is None
        assert users.authenticate("nobody", "hunter2hunter2") is None

    def test_username_is_case_insensitive(self, users):
        users.create("Abra", "hunter2hunter2")
        assert users.authenticate("abra", "hunter2hunter2") is not None

    def test_duplicate_rejected(self, users):
        users.create("abra", "hunter2hunter2")
        with pytest.raises(ValueError):
            users.create("ABRA", "different1")

    def test_short_password_rejected(self, users):
        with pytest.raises(ValueError):
            users.create("abra", "short")

    def test_disabled_user_cannot_sign_in(self, users):
        user = users.create("abra", "hunter2hunter2")
        user.enabled = False
        users._save()
        assert users.authenticate("abra", "hunter2hunter2") is None

    def test_bootstrap_only_once(self, users):
        first = users.ensure_bootstrap("admin", "hunter2hunter2")
        assert first is not None and first.role == "admin"
        assert users.ensure_bootstrap("other", "hunter2hunter2") is None
        assert users.count() == 1

    def test_persists_across_restart(self, tmp_path):
        a = UserStore(tmp_path)
        a.create("abra", "hunter2hunter2", role="operator")
        b = UserStore(tmp_path)
        assert b.count() == 1
        assert b.authenticate("abra", "hunter2hunter2") is not None

    def test_secret_key_stable_across_restart(self, tmp_path):
        a = UserStore(tmp_path)
        a.create("abra", "hunter2hunter2")
        token = a.issue(a.get("abra"))
        b = UserStore(tmp_path)
        assert b.read(token) is not None, "sessions must survive a restart"


class TestSessions:
    def test_issue_and_read(self, users):
        user = users.create("abra", "hunter2hunter2")
        token = users.issue(user)
        read = users.read(token)
        assert read is not None and read.username == "abra"

    def test_tampered_token_rejected(self, users):
        user = users.create("abra", "hunter2hunter2")
        token = users.issue(user)
        body, _, sig = token.rpartition(".")
        forged = f"{body}x.{sig}"
        assert users.read(forged) is None

    def test_token_from_another_store_rejected(self, tmp_path):
        a, b = UserStore(tmp_path / "a"), UserStore(tmp_path / "b")
        user = a.create("abra", "hunter2hunter2")
        assert b.read(a.issue(user)) is None, "signature must bind to the key"

    def test_expired_token_rejected(self, users):
        user = users.create("abra", "hunter2hunter2")
        assert users.read(users.issue(user, ttl=-1)) is None

    def test_disabled_user_session_dies(self, users):
        user = users.create("abra", "hunter2hunter2")
        token = users.issue(user)
        user.enabled = False
        users._save()
        assert users.read(token) is None

    def test_deleted_user_session_dies(self, users):
        user = users.create("abra", "hunter2hunter2")
        token = users.issue(user)
        users.delete("abra")
        assert users.read(token) is None

    def test_junk_rejected(self, users):
        for junk in ("", "abc", "a.b", "....", None):
            assert users.read(junk) is None


class TestRoles:
    @pytest.mark.parametrize("role,view,ptz,manage", [
        ("viewer",   True,  False, False),
        ("operator", True,  True,  False),
        ("admin",    True,  True,  True),
    ])
    def test_matrix(self, users, role, view, ptz, manage):
        user = users.create(f"u{role}", "hunter2hunter2", role=role)
        assert user.can("view") is view
        assert user.can("ptz") is ptz
        assert user.can("record") is ptz
        assert user.can("manage") is manage

    def test_disabled_user_can_do_nothing(self, users):
        user = users.create("operator", "hunter2hunter2", role="admin")
        user.enabled = False
        assert user.can("view") is False
        assert user.can("manage") is False

    def test_unknown_role_has_no_access(self, users):
        user = users.create("stranger", "hunter2hunter2", role="superuser")
        assert user.can("view") is False


class TestApiAuth:
    """End-to-end over HTTP against the real app."""

    @pytest.fixture
    def client(self, tmp_path, monkeypatch):
        import importlib
        monkeypatch.setenv("SENTINEL_DATA", str(tmp_path / "data"))
        monkeypatch.delenv("SENTINEL_TOKEN", raising=False)
        monkeypatch.delenv("SENTINEL_ADMIN_PASSWORD", raising=False)
        # DATA_DIR is bound at import time, so reload both modules or the
        # app keeps pointing at the real data/ directory.
        import backend.store
        import backend.app
        importlib.reload(backend.store)
        importlib.reload(backend.app)
        from fastapi.testclient import TestClient
        with TestClient(backend.app.app) as c:
            yield c

    def test_api_is_closed_before_any_login(self, client):
        assert client.get("/api/devices").status_code == 401
        assert client.get("/api/health").status_code == 401

    def test_healthz_stays_public(self, client):
        r = client.get("/healthz")
        assert r.status_code == 200 and r.json() == {"status": "ok"}

    def test_first_login_creates_admin(self, client):
        r = client.post("/api/auth/login",
                        json={"username": "owner", "password": "hunter2hunter2"})
        assert r.status_code == 200
        assert r.json()["user"]["role"] == "admin"
        assert COOKIE_NAME in r.cookies

    def test_session_survives_and_opens_the_api(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        assert client.get("/api/devices").status_code == 200

    def test_wrong_password_rejected(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        r = client.post("/api/auth/login",
                        json={"username": "owner", "password": "nope"})
        assert r.status_code == 401

    def test_logout_closes_the_api(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/devices").status_code == 401

    def test_session_endpoint_reports_state(self, client):
        assert client.get("/api/auth/session").json()["needsSetup"] is True
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        body = client.get("/api/auth/session").json()
        assert body["authenticated"] is True
        assert body["user"]["username"] == "owner"

    def test_short_bootstrap_password_rejected(self, client):
        r = client.post("/api/auth/login",
                        json={"username": "owner", "password": "short"})
        assert r.status_code == 400

    def test_viewer_cannot_add_devices(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        client.post("/api/auth/users",
                    json={"username": "watcher", "password": "hunter2hunter2",
                          "role": "viewer"})
        client.post("/api/auth/logout")
        client.post("/api/auth/login",
                    json={"username": "watcher", "password": "hunter2hunter2"})
        assert client.get("/api/devices").status_code == 200
        r = client.post("/api/devices",
                        json={"name": "nope", "host": "127.0.0.1"})
        assert r.status_code == 403
        assert client.post("/api/auth/users",
                           json={"username": "x", "password": "hunter2hunter2"}
                           ).status_code == 403

    def test_admin_cannot_delete_own_account(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        assert client.delete("/api/auth/users/owner").status_code == 400

    def test_admin_can_manage_users(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        client.post("/api/auth/users",
                    json={"username": "operator", "password": "hunter2hunter2",
                          "role": "operator"})
        names = [u["username"] for u in
                 client.get("/api/auth/users").json()["users"]]
        assert names == ["operator", "owner"]
        assert client.delete("/api/auth/users/operator").status_code == 200

    def test_import_requires_admin(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        client.post("/api/auth/users",
                    json={"username": "watcher", "password": "hunter2hunter2",
                          "role": "viewer"})
        client.post("/api/auth/logout")
        client.post("/api/auth/login",
                    json={"username": "watcher", "password": "hunter2hunter2"})
        assert client.post("/api/devices/import", json={"xml": "<x/>"}
                           ).status_code == 403

    def test_device_list_reports_capabilities(self, client):
        client.post("/api/auth/login",
                    json={"username": "owner", "password": "hunter2hunter2"})
        body = client.get("/api/devices").json()
        assert body["mayManage"] is True
        assert body["user"]["username"] == "owner"