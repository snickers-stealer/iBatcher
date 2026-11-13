"""Generate the Windows icon from the browser logo."""

from __future__ import annotations

import io
from pathlib import Path

import cairosvg
from PIL import Image


ROOT = Path(__file__).parent
SOURCE = ROOT / "static" / "logo.svg"
TARGET = ROOT / "iBatcher.ico"


def main() -> None:
    png = cairosvg.svg2png(url=str(SOURCE), output_width=256, output_height=256)
    image = Image.open(io.BytesIO(png)).convert("RGBA")
    image.save(TARGET, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print(TARGET)


if __name__ == "__main__":
    main()
