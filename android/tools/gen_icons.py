#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generates the Sentinel Live legacy launcher icons.

The adaptive icon (API 26+) is a vector in res/drawable; these PNGs cover
API 24-25, which have no adaptive-icon support at all.

    python3 tools/gen_icons.py
"""
import os
from PIL import Image, ImageDraw

RES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "app", "res")

BG = (8, 11, 16)
GREEN = (52, 211, 153)
BLUE = (14, 165, 233)

LEGACY = {"mdpi": 48, "hdpi": 72, "xhdpi": 96, "xxhdpi": 144, "xxxhdpi": 192}
SS = 4  # supersample for smooth edges


def draw_icon(size):
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Rounded square plate.
    radius = int(s * 0.22)
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=radius, fill=BG + (255,))

    # Camera body.
    bx0, by0 = s * 0.20, s * 0.38
    bx1, by1 = s * 0.80, s * 0.68
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=int(s * 0.05), fill=GREEN + (255,))
    # Viewfinder hump.
    d.rounded_rectangle([s * 0.36, s * 0.28, s * 0.64, s * 0.40],
                        radius=int(s * 0.02), fill=GREEN + (255,))
    # Lens.
    cx, cy, r = s * 0.50, s * 0.53, s * 0.105
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=BLUE + (255,))
    d.ellipse([cx - r * 0.45, cy - r * 0.45, cx + r * 0.45, cy + r * 0.45],
              fill=BG + (255,))
    # Status bar, echoing the live indicator in the app.
    d.rounded_rectangle([s * 0.20, s * 0.76, s * 0.80, s * 0.81],
                        radius=int(s * 0.025), fill=GREEN + (140,))

    return img.resize((size, size), Image.LANCZOS)


def main():
    for bucket, size in LEGACY.items():
        out = os.path.join(RES, f"mipmap-{bucket}")
        os.makedirs(out, exist_ok=True)
        path = os.path.join(out, "ic_launcher.png")
        draw_icon(size).save(path)
        print(f"  {path} ({size}x{size})")


if __name__ == "__main__":
    main()