"""Generate a synthetic H.264 Annex-B clip for testing.

Development only - the app itself never imports PyAV. Used by
``mock_nvr.py`` to produce a real decodable elementary stream so the whole
pipeline (DVRIP framing -> websocket -> WebCodecs) can be exercised without
hardware.
"""

from __future__ import annotations

import sys
from pathlib import Path

import av
import numpy as np

WIDTH, HEIGHT = 1280, 720
FPS = 15
SECONDS = 20


def _frame(index: int, total: int):
    """A moving box over a colour bar, with a burnt-in frame counter."""
    img = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    bar = 8
    for i in range(bar):
        shade = int(28 + i * 9)
        img[:, i * WIDTH // bar:(i + 1) * WIDTH // bar] = (shade, shade // 2 + 20, 255 - shade)

    phase = index / max(1, total)
    cx = int((0.12 + 0.76 * phase) * WIDTH)
    cy = int(HEIGHT * (0.5 + 0.26 * np.sin(phase * 6.28)))
    half = 70
    y0, y1 = max(0, cy - half), min(HEIGHT, cy + half)
    x0, x1 = max(0, cx - half), min(WIDTH, cx + half)
    img[y0:y1, x0:x1] = (40, 235, 120)

    img[10:46, 10:200] = (10, 10, 14)
    for row in range(36):
        for col in range(24):
            if (row // 2 + col // 2) % 2 == 0:
                img[12 + row:14 + row, 12 + col * 7:14 + col * 7] = (60, 220, 150)
    return av.VideoFrame.from_ndarray(img, format="rgb24")


def build(out_path: Path, total_frames: int | None = None, gop: int = 30,
          codec: str = "libx264") -> Path:
    total = total_frames or SECONDS * FPS
    out_path.parent.mkdir(parents=True, exist_ok=True)
    container = av.open(str(out_path), mode="w", format="h264")
    stream = container.add_stream(codec, rate=FPS)
    stream.width = WIDTH
    stream.height = HEIGHT
    stream.pix_fmt = "yuv420p"
    stream.gop_size = gop
    stream.bit_rate = 1_200_000
    stream.options = {"preset": "ultrafast", "tune": "zerolatency"}

    written = 0
    for index in range(total):
        frame = _frame(index, total)
        for packet in stream.encode(frame):
            out_path.open("ab").write(bytes(packet))
            written += packet.size
        if index % 30 == 0:
            print(f"  frame {index}/{total}", flush=True)

    for packet in stream.encode():
        out_path.open("ab").write(bytes(packet))
        written += packet.size
    container.close()
    print(f"wrote {out_path} ({written} bytes, {total} frames @ {FPS}fps)")
    return out_path


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/opencode/testclip.h264")
    frames = int(sys.argv[2]) if len(sys.argv) > 2 else SECONDS * FPS
    if target.exists() and target.stat().st_size > 0:
        print(f"{target} already exists ({target.stat().st_size} bytes)")
    else:
        build(target, frames)