"""Live preview sessions and the subscriber hub.

One :class:`DeviceSession` per device owns two DVRIP connections (control and
preview), keeps the preview alive across network blips, and fans elementary
stream chunks out to any number of consumers - WebSocket clients, the
recorder, the motion detector.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import codec
from .dvrip_client import DvripAuthError, DvripConnection, DvripError
from .store import Device, DeviceStore

log = logging.getLogger("hub")

MAX_QUEUE = 90          # chunks buffered per consumer before dropping
GOP_CACHE_BYTES = 800 * 1024
GOP_WIRE_CHUNK = 256 * 1024


@dataclass
class Health:
    state: str = "idle"            # idle | connecting | live | reconnecting | error
    detail: str = ""
    fps: float = 0.0
    kbps: float = 0.0
    width: int = 0
    height: int = 0
    codec: str = ""
    last_frame_at: float = 0.0
    connected_at: float = 0.0
    reconnects: int = 0
    dropped: int = 0
    frames: int = 0

    def to_dict(self) -> dict:
        now = time.time()
        return {
            "state": self.state,
            "detail": self.detail,
            "fps": round(self.fps, 1),
            "kbps": round(self.kbps, 1),
            "width": self.width,
            "height": self.height,
            "codec": self.codec,
            "latencyMs": int((now - self.last_frame_at) * 1000)
            if self.last_frame_at else None,
            "uptimeS": int(now - self.connected_at) if self.connected_at else 0,
            "reconnects": self.reconnects,
            "dropped": self.dropped,
            "frames": self.frames,
        }


class Subscriber:
    """An async consumer queue fed from the preview thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, name: str = ""):
        self.loop = loop
        self.name = name
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self.closed = False

    def offer(self, item) -> None:
        if self.closed:
            return
        try:
            self.loop.call_soon_threadsafe(self._put, item)
        except RuntimeError:
            self.closed = True

    def _put(self, item) -> None:
        if self.closed:
            return
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()      # drop oldest, keep it live
                self.queue.put_nowait(item)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    async def get(self):
        return await self.queue.get()

    def close(self) -> None:
        self.closed = True


