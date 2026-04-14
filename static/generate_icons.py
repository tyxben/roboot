#!/usr/bin/env python3
"""Generate PWA icons for Roboot."""

from PIL import Image, ImageDraw, ImageFont
import os

def create_icon(size, output_path):
    """Create a simple icon with gradient background and text."""
    # Create image with gradient
    img = Image.new('RGB', (size, size))
    draw = ImageDraw.Draw(img)

    # Draw gradient background (accent color)
    for y in range(size):
        # From #e94560 to #533483
        r = int(233 - (233 - 83) * y / size)
        g = int(69 - (69 - 52) * y / size)
        b = int(96 - (96 - 131) * y / size)
        draw.line([(0, y), (size, y)], fill=(r, g, b))

    # Draw circle for robot icon
    padding = size // 6
    circle_size = size - 2 * padding
    draw.ellipse(
        [padding, padding, padding + circle_size, padding + circle_size],
        fill=(255, 255, 255, 200),
        outline=(255, 255, 255)
    )

    # Draw simple "A" for Ava/Agent
    try:
        font_size = size // 2
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", font_size)
        except:
            font = ImageFont.load_default()

        text = "A"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (size - text_width) // 2
        text_y = (size - text_height) // 2 - font_size // 8

        draw.text((text_x, text_y), text, fill=(83, 52, 131), font=font)
    except Exception as e:
        print(f"Font rendering failed: {e}")

    img.save(output_path, 'PNG')
    print(f"Created {output_path}")

if __name__ == '__main__':
    script_dir = os.path.dirname(os.path.abspath(__file__))
    create_icon(192, os.path.join(script_dir, 'icon-192.png'))
    create_icon(512, os.path.join(script_dir, 'icon-512.png'))
    print("Icons generated successfully!")
