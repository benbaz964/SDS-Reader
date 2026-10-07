"""Generates build/icon.ico (and docs/assets/icon.png for the website) from
code, so the app's branding lives in version control without binary
hand-edits. Run: python build/make_icon.py"""

import os

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ACCENT = (15, 122, 92, 255)
DARK = (15, 26, 36, 255)


def draw(size: int) -> Image.Image:
    s = size * 4  # supersample for smooth edges
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = s * 0.22
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=r, fill=DARK)
    m = s * 0.18
    c = s / 2
    d.polygon([(c, m), (s - m, c), (c, s - m), (m, c)], fill=ACCENT)  # GHS-style diamond
    bar_w = s * 0.075
    d.rounded_rectangle([c - bar_w / 2, s * 0.33, c + bar_w / 2, s * 0.56], radius=bar_w / 2, fill="white")
    d.ellipse([c - bar_w * 0.62, s * 0.61, c + bar_w * 0.62, s * 0.61 + bar_w * 1.24], fill="white")
    return img.resize((size, size), Image.LANCZOS)


def main():
    sizes = [16, 24, 32, 48, 64, 128, 256]
    big = draw(256)
    big.save(os.path.join(HERE, "icon.ico"), sizes=[(n, n) for n in sizes])
    assets = os.path.join(ROOT, "docs", "assets")
    os.makedirs(assets, exist_ok=True)
    draw(512).save(os.path.join(assets, "icon.png"))
    draw(64).save(os.path.join(assets, "favicon.png"))
    print("icons written")


if __name__ == "__main__":
    main()
