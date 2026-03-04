#!/usr/bin/env python3
"""Generate icon files for the application."""

from PIL import Image, ImageDraw

def create_icon():
    """Create a simple Twitch-purple colored icon."""
    sizes = [16, 32, 48, 64, 128, 256]
    images = []
    
    for size in sizes:
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        
        # Twitch purple background circle
        margin = size // 8
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill="#9146FF"
        )
        
        # White "play" triangle or dot in center
        center = size // 2
        dot_size = size // 6
        draw.ellipse(
            [center - dot_size, center - dot_size, center + dot_size, center + dot_size],
            fill="white"
        )
        
        images.append(img)
    
    # Save as ICO
    images[0].save(
        "icon.ico",
        format="ICO",
        sizes=[(s, s) for s in sizes],
        append_images=images[1:]
    )
    print("Created icon.ico")


if __name__ == "__main__":
    create_icon()
