#!/usr/bin/env python3
"""Generate icon files for the Chrome extension.

Produces:
  - chrome_extension/icon-16.png
  - chrome_extension/icon-32.png
  - chrome_extension/icon-48.png
  - chrome_extension/icon-96.png
  - chrome_extension/icon-128.png  (also used as the Chrome Web Store store icon)

Matches the existing Twitch-purple ring with a white center dot.
"""

from pathlib import Path
from PIL import Image, ImageDraw


OUT_DIR = Path(__file__).resolve().parent / "chrome_extension"
SIZES = [16, 32, 48, 96, 128]
PURPLE = "#9146FF"


def draw_icon(size: int) -> Image.Image:
    # Render at 4x and downsample for smooth anti-aliasing at small sizes.
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Outer filled circle (Twitch purple)
    margin = max(1, s // 12)
    draw.ellipse(
        [margin, margin, s - margin, s - margin],
        fill=PURPLE,
    )

    # Inner white "hole" — produces the ring look
    center = s // 2
    hole_radius = s // 5
    draw.ellipse(
        [
            center - hole_radius,
            center - hole_radius,
            center + hole_radius,
            center + hole_radius,
        ],
        fill=(255, 255, 255, 255),
    )

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for size in SIZES:
        img = draw_icon(size)
        out = OUT_DIR / f"icon-{size}.png"
        img.save(out, "PNG", optimize=True)
        print(f"Created {out} ({size}x{size})")


if __name__ == "__main__":
    main()
