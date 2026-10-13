"""Continuous recording and motion detection."""

from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

log = logging.getLogger("recorder")

PROBE_W, PROBE_H = 64, 48
MOTION_PIXEL_DELTA = 12      # per-pixel luma change that counts as movement
MOTION_PIXEL_RATIO = 0.015   # fraction of the frame that must change
MOTION_COOLDOWN = 30.0       # seconds between events for one device

# Default base recording directory - can be overridden via environment or config
DEFAULT_RECORDINGS_DIR = os.environ.get(
    "RECORDINGS_BASE_DIR",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "recordings"),
)


class Recorder:
    """Writes the elementary stream to disk in time-based segments."""

    def __init__(self, media_dir: Path | str = DEFAULT_RECORDINGS_DIR):
        if isinstance(media_dir, str):
            media_dir = Path(media_dir)
        self.media_dir = media_dir
        self._sessions: dict[str, "_RecorderRun"] = {}
        self._ensure_dir()

    def _ensure_dir(self) -> None:
        """Create the base recordings directory if it doesn't exist."""
        try:
            self.media_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("could not create recordings directory %s: %s", self.media_dir, exc)

    def is_recording(self, device_id: str) -> bool:
        run = self._sessions.get(device_id)
        return bool(run and run.active)

    def status(self) -> list[dict]:
        return [run.describe() for run in self._sessions.values() if run.active]

    def stop(self, device_id: str) -> bool:
        run = self._sessions.pop(device_id, None)
        if run:
            run.stop()
            return True
        return False

    def start(self, session, segment_seconds: int = 900) -> "_RecorderRun":
        run = self._sessions.get(session.device.id)
        if run and run.active:
            return run
        # Use device-specific subdirectory if configured, otherwise base dir
        device_dir = self.media_dir / session.device.id
        run = _RecorderRun(session, device_dir, segment_seconds)
        self._sessions[session.device.id] = run
        run.start()
        return run

    def stop_all(self) -> None:
        for run in list(self._sessions.values()):
            run.stop()
        self._sessions.clear()


