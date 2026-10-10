"""Tests for the Dahua RPC2 transport.

The challenge/response is checked against a value derived by hand from the
recorder's own JavaScript, and the login flow runs against a local stub that
reproduces the real device's replies - including the lockout behaviour that
matters in the field.
"""

from __future__ import annotations

import hashlib
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from backend.transports.dahua_rpc2 import (DahuaError,      # noqa: E402
                                            DahuaLockout,
                                            DahuaSession)

REALM = "Login to 5c7f82ae10f65f4321a0143bdb69551b"
RANDOM = "1593630018"
GOOD_PASSWORD = "correct-horse-battery"
BAD_PASSWORD = "definitely-wrong"
JPEG = b"\xff\xd8\xff\xe0" + b"fakejpeg" * 40


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def expected_answer(user: str, password: str,
                    realm: str = REALM, random: str = RANDOM) -> str:
    return md5(f"{user}:{random}:{md5(f'{user}:{realm}:{password}')}")


class TestChallengeAlgorithm:
    def test_matches_hand_computed_value(self):
        assert DahuaSession.answer(REALM, RANDOM, "admin", GOOD_PASSWORD) == \
            expected_answer("admin", GOOD_PASSWORD)

    def test_known_value_is_stable(self):
        # Guards against a refactor silently inverting the nesting order -
        # md5(user:md5(...):random) is the easy mistake to make.
        value = DahuaSession.answer("Login to abc", "12345", "admin", "pw")
        assert value == md5("admin:12345:" + md5("admin:Login to abc:pw"))
        assert value != md5("admin:Login to abc:" + md5("admin:12345:pw"))

    def test_changing_any_input_changes_the_answer(self):
        base = DahuaSession.answer(REALM, RANDOM, "admin", GOOD_PASSWORD)
        assert DahuaSession.answer(REALM + "x", RANDOM, "admin", GOOD_PASSWORD) != base
        assert DahuaSession.answer(REALM, RANDOM + "x", "admin", GOOD_PASSWORD) != base
        assert DahuaSession.answer(REALM, RANDOM, "bob", GOOD_PASSWORD) != base
        assert DahuaSession.answer(REALM, RANDOM, "admin", GOOD_PASSWORD + "x") != base


# --------------------------------------------------------------- stub device

class StubRecorder(BaseHTTPRequestHandler):
    """Reproduces the RPC2 login dance, including lockout accounting."""

    password = GOOD_PASSWORD
    locked_for = 0
    attempts_left = 5
    sessions: dict = {}

    def log_message(self, *args):
        pass

    def _json(self, payload: dict, status: int = 200, cookie: str = ""):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            req = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": {"code": 1}}, 400)

        cls = type(self)
        if cls.locked_for > 0:
            return self._json({"error": {"code": 268632081}, "id": req.get("id", 1),
                               "result": False,
                               "params": {"remainLockSecond": cls.locked_for,
                                          "remainLoginTimes": cls.attempts_left}})

        supplied = req.get("params", {}).get("password", "")
        if not supplied:
            return self._json({
                "error": {"code": 268632079, "message": "Component error: login challenge!"},
                "id": req.get("id", 1),
                "params": {"authorization": "deadbeef", "encryption": "Default",
                           "random": RANDOM, "realm": REALM},
                "session": 4242,
            }, cookie="DhWebClientSessionID=stub; Path=/")

        if supplied == expected_answer(req["params"]["userName"], cls.password):
            sess = 777
            cls.sessions[sess] = True
            return self._json({"result": True, "id": req.get("id", 1),
                               "session": sess},
                              cookie="DhWebClientSessionID=stub; Path=/")

        cls.attempts_left = max(0, cls.attempts_left - 1)
        if cls.attempts_left == 0:
            cls.locked_for = 941
        return self._json({"error": {"code": 268632081, "message": ""},
                           "id": req.get("id", 1), "result": False,
                           "params": {"remainLockSecond": cls.locked_for,
                                      "remainLoginTimes": cls.attempts_left}})

    def do_GET(self):
        cls = type(self)
        if self.path.startswith("/cgi-bin/Snapshot.cgi"):
            cookie = self.headers.get("Cookie", "")
            if "DhWebClientSessionID" not in cookie:
                return self._json({"error": "unauthorised"}, 401)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(JPEG)))
            self.end_headers()
            self.wfile.write(JPEG)
            return
        if self.path == "/cap.js":
            body = b"var devType='DHI-NVR1108HS-8P-S3/H';"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


@pytest.fixture
def recorder():
    StubRecorder.password = GOOD_PASSWORD
    StubRecorder.locked_for = 0
    StubRecorder.attempts_left = 5
    StubRecorder.sessions = {}
    server = HTTPServer(("127.0.0.1", 0), StubRecorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_address[1]
    server.shutdown()


def session(port, password=GOOD_PASSWORD, username="admin"):
    return DahuaSession("127.0.0.1", username, password, port=port, timeout=5)


class TestLogin:
    def test_correct_password_logs_in(self, recorder):
        s = session(recorder)
        assert s.login() is True
        assert s.session == 777
        assert "DhWebClientSessionID" in s.cookie

    def test_wrong_password_rejected(self, recorder):
        s = session(recorder, BAD_PASSWORD)
        with pytest.raises(DahuaError, match="incorrect"):
            s.login()

    def test_lockout_is_reported_with_numbers(self, recorder):
        s = session(recorder, BAD_PASSWORD)
        with pytest.raises(DahuaError):
            s.login()          # burns attempts down to zero
        StubRecorder.locked_for = 941
        StubRecorder.attempts_left = 0
        s2 = session(recorder, GOOD_PASSWORD)
        with pytest.raises(DahuaLockout) as exc:
            s2.login()
        assert exc.value.seconds == 941
        assert exc.value.attempts == 0

    def test_lockout_blocks_even_correct_password(self, recorder):
        StubRecorder.locked_for = 300
        StubRecorder.attempts_left = 0
        with pytest.raises(DahuaLockout):
            session(recorder, GOOD_PASSWORD).login()


class TestCapture:
    def test_snapshot_returns_jpeg(self, recorder):
        s = session(recorder)
        s.login()
        assert s.snapshot(0) == JPEG

    def test_snapshot_logs_in_on_demand(self, recorder):
        s = session(recorder)
        assert s.snapshot(2) == JPEG
        assert s.session is not None

    def test_rtsp_url_encodes_credentials(self, recorder):
        s = session(recorder, "p@ss/word", username="user name")
        url = s.rtsp_url(channel=2)
        assert "channel=3" in url
        assert "user%20name" in url
        assert "p%40ss%2Fword" in url
        assert ":554/" in url

    def test_rtsp_low_stream_uses_subtype_1(self, recorder):
        s = session(recorder)
        assert "subtype=1" in s.rtsp_url(channel=0, high=False)

    def test_rtsp_reachability_check(self, recorder):
        s = session(recorder)
        assert s.rtsp_reachable(port=recorder) is True
        assert s.rtsp_reachable(port=1, timeout=0.5) is False


class TestUnreachable:
    def test_raises_rather_than_hanging(self):
        s = DahuaSession("127.0.0.1", "admin", "x", port=1, timeout=1.0)
        with pytest.raises(DahuaError, match="cannot reach"):
            s.login()