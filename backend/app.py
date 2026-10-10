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

from fastapi import (Body, Depends, FastAPI, HTTPException, Query, Request,
                     WebSocket, WebSocketDisconnect)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from .auth import COOKIE_NAME, User, UserStore
from .dvrip_client import DvripAuthError, DvripError
from .transports.dahua_rpc2 import DahuaError, DahuaLockout
from .hub import StreamHub
from .recorder import MotionDetector, Recorder, list_recordings
from .store import TRANSPORTS, Device, DeviceStore, _data_dir

log = logging.getLogger("api")
FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
DATA_DIR = Path(_data_dir())
# Optional machine-to-machine secret, kept for scripted access and for the
# bootstrap case. Interactive users now get a real session cookie instead.
TOKEN = os.environ.get("SENTINEL_TOKEN", "")
ADMIN_BOOTSTRAP = os.environ.get("SENTINEL_ADMIN_PASSWORD", "")

OPEN_API = {"/api/auth/session", "/api/auth/login"}

STATE: dict = {}


def _store() -> DeviceStore:
    return STATE["store"]


def _hub() -> StreamHub:
    return STATE["hub"]


def _recorder() -> Recorder:
    return STATE["recorder"]


def _users() -> "UserStore":
    return STATE["users"]


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
    users = UserStore(DATA_DIR)
    STATE.update(store=store, hub=hub, recorder=recorder, motion=motion,
                 users=users, events=deque(maxlen=500))
    if ADMIN_BOOTSTRAP:
        created = users.ensure_bootstrap("admin", ADMIN_BOOTSTRAP)
        if created:
            log.info("created bootstrap admin account 'admin'")
    log.info("Sentinel ready: %s device(s), %s user(s), data in %s",
             len(store.list()), users.count(), DATA_DIR)
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
    if os.environ.get("SENTINEL_REQUIRE_TOKEN") == "1":
        if not TOKEN and _users().count() == 0:
            raise RuntimeError(
                "SENTINEL_REQUIRE_TOKEN=1 but there is no way in: no "
                "SENTINEL_TOKEN and no user accounts. Set "
                "SENTINEL_ADMIN_PASSWORD=<long random string> to create the "
                "first admin on boot, or `fly secrets set "
                "SENTINEL_TOKEN=$(openssl rand -hex 32)` for API access."
            )
        return
    if TOKEN:
        log.info("SENTINEL_TOKEN set - API accepts it as well as user sessions")
    if _users().count():
        log.info("%s user account(s); interactive access requires sign-in",
                 _users().count())
        return
    bind = os.environ.get("SENTINEL_BIND", "0.0.0.0")
    if bind in ("127.0.0.1", "localhost", "::1"):
        return
    log.warning(
        "=" * 66 + "\n"
        "  NO USERS AND NO TOKEN. This server can view and control every\n"
        "  configured camera, with no sign-in.\n"
        "    set SENTINEL_ADMIN_PASSWORD=<password> to create the first admin\n"
        "  (or SENTINEL_TOKEN for scripted API access).\n"
        + "=" * 66
    )


app = FastAPI(title="Sentinel Live", version="1.0.0", lifespan=lifespan)


def _resolve_user(request: Request) -> User | None:
    """Session cookie first, then the machine token for scripted access."""
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        user = _users().read(cookie)
        if user is not None:
            return user
    if TOKEN:
        supplied = (request.headers.get("authorization") or "").removeprefix("Bearer ")
        if not supplied:
            supplied = request.query_params.get("token", "")
        if supplied and hmac.compare_digest(supplied, TOKEN):
            return User(id="token", username="api-token",
                        password_hash="", role="admin")
    return None


@app.middleware("http")
async def guard(request: Request, call_next):
    if not request.url.path.startswith("/api/"):
        return await call_next(request)

    request.state.user = None
    if request.url.path in OPEN_API:
        return await call_next(request)

    if _resolve_user(request) is None:
        return JSONResponse({"detail": "authentication required"},
                            status_code=401)
    return await call_next(request)


