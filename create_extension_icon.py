#!/usr/bin/env python3
"""Generate icon for the Firefox extension."""

from PIL import Image, ImageDraw

def create_extension_icon():
    """Create a simple Twitch-purple colored icon."""
    size = 96
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Twitch purple background circle
    margin = 4
    draw.ellipse(
        [margin, margin, size - margin, size - margin],
        fill="#9146FF"
    )
    
    # White dot in center
    center = size // 2
    dot_size = size // 6
    draw.ellipse(
        [center - dot_size, center - dot_size, center + dot_size, center + dot_size],
        fill="white"
    )
    
    # Save as PNG
    img.save("firefox_extension/icon.png")
    print("Created firefox_extension/icon.png")


if __name__ == "__main__":
    create_extension_icon()
