"""A DVRIP-speaking NVR simulator for development and testing.

Implements the server side of the Xiongmai protocol so the whole pipeline -
client framing, websocket fan-out, WebCodecs decode - can be exercised
without hardware. Serves the synthetic clip produced by ``make_testclip.py``
and JPEG stills rendered with Pillow.

    python3 tools/mock_nvr.py --port 34567 --clip /tmp/opencode/testclip.h264
"""

from __future__ import annotations

import argparse
import json
import socket
import socketserver
import struct
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image, ImageDraw  # noqa: E402

from backend import codec  # noqa: E402
from backend.dvrip_client import sofia_hash  # noqa: E402

MAGIC = 0xFF
CTRL_HDR = struct.Struct("BB2xII2xHI")
BIN_HDR = struct.Struct("BB2xIIBBHI")

DT_P_FRAME = 0x1FD
DT_P_FRAME2 = 0x1FC
JPEG_TAG = 0xFFD8FFE0
MEDIA_H264 = 2
MAX_BINARY_PAYLOAD = 1400


def pack_datetime(dt: datetime) -> int:
    return ((dt.year - 2000) << 26) | (dt.month << 22) | (dt.day << 17) \
        | (dt.hour << 12) | (dt.minute << 6) | dt.second


class Session:
    def __init__(self):
        self.logged_in = False
        self.session = 0
        self.seq = 0
        self.monitoring = False
        self.ptz_log: list[str] = []


