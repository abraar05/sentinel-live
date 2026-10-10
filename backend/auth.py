"""User accounts and sessions.

The shared ``SENTINEL_TOKEN`` was a single shared secret: everyone with it had
everything, and it travelled in query strings where proxies log it. Real user
accounts with an HttpOnly session cookie fix both.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

PBKDF2_ROUNDS = 240_000
SESSION_TTL = 12 * 3600
COOKIE_NAME = "sentinel_session"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def hash_password(password: str, salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256, stored as ``pbkdf2$rounds$salt$hash``."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS
    )
    return f"pbkdf2${PBKDF2_ROUNDS}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds, salt_b64, digest_b64 = stored.split("$")
        if scheme != "pbkdf2":
            return False
        salt = _unb64(salt_b64)
        expected = _unb64(digest_b64)
        rounds = int(rounds)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
    return hmac.compare_digest(actual, expected)


ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
ACTION_RANK = {"view": 0, "ptz": 1, "record": 1, "manage": 2}


@dataclass
class User:
    id: str
    username: str
    password_hash: str
    role: str = "viewer"          # viewer | operator | admin
    enabled: bool = True
    created_at: float = 0.0

    def public(self) -> dict:
        return {"id": self.id, "username": self.username,
                "role": self.role, "enabled": self.enabled}

    def can(self, action: str) -> bool:
        # An unrecognised role must grant nothing. Defaulting it to viewer
        # would mean a typo in a role name quietly creates read access.
        rank = ROLE_RANK.get(self.role)
        if rank is None:
            return False
        return self.enabled and rank >= ACTION_RANK.get(action, 2)


class UserStore:
    def __init__(self, data_dir: str | Path):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "users.json"
        self.secret_path = self.dir / "session.key"
        self._lock = threading.RLock()
        self._users: dict[str, User] = {}
        self._secret = self._load_secret()
        self._load()

    # -------------------------------------------------------------- storage

    def _load_secret(self) -> bytes:
        if self.secret_path.exists():
            raw = self.secret_path.read_text().strip()
            if len(raw) == 64:
                return bytes.fromhex(raw)
        secret = secrets.token_bytes(32)
        self.secret_path.write_text(secret.hex())
        try:
            os.chmod(self.secret_path, 0o600)
        except OSError:
            pass
        return secret

    def _load(self) -> None:
        if not self.path.exists():
            self._users = {}
            return
        try:
            raw = json.loads(self.path.read_text())
        except (ValueError, OSError):
            self._users = {}
            return
        self._users = {}
        for entry in raw.get("users", []):
            try:
                self._users[entry["username"].lower()] = User(**entry)
            except TypeError:
                continue

    def _save(self) -> None:
        payload = {"version": 1,
                   "users": [vars(u) for u in self._users.values()]}
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        os.replace(tmp, self.path)

    # -------------------------------------------------------------- accounts

    def create(self, username: str, password: str, role: str = "viewer") -> User:
        username = (username or "").strip()
        if len(username) < 3:
            raise ValueError("username must be at least 3 characters")
        if len(password) < 8:
            raise ValueError("password must be at least 8 characters")
        with self._lock:
            key = username.lower()
            if key in self._users:
                raise ValueError("username already exists")
            user = User(id=secrets.token_hex(8), username=username,
                        password_hash=hash_password(password),
                        role=role, created_at=time.time())
            self._users[key] = user
            self._save()
            return user

    def authenticate(self, username: str, password: str) -> User | None:
        with self._lock:
            user = self._users.get((username or "").strip().lower())
        if user is None or not user.enabled:
            # Hash anyway so a missing user and a wrong password take the
            # same time, which otherwise leaks which usernames exist.
            hash_password(password or "x")
            return None
        if not verify_password(password or "", user.password_hash):
            return None
        return user

    def get(self, username: str) -> User | None:
        return self._users.get((username or "").strip().lower())

    def list(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: u.username.lower())

    def delete(self, username: str) -> bool:
        with self._lock:
            if self._users.pop((username or "").strip().lower(), None):
                self._save()
                return True
            return False

    def count(self) -> int:
        return len(self._users)

    def ensure_bootstrap(self, username: str, password: str) -> User | None:
        """Create the first admin account. No-op once any user exists."""
        if self.count():
            return None
        return self.create(username, password, role="admin")

    # -------------------------------------------------------------- sessions

    def issue(self, user: User, ttl: int = SESSION_TTL) -> str:
        payload = f"{user.id}:{user.username}:{int(time.time() + ttl)}"
        body = _b64(payload.encode())
        signature = hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()
        return f"{body}.{_b64(signature)}"

    def read(self, token: str) -> User | None:
        if not token or "." not in token:
            return None
        body, _, signature = token.rpartition(".")
        try:
            payload = _unb64(body).decode()
            given = _unb64(signature)
        except (ValueError, UnicodeDecodeError):
            return None
        expected = hmac.new(self._secret, payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(expected, given):
            return None
        parts = payload.split(":")
        if len(parts) != 3:
            return None
        try:
            expires = int(parts[2])
        except ValueError:
            return None
        if expires < time.time():
            return None
        user = self._users.get(parts[1].lower())
        if user is None or not user.enabled:
            return None
        return user