class _RecorderRun:
    def __init__(self, session, out_dir: Path, segment_seconds: int):
        self.session = session
        self.out_dir = out_dir
        self.segment_seconds = max(60, int(segment_seconds))
        self.active = False
        self.started_at = 0.0
        self.bytes_written = 0
        self.segments: list[dict] = []
        self._fh = None
        self._ext = ".h264"
        self._current: Path | None = None
        self._segment_started = 0.0
        self._stop = threading.Event()
        self._sub = None
        self._thread: threading.Thread | None = None
        self._ensure_dir()

    # ------------------------------------------------------------------ run

    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.active = True
        self.started_at = time.time()
        self._stop.clear()
        self._open_segment()
        self._sub = self.session.subscribe(include_gop=True)
        self._thread = threading.Thread(target=self._pump, name="recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self.active = False
        if self._sub:
            # subscribe() registers on the session's public subscriber list.
            self.session.unsubscribe(self._sub)
            self._sub = None
        self._close_segment()

    def _pump(self) -> None:
        if not self.session.loop.is_running():
            asyncio.run(self._consume())
            return
        # The hub always runs inside the server's loop; hop onto it so the
        # subscriber's asyncio.Queue is awaited on the right loop. The future's
        # exception is retrieved explicitly - otherwise a crash in _consume is
        # swallowed and the recording silently writes nothing.
        future = asyncio.run_coroutine_threadsafe(self._consume(), self.session.loop)
        future.add_done_callback(self._report_future_error)

    def _report_future_error(self, future) -> None:
        try:
            exc = future.exception()
        except Exception:                      # pragma: no cover - cancelled
            return
        if exc:
            log.error("recorder %s failed: %r", self.session.device.name, exc)

    async def _consume(self) -> None:
        sub = self._sub
        while not self._stop.is_set() and sub is not None and not sub.closed:
            try:
                item = await asyncio.wait_for(sub.get(), timeout=2.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                continue
            except Exception as exc:              # noqa: BLE001
                log.error("recorder queue failed: %r", exc)
                return
            # Control messages carry no media; the GOP now arrives as binary.
            if isinstance(item, dict):
                continue
            if item:
                self._write(item)
                self._rotate_if_needed()

    def _write(self, data: bytes) -> None:
        if self._fh is None:
            self._open_segment()
        if self._fh is None:
            return
        try:
            self._fh.write(data)
            self.bytes_written += len(data)
        except OSError as exc:
            log.error("recording write failed: %s", exc)
            self._close_segment()

    def _ensure_dir(self) -> None:
        """Create the output directory if it doesn't exist.

        Works for both local paths and NAS UNC paths (Windows UNC or
        Linux /mnt/path). For UNC paths, we use os.makedirs which handles them."""
        try:
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.warning("could not create recording directory %s: %s", self.out_dir, exc)

    def _open_segment(self) -> None:
        self._close_segment()
        self._ensure_dir()
        codec_name = self.session.sets.codec or "h264"
        self._ext = ".h265" if codec_name == "h265" else ".h264"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self._current = self.out_dir / f"{stamp}{self._ext}"
        self._fh = open(self._current, "wb")
        self._segment_started = time.time()
        self.segments.append({"file": self._current.name,
                              "startedAt": self._segment_started})

    def _close_segment(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            if self.segments:
                self.segments[-1]["endedAt"] = time.time()
                self.segments[-1]["bytes"] = (
                    self.segments[-1].get("bytes", 0)
                ) or (self._current.stat().st_size
                      if self._current and self._current.exists() else 0)
            self._fh = None

    def _rotate_if_needed(self) -> None:
        if time.time() - self._segment_started >= self.segment_seconds:
            self._open_segment()

    def describe(self) -> dict:
        return {
            "deviceId": self.session.device.id,
            "deviceName": self.session.device.name,
            "active": self.active,
            "startedAt": self.started_at,
            "durationS": int(time.time() - self.started_at) if self.active else 0,
            "bytes": self.bytes_written,
            "segments": list(self.segments[-5:]),
            "dir": str(self.out_dir),
        }


class MotionDetector:
    """Frame-difference motion detection driven by device JPEG snapshots.

    Cheaper and far more robust than decoding the video stream, and it works
    on channels that only expose stills.
    """

    def __init__(self, event_sink, interval: float = 5.0):
        self.event_sink = event_sink
        self.interval = interval
        self._threads: dict[str, threading.Thread] = {}
        self._stops: dict[str, threading.Event] = {}
        self._previous: dict[str, bytes] = {}
        self._last_event: dict[str, float] = {}
        self.active: set[str] = set()

    def toggle(self, device_id: str, session) -> bool:
        if device_id in self._threads:
            self.stop(device_id)
            return False
        stop = threading.Event()
        thread = threading.Thread(
            target=self._run, args=(session, stop), name=f"motion-{device_id}",
            daemon=True,
        )
        self._stops[device_id] = stop
        self._threads[device_id] = thread
        self.active.add(device_id)
        thread.start()
        return True

    def stop(self, device_id: str) -> None:
        stop = self._stops.pop(device_id, None)
        if stop:
            stop.set()
        self._threads.pop(device_id, None)
        self._active_discard(device_id)

    def _active_discard(self, device_id: str) -> None:
        self.active.discard(device_id)

    def stop_all(self) -> None:
        for device_id in list(self._threads):
            self.stop(device_id)

    def _run(self, session, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                jpeg = session.snapshot()
                if jpeg:
                    probe = self._probe(jpeg)
                    key = session.device.id
                    previous = self._previous.get(key)
                    if previous is not None:
                        score = self._score(previous, probe)
                        now = time.time()
                        if score >= MOTION_PIXEL_RATIO * 100 and (
                            now - self._last_event.get(key, 0) > MOTION_COOLDOWN
                        ):
                            self._last_event[key] = now
                            self.event_sink({
                                "deviceId": key,
                                "deviceName": session.device.name,
                                "timestamp": now,
                                "score": round(score, 2),
                                "thumbnail": jpeg,
                            })
                    self._previous[key] = probe
            except Exception as exc:
                log.debug("motion probe failed for %s: %s", session.device.name, exc)
            stop.wait(self.interval)

    @staticmethod
    def _probe(jpeg: bytes) -> bytes:
        with Image.open(io.BytesIO(jpeg)) as img:
            small = img.convert("L").resize((PROBE_W, PROBE_H), Image.BILINEAR)
            return small.tobytes()

    @staticmethod
    def _score(a: bytes, b: bytes) -> float:
        """Percentage of pixels whose luma moved by more than the threshold.

        A mean-absolute-difference over the whole frame is dominated by the
        (large, unchanging) background: a subject entering frame reads ~0.4
        against a noise floor of ~0.3. Counting the *fraction* of pixels that
        actually moved is scale-invariant and separates the two cleanly.
        """
        if len(a) != len(b):
            return 100.0
        moved = sum(1 for x, y in zip(a, b) if abs(x - y) > MOTION_PIXEL_DELTA)
        return moved * 100.0 / len(a)

    # Retained for callers/tests written against the old name.
    _delta = _score


def list_recordings(media_dir: Path | str = DEFAULT_RECORDINGS_DIR) -> list[dict]:
    """Enumerate recorded segments newest first."""
    if isinstance(media_dir, str):
        media_dir = Path(media_dir)
    items: list[dict] = []
    if not media_dir.exists():
        return items
    for device_dir in sorted(media_dir.iterdir()):
        if not device_dir.is_dir():
            continue
        for file in sorted(device_dir.glob("*")):
            if file.suffix.lower() not in (".h264", ".h265", ".mp4"):
                continue
            stat = file.stat()
            items.append({
                "deviceId": device_dir.name,
                "file": file.name,
                "path": str(file),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items


def list_all_recordings() -> list[dict]:
    """Enumerate all recorded segments across all device directories newest first."""
    items: list[dict] = []
    if not DEFAULT_RECORDINGS_DIR.exists():
        return items
    for device_dir in sorted(DEFAULT_RECORDINGS_DIR.iterdir()):
        if not device_dir.is_dir():
            continue
        for file in sorted(device_dir.glob("*")):
            if file.suffix.lower() not in (".h264", ".h265", ".mp4"):
                continue
            stat = file.stat()
            items.append({
                "deviceId": device_dir.name,
                "file": file.name,
                "path": str(file),
                "size": stat.st_size,
                "modified": stat.st_mtime,
            })
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items