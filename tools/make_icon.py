#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Рисует иконку приложения: assets/AppIcon.icns + assets/icon_256.png."""

import math
import os
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "assets")
S = 1024

BG_TOP = (24, 34, 48)
BG_BOTTOM = (9, 13, 19)
GREEN = (61, 220, 151)
AMBER = (255, 176, 32)


def rounded_mask(size: int, radius: int) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def main() -> None:
    os.makedirs(OUT, exist_ok=True)

    # фон с вертикальным градиентом внутри скруглённого квадрата
    base = Image.new("RGB", (S, S))
    px = base.load()
    for y in range(S):
        t = y / (S - 1)
        col = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        for x in range(S):
            px[x, y] = col

    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    img.paste(base, (0, 0), rounded_mask(S, 230))

    d = ImageDraw.Draw(img)

    # дуга-гейдж
    box = [150, 150, 874, 874]
    d.arc(box, start=150, end=390, fill=GREEN, width=62)
    for ang in (150, 390):
        cx = (box[0] + box[2]) / 2 + (box[2] - box[0]) / 2 * math.cos(math.radians(ang))
        cy = (box[1] + box[3]) / 2 + (box[3] - box[1]) / 2 * math.sin(math.radians(ang))
        d.ellipse([cx - 31, cy - 31, cx + 31, cy + 31], fill=GREEN)

    # молния
    bolt = [(566, 266), (384, 572), (497, 572), (442, 796), (644, 458), (513, 458)]
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x + 6, y + 14) for x, y in bolt], fill=(0, 0, 0, 150))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(20)))
    d.polygon(bolt, fill=(238, 247, 255, 255))

    icon = img.resize((S, S), Image.LANCZOS)
    icon.save(os.path.join(OUT, "icon_1024.png"))
    icon.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "icon_256.png"))

    # .icns
    iconset = os.path.join(OUT, "AppIcon.iconset")
    shutil.rmtree(iconset, ignore_errors=True)
    os.makedirs(iconset)
    for size in (16, 32, 64, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{size}x{size}.png"))
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(
            os.path.join(iconset, f"icon_{size}x{size}@2x.png"))

    subprocess.run(["iconutil", "-c", "icns", iconset,
                    "-o", os.path.join(OUT, "AppIcon.icns")], check=True)
    shutil.rmtree(iconset, ignore_errors=True)
    print("✓ assets/AppIcon.icns")


if __name__ == "__main__":
    main()
