"""Render the Energy Control brand icons.

Usage: python scripts/make_icon.py custom_components/energy_control/brand  (needs Pillow)
"""

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 2048  # supersampled canvas


def bolt(cx: float, cy: float, k: float) -> list[tuple[float, float]]:
    pts = [(70, -330), (-150, 40), (-10, 40), (-70, 330), (150, -60), (10, -60), (90, -330)]
    return [(cx + x * k, cy + y * k) for x, y in pts]


def render(outline: tuple[int, int, int], out: Path, sizes: dict[str, int]) -> None:
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    stroke = 120
    # Battery body + terminal.
    body = (470, 330, 1578, 1900)
    d.rounded_rectangle(body, radius=210, outline=outline, width=stroke)
    d.rounded_rectangle((800, 150, 1248, 330 + stroke // 2), radius=70, fill=outline)
    # Charge levels: three bars, green to teal.
    inner_l, inner_r = body[0] + stroke + 70, body[2] - stroke - 70
    bars = [((1450, 1710), (46, 160, 110)), ((1170, 1410), (38, 166, 154)), ((890, 1130), (66, 133, 244))]
    for (top, bottom), color in bars:
        d.rounded_rectangle((inner_l, top, inner_r, bottom), radius=60, fill=(*color, 255))
    # Bolt with a transparent knockout gap around it.
    pts = bolt(1024, 1120, 1.72)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).polygon(pts, fill=255)
    gap = mask.filter(ImageFilter.MaxFilter(71))
    alpha = ImageChops.subtract(img.getchannel("A"), gap)
    img.putalpha(alpha)
    d = ImageDraw.Draw(img)
    d.polygon(pts, fill=(255, 179, 0, 255))
    # Trim to content, pad to square with a small margin.
    box = img.getbbox()
    img = img.crop(box)
    side = max(img.size)
    margin = int(side * 0.02)
    square = Image.new("RGBA", (side + 2 * margin, side + 2 * margin), (0, 0, 0, 0))
    square.paste(img, ((square.width - img.width) // 2, (square.height - img.height) // 2))
    for name, px in sizes.items():
        square.resize((px, px), Image.LANCZOS).save(out / name, optimize=True)


if __name__ == "__main__":
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    render((40, 52, 64, 255), out, {"icon.png": 256, "icon@2x.png": 512})
    render((226, 232, 240, 255), out, {"dark_icon.png": 256, "dark_icon@2x.png": 512})
