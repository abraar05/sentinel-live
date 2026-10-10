"""Dahua RPC2 session used by the app's Dahua transport.

Kept separate from :mod:`backend.hub` so the DVRIP code stays focused; this
only needs login, capability probing and snapshots, which is what a rebadged
recorder that has DVRIP disabled still gives us.
"""

from __future__ import annotations

import logging
import threading
import time

from .transports.dahua_rpc2 import DahuaError, DahuaLockout, DahuaSession

log = logging.getLogger("dahua.session")


class DahuaDevice:
    """One recorder reached over its web RPC2 API."""

    def __init__(self, device, store):
        self.device = device
        self.store = store
        self.health = {
            "state": "idle", "detail": "", "fps": 0.0, "kbps": 0.0,
            "width": 0, "height": 0, "codec": "jpeg", "latencyMs": None,
            "uptimeS": 0, "reconnects": 0, "dropped": 0, "frames": 0,
        }
        self.channels: list[dict] = []
        self.system_info: dict = {}
        self._session: DahuaSession | None = None
        self._lock = threading.RLock()
        self._connected_at = 0.0

    # ---------------------------------------------------------------- login

    def _secret(self) -> str:
        return self.store.secret(self.device)

    def session(self, refresh: bool = False) -> DahuaSession:
        """Authenticated session, created on demand."""
        with self._lock:
            if self._session is not None and self._session.session and not refresh:
                return self._session
            conn = DahuaSession(
                host=self.device.host,
                username=self.device.username,
                password=self._secret(),
                port=self.device.port or 80,
            )
            conn.login()
            self._session = conn
            self._connected_at = time.time()
            self.health["state"] = "live"
            self.health["detail"] = ""
            return conn

    def probe(self) -> dict:
        conn = self.session()
        info = conn.probe()
        self.channels = info["channels"]
        self.system_info = info["device"]
        self.health["state"] = "live"
        return {"device": info["device"], "channels": info["channels"],
                "rtsp": conn.rtsp_url(0),
                "rtspReachable": conn.rtsp_reachable()}

    # ------------------------------------------------------------- captures

    def snapshot(self, channel: int = 0) -> bytes:
        try:
            data = self.session().snapshot(channel)
        except (DahuaError, OSError):
            # Session expired between calls: rebuild it once.
            with self._lock:
                self._session = None
            data = self.session(refresh=True).snapshot(channel)
        self.health["frames"] += 1
        self.health["latencyMs"] = None
        return data

    def rtsp_url(self, channel: int = 0, high: bool = True) -> str:
        return self.session().rtsp_url(channel, high)

    # --------------------------------------------------------------- health

    def health_dict(self) -> dict:
        data = dict(self.health)
        data["uptimeS"] = int(time.time() - self._connected_at) \
            if self._connected_at else 0
        data["subscribers"] = 0
        return data

    def close(self) -> None:
        with self._lock:
            self._session = None
        self.health["state"] = "idle"

    def start(self) -> None:
        """Dahua previews are pulled per-snapshot, so 'starting' is a probe."""
        try:
            self.session()
        except DahuaLockout as lock:
            self.health["state"] = "error"
            self.health["detail"] = str(lock)
        except DahuaError as exc:
            self.health["state"] = "error"
            self.health["detail"] = str(exc)
        else:
            self.health["state"] = "live"
            self.health["detail"] = ""

    def stop(self, timeout: float = 0.0) -> None:
        self.close()