"""
DVRIP / NETsurveillance ("Sofia") protocol client.

Implements the TCP control + binary-preview protocol used by Xiongmai /
XMeye NVRs, DVRs and IP cameras. This is the protocol that the vendor's own
software speaks *after* the XMeye P2P cloud handshake, addressed directly at
host:34567.

Written from the public Xiongmai DVR API docs and cross-checked against the
`python-dvr` reference implementation. Unlike that library this client:

  * never deadlocks (the reference acquires a lock and leaks it on timeout),
  * treats socket timeouts as recoverable stream events rather than crashes,
  * separates control and preview onto independent connections.

Wire format, 20-byte header for both packet flavours:

    control : BB2xII2xHI   magic ver _ session seq _ msgid  length
    binary  : BB2xIIBBHI    magic ver _ session seq tot cur msgid length

`length` counts the payload plus the trailing b"\\n\\x00" terminator.
"""

from __future__ import annotations

import hashlib
import json
import logging
import socket
import struct
import threading
import time
from datetime import datetime

log = logging.getLogger("dvrip")

MAGIC = 0xFF
CTRL_HDR = struct.Struct("BB2xII2xHI")
BIN_HDR = struct.Struct("BB2xIIBBHI")

CMD_LOGIN = 1000
CMD_KEEPALIVE = 1006
CMD_SYSTEMINFO = 1020
CMD_SYSCAPS = 1360
CMD_MONITOR_CLAIM = 1413
CMD_MONITOR_START = 1410
CMD_PTZ = 1400
CMD_SNAPSHOT = 1560
CMD_ALARM_START = 1506

RET_OK = 100
RET_CODES = {
    100: "ok",
    101: "unknown error",
    102: "unsupported version",
    103: "request not permitted",
    104: "user already logged in",
    105: "user is not logged in",
    106: "username or password is incorrect",
    107: "insufficient permissions",
    203: "password is incorrect",
}

# Binary frame data types (big-endian uint32 tag).
DT_P_FRAME = 0x1FD
DT_P_FRAME2 = 0x1FC
DT_JPEG = 0x1FE
DT_AUDIO = 0x1FA
DT_INFO = 0x1F9

CODEC_BY_MEDIA = {1: "mpeg4", 2: "h264", 3: "h265"}
JPEG_MAGIC = 0xFFD8FFE0

PTZ_KEY = 0x1007  # direction key bitmask carrier


class DvripError(Exception):
    pass


class DvripAuthError(DvripError):
    pass


def sofia_hash(password: str) -> str:
    """Xiongmai password digest: base62 over paired md5 bytes."""
    digest = hashlib.md5(password.encode("utf-8")).digest()
    chars = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
    return "".join(chars[sum(p) % 62] for p in zip(digest[::2], digest[1::2]))


def _decode_datetime(raw: int) -> str:
    second = raw & 0x3F
    minute = (raw & 0xFC0) >> 6
    hour = (raw & 0x1F000) >> 12
    day = (raw & 0x1E0000) >> 17
    month = (raw & 0x3C00000) >> 22
    year = ((raw & 0xFC000000) >> 26) + 2000
    try:
        return datetime(year, month, day, hour, minute, second).isoformat()
    except ValueError:
        return ""


class _Deferred:
    """Marker for a connection that must not run its own keepalive thread."""

    __slots__ = ()


