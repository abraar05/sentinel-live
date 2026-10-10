"""Dahua-style RPC2 transport (web API + RTSP).

Many Xiongmai-firmware recorders are rebadged by other vendors - this one
reports ``devType=DHI-NVR1108HS-8P-S3/H`` - and the Xiongmai web interface
exposes them through a JSON-RPC endpoint rather than the old ``cgi-bin`` API.

Login is a challenge/response:

1. ``POST /RPC2_Login`` with an empty password returns ``random``, ``realm``,
   an ``authorization`` blob and a session.
2. The answer is::

       o = md5("<user>:<realm>:<password>")
       response = md5("<user>:<random>:<o>")

   sent as a second ``global.login`` carrying the first call's session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("dahua")

LOGIN_PATH = "/RPC2_Login"
SNAP_PATH = "/cgi-bin/Snapshot.cgi"
USER_AGENT = "Sentinel/1.0"


class DahuaError(Exception):
    pass


class DahuaLockout(DahuaError):
    """Too many failed logins; the recorder refuses further attempts."""

    def __init__(self, seconds: int, attempts: int):
        super().__init__(
            f"login locked for {seconds}s ({attempts} attempts remaining)")
        self.seconds = seconds
        self.attempts = attempts


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class DahuaSession:
    """One authenticated RPC2 session. Guards itself with a lock: the
    device allows one login at a time and answers the wrong request when two
    race."""

    def __init__(self, host: str, username: str, password: str,
                 port: int = 80, timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.base = f"http://{host}:{port}"
        self.username = username or "admin"
        self.password = password or ""
        self.timeout = timeout
        self.session: int | None = None
        self.cookie = ""
        self.channels: list[dict] = []
        self.lock = threading.RLock()

    # ------------------------------------------------------------------ http

    def _post(self, path: str, body: dict, cookie: str = "") -> tuple[int, dict, str]:
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "User-Agent": USER_AGENT},
            method="POST")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return res.status, dict(res.headers), res.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            return exc.code, dict(exc.headers), exc.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            raise DahuaError(f"cannot reach {self.base}: {exc}") from exc

    def _get(self, path: str, cookie: str = "") -> tuple[int, bytes, dict]:
        req = urllib.request.Request(self.base + path,
                                     headers={"User-Agent": USER_AGENT})
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                return res.status, res.read(), dict(res.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)
        except (urllib.error.URLError, OSError) as exc:
            raise DahuaError(f"cannot reach {self.base}: {exc}") from exc

    # ----------------------------------------------------------------- login

    @staticmethod
    def answer(realm: str, random: str, user: str, password: str) -> str:
        """The challenge/response the recorder expects."""
        inner = _md5(f"{user}:{realm}:{password}")
        return _md5(f"{user}:{random}:{inner}")

    def login(self) -> bool:
        with self.lock:
            status, headers, body = self._post(LOGIN_PATH, {
                "method": "global.login",
                "params": {"userName": self.username, "password": "",
                           "clientType": "Web3.0"},
                "id": 1,
            })
            try:
                challenge = json.loads(body)
            except ValueError as exc:
                raise DahuaError(f"unexpected login reply: {body[:120]}") from exc

            params = challenge.get("params", {})
            if "challenge" not in body:
                # A lockout is reported on the challenge call too, when the
                # recorder is still counting down from an earlier failure.
                lock = params.get("remainLockSecond")
                if lock:
                    raise DahuaLockout(int(lock),
                                       int(params.get("remainLoginTimes", 0)))
                if challenge.get("result") is True:
                    self.session = challenge.get("session")
                    self.cookie = headers.get("Set-Cookie", "").split(";")[0]
                    return True
                raise DahuaError(
                    f"login refused: {challenge.get('error', {}).get('message')}")

            encryption = params.get("encryption", "Default")
            response = self.answer(params.get("realm", ""),
                                   params.get("random", ""),
                                   self.username, self.password)

            second = {
                "method": "global.login",
                "params": {
                    "userName": self.username,
                    "password": response,
                    "clientType": "Web3.0",
                    "authorityType": encryption,
                    "passwordType": encryption,
                },
                "id": 2,
            }
            if challenge.get("session") is not None:
                second["session"] = challenge["session"]

            cookie = headers.get("Set-Cookie", "").split(";")[0]
            _, headers2, body2 = self._post(LOGIN_PATH, second, cookie)

            try:
                reply = json.loads(body2)
            except ValueError as exc:
                raise DahuaError(f"unexpected login reply: {body2[:120]}") from exc

            if reply.get("remainLockSecond") is not None or \
                    reply.get("params", {}).get("remainLockSecond"):
                lock = reply.get("params", {}).get("remainLockSecond", 0)
                attempts = reply.get("params", {}).get("remainLoginTimes", 0)
                raise DahuaLockout(int(lock), int(attempts))

            if reply.get("result") is True or reply.get("error") is None:
                self.session = reply.get("session")
                self.cookie = headers2.get("Set-Cookie", "") or cookie
                return True
            raise DahuaError("username or password is incorrect")

    # -------------------------------------------------------------- queries

    def rpc(self, method: str, params: dict | None = None, path: str = "/RPC2",
            retries: int = 1) -> dict:
        """Call an RPC2 method, refreshing the session once if it expires."""
        for attempt in range(retries + 1):
            if self.session is None:
                self.login()
            body = {"method": method, "params": params or {}, "id": 10,
                    "session": self.session}
            _, _, raw = self._post(path, body, self.cookie)
            try:
                reply = json.loads(raw)
            except ValueError as exc:
                raise DahuaError(f"bad reply from {method}: {raw[:120]}") from exc
            if reply.get("error") in (268632079,) or reply.get("session") is None:
                if attempt < retries:
                    self.session = None
                    self.cookie = ""
                    continue
            return reply
        return {}

    def probe(self) -> dict:
        """Device model, serial and channel count."""
        info = self.rpc("magicBox.getDeviceInfo", {})
        device = (info.get("params") or {}).get("device", info.get("result") or {})
        try:
            count = int(device.get("VideoInChannels")
                        or device.get("videoInChannels") or 0)
        except (TypeError, ValueError):
            count = 0
        if not count:
            count = int((info.get("result") or {}).get("Channels", 0) or 0)
        self.channels = [{"index": i, "name": f"CH{i + 1}"} for i in range(count)]
        return {"device": device, "channels": self.channels}

    # ------------------------------------------------------------- captures

    def snapshot(self, channel: int = 0) -> bytes:
        """Full-resolution JPEG for one channel."""
        if self.session is None:
            self.login()
        query = urllib.parse.urlencode({
            "channel": channel + 1,
            "subtype": 0,
            "session": self.session,
        })
        status, body, _ = self._get(f"{SNAP_PATH}?{query}", self.cookie)
        if status == 401 or not body.startswith(b"\xff\xd8"):
            # Session expired or refused: retry once with a fresh login.
            self.session = None
            self.cookie = ""
            self.login()
            query = urllib.parse.urlencode({
                "channel": channel + 1, "subtype": 0, "session": self.session})
            status, body, _ = self._get(f"{SNAP_PATH}?{query}", self.cookie)
        if not body.startswith(b"\xff\xd8"):
            raise DahuaError(f"snapshot failed: HTTP {status}")
        return body

    def rtsp_url(self, channel: int = 0, high: bool = True) -> str:
        """Live URL, if the recorder's RTSP server is on and reachable."""
        host = self.host
        user = urllib.parse.quote(self.username, safe="")
        secret = urllib.parse.quote(self.password, safe="")
        if high:
            return (f"rtsp://{user}:{secret}@{host}:554/"
                    f"cam/realmonitor?channel={channel + 1}&subtype=0")
        return (f"rtsp://{user}:{secret}@{host}:554/"
                f"cam/realmonitor?channel={channel + 1}&subtype=1")

    # ------------------------------------------------------------- liveness

    def rtsp_reachable(self, port: int = 554, timeout: float = 4.0) -> bool:
        try:
            with socket.create_connection((self.host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def describe(self) -> str:
        state = "connected" if self.session is not None else "idle"
        return (f"{self.host}:{self.port} as {self.username} [{state}] "
                f"channels={len(self.channels)}")


def wait_until_unlocked(session: DahuaSession, poll: int = 30) -> None:
    """Block while the recorder is serving a login lockout."""
    while True:
        try:
            session.login()
            return
        except DahuaLockout as lock:
            log.warning("locked out for %ss - waiting", lock.seconds)
            time.sleep(min(lock.seconds, poll) + 1)
        except DahuaError:
            return