def require(action: str):
    """Dependency that resolves the session user and checks their role."""
    async def dependency(request: Request) -> User:
        user = _resolve_user(request)
        if user is None:
            raise HTTPException(401, "authentication required")
        if not user.can(action):
            raise HTTPException(
                403, f"role '{user.role}' is not allowed to {action}")
        return user
    return dependency


# ---------------------------------------------------------------- auth

@app.get("/api/auth/session")
async def auth_session(request: Request):
    """Open by design: the UI calls this to decide whether to show login."""
    user = _resolve_user(request)
    if user is None:
        return {"authenticated": False, "needsSetup": _users().count() == 0}
    return {"authenticated": True, "user": user.public()}


@app.post("/api/auth/login")
async def auth_login(payload: dict = Body(...)):
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        raise HTTPException(400, "username and password are required")

    users = _users()
    if users.count() == 0:
        # First run: the credentials supplied become the first admin.
        try:
            user = users.create(username, password, role="admin")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        log.info("created first admin account '%s'", user.username)
    else:
        user = users.authenticate(username, password)
        if user is None:
            # Deliberately vague, and no per-account throttling beyond what
            # PBKDF2's cost already imposes.
            raise HTTPException(401, "incorrect username or password")

    token = users.issue(user)
    response = JSONResponse({"user": user.public()})
    response.set_cookie(
        COOKIE_NAME, token,
        httponly=True, samesite="lax",
        secure=os.environ.get("SENTINEL_COOKIE_SECURE") == "1",
        max_age=12 * 3600,
        path="/",
    )
    return response


@app.post("/api/auth/logout")
async def auth_logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE_NAME, path="/")
    return response


@app.get("/api/auth/users")
async def auth_users(user: User = Depends(require("manage"))):
    return {"users": [u.public() for u in _users().list()]}


@app.post("/api/auth/users")
async def auth_create_user(payload: dict = Body(...),
                           user: User = Depends(require("manage"))):
    role = payload.get("role") or "viewer"
    if role not in ("viewer", "operator", "admin"):
        raise HTTPException(400, "unknown role")
    try:
        created = _users().create(payload.get("username") or "",
                                  payload.get("password") or "", role=role)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    log.info("user '%s' created %s by '%s'", created.username, role, user.username)
    return created.public()


@app.delete("/api/auth/users/{username}")
async def auth_delete_user(username: str, user: User = Depends(require("manage"))):
    if user.username.lower() == username.lower() and user.id != "token":
        raise HTTPException(400, "you cannot delete your own account")
    if not _users().delete(username):
        raise HTTPException(404, "no such user")
    log.info("user '%s' deleted by '%s'", username, user.username)
    return {"deleted": username}


# --------------------------------------------------------------------- REST


@app.get("/healthz")
async def healthz():
    """Liveness probe for the hosting platform.

    Deliberately unauthenticated and deliberately says nothing about the
    devices: a public /healthz must not become a way to enumerate cameras.
    """
    return {"status": "ok"}


@app.get("/api/devices")
async def list_devices(request: Request):
    out = []
    for device in _store().list():
        session = _hub()._sessions.get(device.id)
        entry = device.redacted()
        entry["health"] = session.health_dict() if session else None
        entry["recording"] = _recorder().is_recording(device.id)
        entry["motion"] = device.id in STATE["motion"].active
        dev = STATE.get("dahua", {}).get(device.id)
        if dev:
            entry["health"] = dev.health_dict()
        out.append(entry)
    me = _resolve_user(request)
    return {
        "devices": out,
        "user": me.public() if me else None,
        "mayManage": bool(me and me.can("manage")),
        "mayPtz": bool(me and me.can("ptz")),
        "mayRecord": bool(me and me.can("record")),
    }


def _dahua(device_id: str):
    """Lazily build the Dahua session wrapper for a device."""
    from .dahua_session import DahuaDevice
    sessions = STATE.setdefault("dahua", {})
    dev = sessions.get(device_id)
    if dev is None:
        dev = DahuaDevice(_store().require(device_id), _store())
        sessions[device_id] = dev
    return dev


