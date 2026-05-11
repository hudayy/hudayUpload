"""Convert assets/icon.png to assets/icon.ico for Inno Setup."""
from pathlib import Path
from PIL import Image

src = Path("assets/icon.png")
dst = Path("assets/icon.ico")

img = Image.open(src).convert("RGBA")
sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
img.save(dst, format="ICO", sizes=sizes)
print(f"Created {dst}")
