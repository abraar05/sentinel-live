"""HTTP + WebSocket API."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (Body, FastAPI, HTTPException, Query, Request, WebSocket,
                     WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .dvrip_client import DvripAuthError, DvripError
from .hub import StreamHub
from .recorder import MotionDetector, Recorder, list_recordings
from .store import TRANSPORTS, Device, DeviceStore, _data_dir

log = logging.getLogger("api")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
DATA_DIR = Path(_data_dir())
TOKEN = os.environ.get("SENTINEL_TOKEN", "")

STATE: dict = {}


def _store() -> DeviceStore:
    return STATE["store"]


def _hub() -> StreamHub:
    return STATE["hub"]


def _recorder() -> Recorder:
    return STATE["recorder"]


def _events() -> deque:
    return STATE["events"]


def push_event(event: dict) -> None:
    events = _events()
    event = {k: v for k, v in event.items() if k != "thumbnail"}
    events.appendleft({"ts": time.time(), **event})
    del events[500:]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    store = DeviceStore(str(DATA_DIR))
    hub = StreamHub(store, asyncio.get_running_loop())
    recorder = Recorder(DATA_DIR / "recordings")
    motion = MotionDetector(push_event)
    STATE.update(store=store, hub=hub, recorder=recorder, motion=motion,
                 events=deque(maxlen=500))
    log.info("Sentinel ready: %s device(s), data in %s",
             len(store.list()), DATA_DIR)
    _check_exposure()
    try:
        yield
    finally:
        motion.stop_all()
        recorder.stop_all()
        hub.stop_all()


def _check_exposure() -> None:
    """Refuse to let a CCTV control panel look like a public web app.

    Anyone who reaches an unauthenticated instance gets the camera list, the
    DVRIP credentials behind it, PTZ control and a live feed. Public hosting
    sets SENTINEL_REQUIRE_TOKEN=1, which makes that a start-up failure rather
    than something to notice in a log.
    """
    if os.environ.get("SENTINEL_REQUIRE_TOKEN") == "1" and not TOKEN:
        raise RuntimeError(
            "SENTINEL_REQUIRE_TOKEN=1 but SENTINEL_TOKEN is empty. Refusing to "
            "start: this instance would let anyone on the internet view and "
            "control your cameras. Set a long random token, e.g. "
            "`fly secrets set SENTINEL_TOKEN=$(openssl rand -hex 32)`."
        )
    if TOKEN:
        log.info("token auth enabled - keep the token out of shell history")
        return
    bind = os.environ.get("SENTINEL_BIND", "0.0.0.0")
    if bind in ("127.0.0.1", "localhost", "::1"):
        return
    log.warning(
        "=" * 66 + "\n"
        "  NO TOKEN SET. This server can view and control every configured\n"
        "  camera. Do NOT expose it to the internet.\n"
        "    - remote access from your phone: use Tailscale or a VPN, or\n"
        "    - if you must: run with SENTINEL_TOKEN=<long random string>\n"
        "      and put it behind TLS. SENTINEL_BIND=127.0.0.1 keeps it local.\n"
        + "=" * 66
    )


app = FastAPI(title="Sentinel Live", version="1.0.0", lifespan=lifespan)


def _authorised(request: Request) -> bool:
    if not TOKEN:
        return True
    supplied = (request.headers.get("authorization") or "").removeprefix("Bearer ")
    if not supplied:
        supplied = request.query_params.get("token", "")
        if not supplied and request.cookies.get("sentinel_token"):
            supplied = request.cookies["sentinel_token"]
    return hmac.compare_digest(supplied, TOKEN)


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.url.path.startswith("/api/") and not _authorised(request):
        return JSONResponse({"detail": "unauthorised"}, status_code=401)
    return await call_next(request)


# --------------------------------------------------------------------- REST


@app.get("/healthz")
async def healthz():
    """Liveness probe for the hosting platform.

    Deliberately unauthenticated and deliberately says nothing about the
    devices: a public /healthz must not become a way to enumerate cameras.
    """
    return {"status": "ok"}


@app.get("/api/devices")
async def list_devices():
    out = []
    for device in _store().list():
        session = _hub()._sessions.get(device.id)
        entry = device.redacted()
        entry["health"] = session.health_dict() if session else None
        entry["recording"] = _recorder().is_recording(device.id)
        entry["motion"] = device.id in STATE["motion"].active
        out.append(entry)
    return {"devices": out, "tokenRequired": bool(TOKEN)}


def _device_or_404(device_id: str) -> Device:
    device = _store().get(device_id)
    if device is None:
        raise HTTPException(404, "no such device")
    return device


@app.post("/api/devices")
async def create_device(payload: dict = Body(...)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    store = _store()
    password = payload.get("password") or ""
    device = Device(
        id=uuid.uuid4().hex[:12],
        name=name,
        host=(payload.get("host") or "").strip(),
        port=int(payload.get("port") or 34567),
        username=payload.get("username") or "admin",
        password=store.box.seal(password) if password else "",
        transport=payload.get("transport") or "dvrip",
        rtsp_path=payload.get("rtspPath") or "",
        notes=payload.get("notes") or "",
    )
    if device.transport not in TRANSPORTS:
        raise HTTPException(400, f"unknown transport {device.transport}")
    store.upsert(device)
    _hub()._sessions.pop(device.id, None)
    return device.redacted()


@app.patch("/api/devices/{device_id}")
async def update_device(device_id: str, payload: dict = Body(...)):
    device = _device_or_404(device_id)
    store = _store()
    for field in ("name", "host", "transport", "rtspPath", "notes"):
        if field in payload:
            setattr(device, field, payload[field])
    if "port" in payload:
        device.port = int(payload["port"])
    if "username" in payload:
        device.username = payload["username"]
    if "password" in payload and payload["password"]:
        device.password = store.box.seal(payload["password"])
    if "enabled" in payload:
        device.enabled = bool(payload["enabled"])
    if device.transport not in TRANSPORTS:
        raise HTTPException(400, f"unknown transport {device.transport}")
    store.upsert(device)
    session = _hub()._sessions.pop(device_id, None)
    if session:
        session.stop()
    return device.redacted()


@app.delete("/api/devices/{device_id}")
async def delete_device(device_id: str):
    session = _hub()._sessions.pop(device_id, None)
    if session:
        session.stop()
    STATE["motion"].stop(device_id)
    _recorder().stop(device_id)
    if not _store().delete(device_id):
        raise HTTPException(404, "no such device")
    return {"deleted": device_id}


@app.post("/api/devices/import")
async def import_devices(payload: dict = Body(...)):
    xml_text = payload.get("xml") or ""
    if not xml_text.strip():
        raise HTTPException(400, "no XML supplied")
    try:
        devices, warnings = _store().import_devicemanager_xml(
            xml_text, payload.get("hosts") or {}
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "imported": [d.redacted() for d in devices],
        "warnings": warnings,
    }


@app.post("/api/devices/{device_id}/probe")
async def probe_device(device_id: str):
    _device_or_404(device_id)
    session = _hub().session(device_id)
    try:
        return await asyncio.to_thread(session.probe)
    except DvripAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (DvripError, OSError, TimeoutError) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/devices/{device_id}/stream")
async def set_stream(device_id: str, payload: dict = Body(...)):
    _device_or_404(device_id)
    session = _hub().session(device_id)
    if payload.get("on", True):
        session.start()
    else:
        session.stop()
    return {"streaming": session.running}


@app.get("/api/devices/{device_id}/snapshot.jpg")
async def snapshot(device_id: str, channel: int = Query(0, ge=0, le=63)):
    _device_or_404(device_id)
    session = _hub().session(device_id)
    try:
        jpeg = await asyncio.to_thread(session.snapshot, channel)
    except DvripAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (DvripError, OSError, TimeoutError) as exc:
        raise HTTPException(502, str(exc)) from exc
    if not jpeg:
        raise HTTPException(502, "device returned no image")
    return Response(jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/devices/{device_id}/ptz")
async def ptz(device_id: str, payload: dict = Body(...)):
    _device_or_404(device_id)
    command = (payload.get("command") or "").strip()
    if command not in PTZ_COMMANDS:
        raise HTTPException(400, f"unsupported PTZ command {command!r}")
    session = _hub().session(device_id)
    try:
        reply = await asyncio.to_thread(
            session.ptz, command, int(payload.get("step", 5)),
            int(payload.get("preset", -1)), int(payload.get("channel", 0)),
            str(payload.get("pattern") or "Start"),
        )
    except DvripAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (DvripError, OSError, TimeoutError) as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"ret": reply.get("Ret"), "detail": reply.get("Detail", "")}


PTZ_COMMANDS = {
    "DirectionUp", "DirectionDown", "DirectionLeft", "DirectionRight",
    "DirectionLeftUp", "DirectionLeftDown", "DirectionRightUp",
    "DirectionRightDown", "ZoomTile", "ZoomWide", "FocusNear", "FocusFar",
    "SetPreset", "GotoPreset", "ClearPreset", "StartTour", "StopTour",
}


@app.post("/api/devices/{device_id}/record")
async def set_recording(device_id: str, payload: dict = Body(...)):
    _device_or_404(device_id)
    session = _hub().session(device_id)
    if payload.get("on", True):
        session.start()
        await asyncio.sleep(0)
        _recorder().start(session, int(payload.get("segmentSeconds", 900)))
    else:
        _recorder().stop(device_id)
    return {"recording": _recorder().is_recording(device_id)}


@app.post("/api/devices/{device_id}/motion")
async def set_motion(device_id: str, payload: dict = Body(...)):
    _device_or_404(device_id)
    session = _hub().session(device_id)
    on = STATE["motion"].toggle(device_id, session)
    return {"motion": on}


@app.get("/api/health")
async def health():
    sessions = _hub().all_sessions()
    return {
        "sessions": [s.health_dict() | {"deviceId": s.device.id,
                                        "deviceName": s.device.name}
                     for s in sessions],
        "recordings": _recorder().status(),
        "events": list(_events())[:50],
    }


@app.get("/api/recordings")
async def recordings():
    return {"recordings": list_recordings(DATA_DIR / "recordings")}


@app.get("/api/recordings/download")
async def download(path: str = Query(...)):
    target = (DATA_DIR / "recordings" / path).resolve()
    root = (DATA_DIR / "recordings").resolve()
    if not str(target).startswith(str(root)) or not target.is_file():
        raise HTTPException(404, "no such recording")
    return FileResponse(target, filename=target.name)


# ---------------------------------------------------------------- WebSocket


@app.websocket("/ws/stream/{device_id}")
async def stream_socket(websocket: WebSocket, device_id: str):
    if TOKEN and not hmac.compare_digest(
        websocket.query_params.get("token", ""), TOKEN
    ):
        await websocket.close(code=4401)
        return
    device = _store().get(device_id)
    if device is None:
        await websocket.close(code=4404)
        return

    await websocket.accept()
    session = _hub().session(device_id)
    session.start()
    sub = session.subscribe(include_gop=True)
    pump: asyncio.Task | None = None
    try:
        while True:
            try:
                item = await asyncio.wait_for(sub.get(), timeout=5.0)
            except asyncio.TimeoutError:
                await websocket.send_json({
                    "type": "status", **session.health_dict(),
                    "codec": session.sets.codec,
                    "codecString": session.sets.codec_string,
                })
                continue

            if isinstance(item, dict):
                message = {k: v for k, v in item.items() if k != "gop"}
                await websocket.send_json(message)
            else:
                await websocket.send_bytes(item)
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001 - surface, never kill the socket
        log.warning("stream socket %s ended: %s", device_id, exc)
    finally:
        session.unsubscribe(sub)
        try:
            await websocket.close()
        except RuntimeError:
            pass


# ------------------------------------------------------------------ frontend

if FRONTEND_DIR.exists():
    app.mount("/app", StaticFiles(directory=str(FRONTEND_DIR), html=True),
              name="frontend")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index():
    index_file = FRONTEND_DIR / "index.html"
    if not index_file.exists():
        return "<h1>Sentinel Live</h1><p>frontend missing</p>"
    return HTMLResponse(index_file.read_text(encoding="utf-8"))