def _device_or_404(device_id: str) -> Device:
    device = _store().get(device_id)
    if device is None:
        raise HTTPException(404, "no such device")
    return device


@app.post("/api/devices")
async def create_device(payload: dict = Body(...),
                      user: User = Depends(require("manage"))):
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
async def update_device(device_id: str, payload: dict = Body(...),
                      user: User = Depends(require("manage"))):
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
async def delete_device(device_id: str, user: User = Depends(require("manage"))):
    session = _hub()._sessions.pop(device_id, None)
    if session:
        session.stop()
    STATE.get("dahua", {}).pop(device_id, None)
    STATE["motion"].stop(device_id)
    _recorder().stop(device_id)
    if not _store().delete(device_id):
        raise HTTPException(404, "no such device")
    return {"deleted": device_id}


@app.post("/api/devices/import")
async def import_devices(payload: dict = Body(...),
                        user: User = Depends(require("manage"))):
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
    device = _device_or_404(device_id)
    if device.transport == "dahua":
        from .dahua_session import DahuaDevice
        dev = _dahua(device_id)
        try:
            return await asyncio.to_thread(dev.probe)
        except DahuaLockout as exc:
            raise HTTPException(429, str(exc)) from exc
        except (DahuaError, OSError) as exc:
            raise HTTPException(502, str(exc)) from exc
    session = _hub().session(device_id)
    try:
        return await asyncio.to_thread(session.probe)
    except DvripAuthError as exc:
        raise HTTPException(401, str(exc)) from exc
    except (DvripError, OSError, TimeoutError) as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/devices/{device_id}/stream")
async def set_stream(device_id: str, payload: dict = Body(...)):
    device = _device_or_404(device_id)
    if device.transport == "dahua":
        dev = _dahua(device_id)
        if payload.get("on", True):
            await asyncio.to_thread(dev.start)
        else:
            dev.stop()
        return {"streaming": dev.health["state"] == "live",
                "mode": "snapshot"}
    session = _hub().session(device_id)
    if payload.get("on", True):
        session.start()
    else:
        session.stop()
    return {"streaming": session.running, "mode": "dvrip"}


@app.get("/api/devices/{device_id}/snapshot.jpg")
async def snapshot(device_id: str, channel: int = Query(0, ge=0, le=63)):
    device = _device_or_404(device_id)
    if device.transport == "dahua":
        dev = _dahua(device_id)
        try:
            jpeg = await asyncio.to_thread(dev.snapshot, channel)
        except DahuaLockout as exc:
            raise HTTPException(429, str(exc)) from exc
        except (DahuaError, OSError) as exc:
            raise HTTPException(502, str(exc)) from exc
        if not jpeg:
            raise HTTPException(502, "device returned no image")
        return Response(jpeg, media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
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
async def ptz(device_id: str, payload: dict = Body(...),
               user: User = Depends(require("ptz"))):
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
async def set_recording(device_id: str, payload: dict = Body(...),
                     user: User = Depends(require("record"))):
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
    dahua = list(STATE.get("dahua", {}).values())
    return {
        "sessions": [s.health_dict() | {"deviceId": s.device.id,
                                        "deviceName": s.device.name}
                     for s in sessions]
                    + [d.health_dict() | {"deviceId": d.device.id,
                                          "deviceName": d.device.name,
                                          "mode": "snapshot"}
                       for d in dahua],
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
    # Authenticate by cookie: same-origin WebSockets carry it automatically,
    # so the session secret never lands in a URL that proxies will log.
    user = None
    cookie = websocket.cookies.get(COOKIE_NAME)
    if cookie:
        user = _users().read(cookie)
    if user is None and TOKEN and hmac.compare_digest(
        websocket.query_params.get("token", ""), TOKEN
    ):
        user = User(id="token", username="api-token", password_hash="",
                    role="admin")
    if user is None or not user.can("view"):
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