class DvripConnection:
    """One authenticated control connection.

    Not thread-safe for concurrent request/response: the reference protocol
    multiplexes strictly sequentially, so callers should hold :attr:`lock`.
    """

    def __init__(self, host: str, user: str, password: str, port: int = 34567,
                 timeout: float = 8.0, defer_keepalive: bool = False):
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.timeout = timeout
        self.session = 0
        self.alive_interval = 20
        self.seq = 0
        self.sock: socket.socket | None = None
        self.lock = threading.RLock()
        self._buf = bytearray()
        self._stop = threading.Event()
        self._keeper: threading.Thread | None = None
        self._rxbuf = bytearray()
        self._defer_keepalive = defer_keepalive
        self._ka_stop = threading.Event()
        self._preview_open = False
        self._stream_keeper: "DvripConnection | None" = None
        self._stream_keeper_thread: threading.Thread | None = None

    # ---------------------------------------------------------------- socket

    def connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock
        self._rxbuf.clear()
        self.seq = 0
        self.session = 0
        self._preview_open = False

    def close(self) -> None:
        self._stop.set()
        self._ka_stop.set()
        self._stop_own_keepalive()
        self._close_stream_keeper()
        sock, self.sock = self.sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    def _recv_exact(self, n: int, deadline: float | None = None) -> bytes:
        sock = self.sock
        if sock is None:
            raise DvripError("not connected")
        while len(self._rxbuf) < n:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("socket read timed out")
                sock.settimeout(remaining)
            chunk = sock.recv(max(4096, n - len(self._rxbuf)))
            if not chunk:
                raise ConnectionError("device closed the connection")
            self._rxbuf.extend(chunk)
        out = bytes(self._rxbuf[:n])
        del self._rxbuf[:n]
        return out

    # ----------------------------------------------------------------- auth

    def login(self) -> bool:
        payload = {
            "EncryptType": "MD5",
            "LoginType": "DVRIP-Web",
            "PassWord": sofia_hash(self.password),
            "UserName": self.user,
        }
        reply = self.request(CMD_LOGIN, "Login", payload, deadline_extra=12.0)
        ret = reply.get("Ret")
        if ret == RET_OK:
            try:
                self.session = int(reply["SessionID"], 16)
            except (KeyError, ValueError):
                self.session = 0
            self.alive_interval = int(reply.get("AliveInterval", 20))
            self._start_keepalive()
            return True
        if ret in (104, 105):  # stale session still alive -> retry once
            reply = self.request(CMD_LOGIN, "Login", payload, deadline_extra=12.0)
            if reply.get("Ret") == RET_OK:
                self.session = int(reply["SessionID"], 16)
                self.alive_interval = int(reply.get("AliveInterval", 20))
                self._start_keepalive()
                return True
        raise DvripAuthError(
            f"login refused: {RET_CODES.get(ret, ret)}"
            if ret in RET_CODES else f"login refused: code {ret}"
        )

    def _start_keepalive(self) -> None:
        if self._defer_keepalive:
            return
        self._ka_stop.clear()
        self._keeper = threading.Thread(
            target=self._keepalive_loop, name=f"ka-{self.host}", daemon=True
        )
        self._keeper.start()

    def _stop_own_keepalive(self) -> None:
        """Hand the session heartbeat over to a dedicated connection."""
        self._ka_stop.set()
        thread, self._keeper = self._keeper, None
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.5)

    # ------------------------------------------------- keepalive during stream

    def _open_stream_keepalive(self) -> None:
        """Carry keepalives on a *second* connection while we stream.

        The preview connection is owned by the binary frame reader: the moment
        a control reply lands on that socket it gets parsed as a video frame
        header (JSON starts 0x7B, which is not a valid frame tag) and the
        stream dies. So during streaming the session heartbeat moves to a
        connection of its own.
        """
        if self._stream_keeper is not None:
            return
        peer = DvripConnection(self.host, self.user, self.password, self.port,
                               timeout=self.timeout, defer_keepalive=True)
        peer.connect()
        peer.login()
        self._stream_keeper = peer
        self._stream_keeper_thread = threading.Thread(
            target=self._stream_keepalive_loop, name=f"ska-{self.host}", daemon=True
        )
        self._stream_keeper_thread.start()

    def _stream_keepalive_loop(self) -> None:
        peer = self._stream_keeper
        while not self._stop.wait(max(5, self.alive_interval - 2)):
            if peer is None:
                return
            try:
                peer.request(CMD_KEEPALIVE, "KeepAlive",
                             {"Interval": peer.alive_interval})
            except Exception as exc:
                log.debug("stream keepalive failed for %s: %s", self.host, exc)

    def _close_stream_keeper(self) -> None:
        peer, self._stream_keeper = self._stream_keeper, None
        thread, self._stream_keeper_thread = self._stream_keeper_thread, None
        if peer is not None:
            peer.close()
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _keepalive_loop(self) -> None:
        while not self._ka_stop.wait(max(5, self.alive_interval - 2)):
            try:
                with self.lock:
                    if self.sock is None:
                        return
                    self.request(CMD_KEEPALIVE, "KeepAlive",
                                 {"Interval": self.alive_interval})
            except Exception as exc:  # keepalive is best-effort
                log.debug("keepalive failed for %s: %s", self.host, exc)

    # -------------------------------------------------------------- request

    def request(self, msgid: int, name: str, payload: dict | None = None,
                wait_response: bool = True, deadline_extra: float = 0.0):
        """Send a control request and optionally read the reply."""
        with self.lock:
            sock = self.sock
            if sock is None:
                raise DvripError("not connected")
            body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            header = CTRL_HDR.pack(MAGIC, 0, self.session, self.seq, msgid,
                                   len(body) + 2)
            try:
                sock.sendall(header + body + b"\n\x00")
            except OSError as exc:
                raise DvripError(f"send failed: {exc}") from exc

            if not wait_response:
                return None

            deadline = time.monotonic() + self.timeout + deadline_extra
            raw_hdr = self._recv_exact(CTRL_HDR.size, deadline)
            _, _, _session, _, reply_id, length = CTRL_HDR.unpack(raw_hdr)
            payload_bytes = self._recv_exact(length, deadline)
            try:
                reply = json.loads(payload_bytes[:-2])
            except (ValueError, UnicodeDecodeError) as exc:
                raise DvripError(f"malformed reply to 0x{msgid:04X}: {exc}") from exc
            reply["_msgid"] = reply_id
            return reply

    def named(self, msgid: int, name: str, data: dict | None = None,
              code: int | None = None) -> dict:
        body = {
            "Name": name,
            "SessionID": f"0x{self.session:08X}",
            name: data or {},
        }
        return self.request(code or msgid, name, body)

    # ------------------------------------------------------------- commands

    def system_info(self) -> dict:
        reply = self.request(CMD_SYSTEMINFO, "SystemInfo")
        return reply.get("SystemInfo", {}) or {}

    def system_caps(self) -> dict:
        reply = self.request(CMD_SYSCAPS, "SystemFunction")
        return reply.get("SystemFunction", {}) or {}

    def encode_info(self) -> dict:
        reply = self.request(1412, "Simplify.Encode")
        return reply.get("Simplify.Encode", []) or []

    def snapshot(self, channel: int = 0, timeout: float = 12.0) -> bytes:
        """Return a JPEG still. Uses its own request/response on this link."""
        body = {
            "Name": "OPSNAP",
            "SessionID": f"0x{self.session:08X}",
            "OPSNAP": {"Channel": channel},
        }
        with self.lock:
            sock = self.sock
            if sock is None:
                raise DvripError("not connected")
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            header = CTRL_HDR.pack(MAGIC, 0, self.session, self.seq,
                                   CMD_SNAPSHOT, len(
                                       json.dumps(body).encode()) + 2)
            sock.sendall(header + json.dumps(body).encode() + b"\n\x00")
            payload, _meta = self._read_binary_frame(
                time.monotonic() + timeout
            )
            return payload

    def ptz(self, command: str, step: int = 5, preset: int = -1,
            channel: int = 0, pattern: str = "Start") -> dict:
        param = {
            "AUX": {"Number": 0, "Status": "On"},
            "Channel": channel,
            "MenuOpts": "Enter",
            "Pattern": pattern,
            "Preset": preset,
            "Step": step,
            "Tour": 1 if "Tour" in command else 0,
        }
        body = {
            "Name": "OPPTZControl",
            "SessionID": f"0x{self.session:08X}",
            "OPPTZControl": {"Command": command, "Parameter": param},
        }
        return self.request(CMD_PTZ, "OPPTZControl", body)

    # ------------------------------------------------------- binary framing

    def _read_binary_frame(self, deadline: float) -> tuple[bytes, dict]:
        """Read one logical binary frame, joining fragments.

        Returns ``(payload, meta)`` where payload has the per-frame header
        stripped, ready to be appended to an Annex-B elementary stream.
        """
        buf = bytearray()
        meta: dict = {}
        remaining = 0
        header_len = 0

        while True:
            raw = self._recv_exact(BIN_HDR.size, deadline)
            _, _, _, _seq, _total, _cur, _msgid, length = BIN_HDR.unpack(raw)
            packet = self._recv_exact(length, deadline)

            if remaining == 0:
                tag = struct.unpack(">I", packet[:4])[0]
                if tag == JPEG_MAGIC:
                    return packet, {"type": "jpeg"}
                if tag in (DT_P_FRAME2, DT_P_FRAME):
                    header_len = 16
                    media, fps, w, h, dt, length_field = struct.unpack(
                        "BBBBII", packet[4:header_len]
                    )
                    meta.update(
                        width=w * 8,
                        height=h * 8,
                        fps=fps,
                        datetime=_decode_datetime(dt),
                        frame="I" if tag == DT_P_FRAME2 else "P",
                    )
                    codec = CODEC_BY_MEDIA.get(media, "h264")
                    meta["codec"] = codec
                    if tag == DT_P_FRAME2:
                        meta["type"] = "jpeg" if codec == "jpeg" else "video"
                elif tag in (DT_AUDIO, DT_INFO):
                    header_len = 8
                    media, _rate, length_field = struct.unpack("BBH", packet[4:8])
                    meta["type"] = "audio" if tag == DT_AUDIO else "info"
                else:
                    raise DvripError(f"unknown frame tag 0x{tag:08X}")
                remaining = length_field
            else:
                header_len = 0

            chunk = packet[header_len:]
            buf.extend(chunk)
            remaining -= len(chunk)
            if remaining <= 0:
                return bytes(buf), meta

    # ----------------------------------------------------------- live preview

    def open_preview(self, channel: int = 0, stream_type: str = "Main",
                      audio: bool = False) -> None:
        """Claim and start the monitor. Blocks until the device accepts."""
        if self._preview_open:
            return
        self._stop_own_keepalive()
        self._open_stream_keepalive()
        params = {
            "Channel": channel,
            "CombinMode": "AudioOut" if audio else "NONE",
            "StreamType": stream_type,
            "TransMode": "TCP",
        }
        claim = self.named(CMD_MONITOR_CLAIM, "OPMonitor",
                           {"Action": "Claim", "Parameter": params})
        if claim.get("Ret") != RET_OK:
            self._close_stream_keeper()
            raise DvripError(
                f"preview claim refused: {RET_CODES.get(claim.get('Ret'), claim.get('Ret'))}"
            )
        self.named(CMD_MONITOR_CLAIM, "OPMonitor",
                   {"Action": "Start", "Parameter": params}, code=CMD_MONITOR_START)
        self._preview_open = True

    def iter_frames(self, channel: int = 0, stream_type: str = "Main",
                    idle_timeout: float = 30.0):
        """Yield ``(payload, meta)`` forever; re-raises transport errors."""
        self.open_preview(channel, stream_type)
        while True:
            try:
                yield self._read_binary_frame(time.monotonic() + idle_timeout)
            except TimeoutError:
                # No frame within idle_timeout: keep waiting rather than
                # tearing down a healthy-but-quiet link.
                log.warning("preview idle for %ss on %s", idle_timeout, self.host)
                yield b"", {"type": "idle"}