class MockNVRHandler(socketserver.BaseRequestHandler):
    server: "MockNVRServer"

    def handle(self) -> None:
        self.buf = bytearray()
        self.state = Session()
        self.request.settimeout(30)
        peer = self.client_address
        self.log(f"connection from {peer[0]}:{peer[1]}")
        try:
            while True:
                data = self.request.recv(65536)
                if not data:
                    break
                self.buf.extend(data)
                self._drain()
        except (OSError, ConnectionError, socket.timeout):
            pass
        finally:
            self.state.monitoring = False
            self.log("disconnected")

    def log(self, message: str) -> None:
        if self.server.verbose:
            print(f"[nvr] {message}", flush=True)

    # ------------------------------------------------------------------- io

    def _recv_exact(self, n: int) -> bytes | None:
        while len(self.buf) < n:
            try:
                chunk = self.request.recv(max(4096, n - len(self.buf)))
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf.extend(chunk)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def _drain(self) -> None:
        while len(self.buf) >= CTRL_HDR.size:
            header = bytes(self.buf[:CTRL_HDR.size])
            _, _, session, _seq, msgid, length = CTRL_HDR.unpack(header)
            if len(self.buf) < CTRL_HDR.size + length:
                return
            payload = bytes(self.buf[CTRL_HDR.size:CTRL_HDR.size + length])
            del self.buf[:CTRL_HDR.size + length]

            self.state.session = session
            try:
                body = json.loads(payload[:-2])
            except (ValueError, UnicodeDecodeError):
                body = {}
            self.dispatch(msgid, body)
            if self.state.monitoring:
                return          # streaming thread owns the socket now

    def send_json(self, msgid: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.state.seq = (self.state.seq + 1) & 0xFFFFFFFF
        header = CTRL_HDR.pack(MAGIC, 0, self.state.session, self.state.seq,
                               msgid, len(body) + 2)
        self.request.sendall(header + body + b"\n\x00")

    def reply(self, msgid: int, payload: dict) -> None:
        self.send_json(msgid, payload)

    def ok(self, msgid: int, name: str, extra: dict | None = None) -> None:
        body = {"Name": name, "Ret": 100,
                "SessionID": f"0x{self.state.session:08X}", **(extra or {})}
        self.send_json(msgid, body)

    # -------------------------------------------------------------- dispatch

    def dispatch(self, msgid: int, body: dict) -> None:
        name = body.get("Name", "")
        self.log(f"<- 0x{msgid:04X} {name or '?'}")

        if msgid == 1000:                                   # login
            expected = sofia_hash(self.server.password)
            if body.get("PassWord") != expected:
                self.reply(1000, {"Name": "Login", "Ret": 106})
                return
            self.state.logged_in = True
            self.send_json(1000, {
                "Name": "Login", "Ret": 100,
                "SessionID": f"0x{self.state.session:08X}",
                "AliveInterval": 20,
                "RandVal": "abcdef1234567890",
                "SoftVersion": "5.00.R02.000529B2.10010.040600.0020000",
            })
            return

        if msgid == 1006:                                   # keepalive
            self.ok(1006, "KeepAlive", {"Interval": 20})
            return

        if msgid == 1020:                                   # system info
            self.send_json(1020, {
                "Name": "SystemInfo", "Ret": 100,
                "SessionID": f"0x{self.state.session:08X}",
                "SystemInfo": {
                    "DeviceModel": "HI3516EV300_85H50AI",
                    "HardWare": "HI3516EV300_85H50AI",
                    "SerialNo": "a166379674a3b447",
                    "VideoInChannel": 4,
                    "VideoOutChannel": 1,
                    "AlarmInChannel": 2,
                    "AlarmOutChannel": 1,
                    "SoftWareVersion": "5.00.R02.000529B2.10010.040600.0020000",
                    "DeviceRunTime": "0x0001f532",
                },
            })
            return

        if msgid == 1412:                                   # encode info
            self.send_json(1412, {
                "Name": "Simplify.Encode", "Ret": 100,
                "SessionID": f"0x{self.state.session:08X}",
                "Simplify.Encode": [{
                    "MainFormat": {"VideoEnable": True, "AudioEnable": True,
                                   "Video": {"Compression": "H.264", "Resolution": "1080P",
                                             "FPS": 15, "BitRate": 1200,
                                             "BitRateControl": "VBR", "GOP": 30}},
                    "ExtraFormat": {"VideoEnable": True, "AudioEnable": False,
                                    "Video": {"Compression": "H.264", "Resolution": "D1",
                                              "FPS": 15, "BitRate": 512,
                                              "BitRateControl": "VBR", "GOP": 30}},
                }],
            })
            return

        if msgid == 1360:                                   # capabilities
            self.send_json(1360, {
                "Name": "SystemFunction", "Ret": 100,
                "SessionID": f"0x{self.state.session:08X}",
                "SystemFunction": {"PreviewFunction": {"Talk": True, "Tour": True}},
            })
            return

        if msgid == 1413:                                   # monitor claim
            self.ok(1413, "OPMonitor")
            return

        if msgid == 1410:                                   # monitor start
            self.ok(1410, "OPMonitor")
            if self.state.monitoring:
                self.log("monitor already running; ignoring duplicate start")
                return
            self.state.monitoring = True
            threading.Thread(target=self._stream, daemon=True).start()
            return

        if msgid == 1560:                                   # snapshot
            threading.Thread(target=self._send_snapshot, daemon=True).start()
            return

        if msgid == 1400:                                   # PTZ
            command = body.get("OPPTZControl", {})
            cmd = command.get("Command", "?")
            pattern = command.get("Parameter", {}).get("Pattern", "?")
            self.state.ptz_log.append(f"{cmd}:{pattern}")
            self.log(f"PTZ {cmd} {pattern}")
            self.ok(1400, "OPPTZControl")
            return

        self.reply(msgid, {"Name": name or "Unknown", "Ret": 101})

    # -------------------------------------------------------------- streaming

    def _binary_header(self, length: int, total: int, current: int) -> bytes:
        self.state.seq = (self.state.seq + 1) & 0xFFFFFFFF
        return BIN_HDR.pack(MAGIC, 0, self.state.session, self.state.seq,
                            total, current, 1411, length)

    def _stream(self) -> None:
        try:
            units = codec.to_access_units(self.server.clip_bytes)
            sets = codec.extract_parameter_sets(self.server.clip_bytes)
        except Exception as exc:  # noqa: BLE001
            self.log(f"stream setup failed: {exc!r}")
            return
        fps = int(self.server.fps)
        width = sets.width or 1280
        height = sets.height or 720
        interval = 1.0 / fps
        self.log(f"streaming {len(units)} AUs @ {fps}fps {width}x{height}")

        index = 0
        sent = 0
        while self.state.monitoring:
            unit = units[index % len(units)]
            index += 1
            key = codec.contains_keyframe(unit)
            tag = DT_P_FRAME2 if key else DT_P_FRAME
            stamp = pack_datetime(datetime.now())
            # Wire format: the data_type tag is big-endian; the remaining
            # frame-header fields are native (little-endian) packed, matching
            # the reference implementation and real Xiongmai firmware.
            head = struct.pack(">I", tag) + struct.pack(
                "BBBBII", MEDIA_H264, fps, width // 8, height // 8,
                stamp, len(unit)
            )

            chunks = [unit[i:i + MAX_BINARY_PAYLOAD]
                      for i in range(0, len(unit), MAX_BINARY_PAYLOAD)] or [b""]
            try:
                for index_of, chunk in enumerate(chunks):
                    first = index_of == 0
                    payload = (head + chunk) if first else chunk
                    self.request.sendall(
                        self._binary_header(len(payload), len(chunks), index_of)
                        + payload
                    )
            except OSError as exc:
                self.log(f"send stopped: {exc}")
                return
            sent += 1
            if sent == 1:
                self.log(f"first AU sent: {len(unit)} bytes in {len(chunks)} packet(s)")
            elif sent % 30 == 0:
                self.log(f"...{sent} frames sent, last AU {len(unit)}B/{len(chunks)}pk")
            time.sleep(interval)

    def _send_snapshot(self) -> None:
        try:
            # Advance the synthetic scene so successive stills differ, which is
            # what motion detection keys on.
            self.server.frame_index += 1
            jpeg = render_still(self.server.frame_index)
        except Exception as exc:  # noqa: BLE001
            self.log(f"snapshot failed: {exc}")
            return
        try:
            self.request.sendall(self._binary_header(len(jpeg), 1, 0) + jpeg)
        except OSError:
            pass

    def send_raw_snapshot(self, jpeg: bytes) -> None:
        self.request.sendall(self._binary_header(len(jpeg), 1, 0) + jpeg)


def render_still(index: int, size: tuple[int, int] = (640, 360)) -> bytes:
    import io
    width, height = size
    img = Image.new("RGB", size, (12, 16, 22))
    draw = ImageDraw.Draw(img)
    bar = 8
    for i in range(bar):
        shade = 28 + i * 9
        x0 = i * width // bar
        x1 = (i + 1) * width // bar
        draw.rectangle([x0, 0, x1, height], fill=(shade, shade // 2 + 20, 255 - shade))
    import math
    # Sweep quickly enough that consecutive stills differ measurably, which is
    # what the motion detector keys on.
    phase = index * 0.35
    cx = int((0.5 + 0.34 * math.sin(phase)) * width)
    cy = int(height * (0.5 + 0.22 * math.sin(phase * 1.7)))
    draw.rectangle([cx - 70, cy - 70, cx + 70, cy + 70], fill=(40, 235, 120))
    draw.rectangle([10, 10, 120, 34], fill=(10, 10, 14))
    draw.text((16, 16), f"frame {index}", fill=(120, 240, 180))
    draw.text((10, height - 22), datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              fill=(200, 220, 240))
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=82)
    return buffer.getvalue()


class MockNVRServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address, clip_path: Path, fps: float, password: str,
                 verbose: bool):
        super().__init__(address, MockNVRHandler)
        self.clip_bytes = clip_path.read_bytes()
        self.fps = fps
        self.password = password
        self.verbose = verbose
        self.frame_index = 0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=34567)
    parser.add_argument("--clip", default="/tmp/opencode/testclip.h264")
    parser.add_argument("--fps", type=float, default=15.0)
    parser.add_argument("--password", default="mockpass")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    clip = Path(args.clip)
    if not clip.exists():
        sys.exit(f"clip not found: {clip} (run tools/make_testclip.py first)")

    server = MockNVRServer((args.host, args.port), clip, args.fps,
                           args.password, args.verbose)
    print(f"mock NVR listening on {args.host}:{args.port} "
          f"({clip.stat().st_size} byte clip @ {args.fps}fps)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        server.shutdown()


if __name__ == "__main__":
    main()