class DeviceSession:
    def __init__(self, device: Device, store: DeviceStore, loop: asyncio.AbstractEventLoop):
        self.device = device
        self.store = store
        self.loop = loop
        self.health = Health()
        self.sets = codec.ParameterSets()
        self.subscribers: list[Subscriber] = []
        self.channels: list[dict] = []
        self.system_info: dict = {}
        self.encode_info: list = []

        self._control: DvripConnection | None = None
        self._preview: DvripConnection | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

        self._pending = bytearray()
        self._gop = deque(maxlen=4096)   # enough for ~1 GOP of access units
        self._gop_bytes = 0
        self._frame_times: deque = deque(maxlen=120)
        self._byte_times: deque = deque(maxlen=120)
        self._backoff = 1.0
        self._internal: list[Subscriber] = []
        self._connected_at = 0.0

    # ------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_preview, name=f"prev-{self.device.id}", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        with self._lock:
            self._stop.set()
            thread, self._thread = self._thread, None
        preview, self._preview = self._preview, None
        if preview:
            preview.close()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self.control_locked_close()

    def control_locked_close(self) -> None:
        with self._lock:
            control, self._control = self._control, None
        if control:
            control.close()
            self._set_health(state="idle")

    # -------------------------------------------------------------- control

    def _ensure_control(self) -> DvripConnection:
        with self._lock:
            if self._control is not None and self._control.sock is not None:
                return self._control
            conn = DvripConnection(
                host=self.device.host,
                user=self.device.username,
                password=self.store.secret(self.device),
                port=self.device.port,
            )
            conn.connect()
            conn.login()
            self._control = conn
            return conn

    def snapshot(self, channel: int = 0) -> bytes:
        conn = self._ensure_control()
        try:
            return conn.snapshot(channel=channel)
        except (DvripError, OSError, TimeoutError, struct.error):
            self.control_locked_close()
            conn = self._ensure_control()
            return conn.snapshot(channel=channel)

    def ptz(self, command: str, step: int = 5, preset: int = -1,
            channel: int = 0, pattern: str = "Start") -> dict:
        return self._ensure_control().ptz(command, step, preset, channel, pattern)

    def probe(self) -> dict:
        """Fetch device capabilities. Cheap enough to run on demand."""
        conn = self._ensure_control()
        self.system_info = conn.system_info()
        self.encode_info = conn.encode_info()
        channels = self.system_info.get("VideoInChannel") or 1
        self.channels = [
            {"index": i, "name": f"CH{i + 1}"} for i in range(int(channels))
        ]
        return {
            "systemInfo": self.system_info,
            "channels": self.channels,
            "encode": self.encode_info,
        }

    # -------------------------------------------------------------- preview

    def _run_preview(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            preview = None
            try:
                if not self.device.host:
                    raise DvripError(
                        "no host configured - this device came from a P2P "
                        "cloud-id export and must be given an address"
                    )
                self._set_health(state="connecting")
                preview = DvripConnection(
                    host=self.device.host,
                    user=self.device.username,
                    password=self.store.secret(self.device),
                    port=self.device.port,
                )
                preview.connect()
                preview.login()
                self._preview = preview
                attempt = 0
                self._set_health(state="connecting", detail="claiming stream")

                channel = 0
                stream_type = "Main"
                self._connected_at = time.time()
                self._set_health(state="live")

                for payload, meta in preview.iter_frames(channel, stream_type):
                    if self._stop.is_set():
                        break
                    if meta.get("type") == "idle" or not payload:
                        continue
                    self._ingest(payload, meta)
            except DvripAuthError as exc:
                self._set_health(state="error", detail=str(exc))
                break                      # bad credentials: retrying is pointless
            except (DvripError, OSError, TimeoutError, ValueError) as exc:
                attempt += 1
                if self._stop.is_set():
                    break
                self._set_health(state="reconnecting", detail=str(exc))
                self.health.reconnects += 1
                log.warning("preview %s failed (%s), attempt %s",
                            self.device.name, exc, attempt)
                if self._stop.wait(min(30.0, self._backoff)):
                    break
                self._backoff = min(30.0, self._backoff * 2)
            finally:
                if preview is not None:
                    preview.close()
                self._preview = None
                self.control_locked_close()

    def _key_message(self) -> dict:
        return {
            "type": "key",
            "codec": self.sets.codec,
            "codecString": self.sets.codec_string,
            "width": self.sets.width,
            "height": self.sets.height,
            "hasDescription": self.sets.description is not None,
            "state": self.health.state,
        }

    def _ingest(self, payload: bytes, meta: dict) -> None:
        now = time.time()
        self._pending.extend(payload)

        signature = ""
        if not self.sets.ready:
            if codec.looks_like_h265(payload):
                self.sets.codec = "h265"
            codec.extract_parameter_sets(
                bytes(self._pending[-512 * 1024:]), self.sets
            )
            if not self.sets.ready:
                return
            # Parameter sets arrived mid-stream. Tell every viewer now - a
            # subscriber that connected before this point has been waiting
            # for the codec string with no way to ask for it.
            signature = self._sets_signature()

        chunk = bytes(self._pending)
        self._pending.clear()
        if not chunk:
            return

        # Keep the most recent GOP so a late viewer starts on a keyframe.
        units = codec.to_access_units(chunk, self.sets.codec)
        for unit in units:
            self._gop.append(unit)
            self._gop_bytes += len(unit)
        self._trim_gop()

        self.health.frames += 1
        self.health.last_frame_at = now
        self._frame_times.append(now)
        self._byte_times.append((now, len(chunk)))
        self.health.width = self.sets.width
        self.health.height = self.sets.height
        self.health.codec = self.sets.codec

        targets = self.subscribers + self._internal
        if signature:
            message = self._key_message()
            for sub in list(targets):
                sub.offer(message)
            for sub in list(targets):
                sub.offer(chunk)
            return

        for sub in list(targets):
            sub.offer(chunk)

    def _trim_gop(self) -> None:
        """Bound the cached GOP and keep it anchored on a keyframe.

        A viewer joining mid-stream cannot decode delta frames, so the cache
        must begin at an IDR. Truncating it from the front may leave no IDR
        at all - in which case the cache empties and the viewer simply waits
        for the next one.
        """
        while self._gop_bytes > GOP_CACHE_BYTES and len(self._gop) > 1:
            self._gop_bytes -= len(self._gop.popleft())
        while self._gop and not codec.contains_keyframe(self._gop[0], self.sets.codec):
            self._gop_bytes -= len(self._gop.popleft())
        if self._gop_bytes < 0:
            self._gop_bytes = 0

    def _sets_signature(self) -> str:
        return f"{self.sets.codec}:{self.sets.codec_string}:{self.sets.width}"

    # ------------------------------------------------------------ subscribers

    def subscribe(self, include_gop: bool = True) -> Subscriber:
        sub = Subscriber(self.loop, self.device.name)
        self.subscribers.append(sub)
        if include_gop and self.sets.ready and self._gop:
            gop = b"".join(self._gop)
            message = self._key_message()
            message["gopBytes"] = len(gop)
            sub.offer(message)
            for start in range(0, len(gop), GOP_WIRE_CHUNK):
                sub.offer(gop[start:start + GOP_WIRE_CHUNK])
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self.subscribers:
            self.subscribers.remove(sub)
        sub.close()

    def add_internal(self, sub: Subscriber) -> None:
        self._internal.append(sub)

    def remove_internal(self, sub: Subscriber) -> None:
        if sub in self._internal:
            self._internal.remove(sub)
        sub.close()

    # ---------------------------------------------------------------- health

    def _set_health(self, state: str, detail: str = "") -> None:
        self.health.state = state
        if detail:
            self.health.detail = detail
        if state == "live":
            self.health.connected_at = self._connected_at
        self.health.width = self.sets.width
        self.health.height = self.sets.height
        self.health.codec = self.sets.codec

    def health_dict(self) -> dict:
        now = time.time()
        frames = list(self._frame_times)
        if len(frames) >= 2 and frames[-1] - frames[0] > 0:
            self.health.fps = (len(frames) - 1) / (frames[-1] - frames[0])
        elif len(frames) < 2:
            self.health.fps = 0.0
        window = [b for t, b in self._byte_times if now - t <= 10]
        span = 10.0 if self._byte_times else 0.0
        if window and self._byte_times:
            first_t = self._byte_times[0][0]
            span = max(1.0, now - first_t)
            self.health.kbps = sum(window) * 8 / span / 1000
        data = self.health.to_dict()
        data["subscribers"] = len(self.subscribers)
        return data


def struct_error():
    import struct
    return struct.error


class StreamHub:
    def __init__(self, store: DeviceStore, loop: asyncio.AbstractEventLoop):
        self.store = store
        self.loop = loop
        self._sessions: dict[str, DeviceSession] = {}

    def session(self, device_id: str, create: bool = True) -> DeviceSession | None:
        session = self._sessions.get(device_id)
        if session:
            return session
        device = self.store.get(device_id)
        if device is None or not create:
            return None
        session = DeviceSession(device, self.store, self.loop)
        self._sessions[device_id] = session
        return session

    def all_sessions(self) -> list[DeviceSession]:
        return list(self._sessions.values())

    def stop_all(self) -> None:
        for session in self._sessions.values():
            session.stop()
        self._sessions.clear()