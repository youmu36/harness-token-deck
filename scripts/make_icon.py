"""Resize the ledger PNG into a multi-resolution Windows ICO (16..256 pixels)."""

import argparse
import importlib
import struct
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SIZES = (16, 24, 32, 48, 64, 128, 256)


def make_icon(source: Path, destination: Path) -> None:
    clr: Any = importlib.import_module("clr")
    clr.AddReference("System.Drawing")
    drawing: Any = importlib.import_module("System.Drawing")
    image: Any = drawing.Bitmap(str(source.resolve()))
    frames: list[bytes] = []
    try:
        for size in SIZES:
            bitmap: Any = drawing.Bitmap(image, size, size)
            try:
                pixels = bytearray()
                for y in reversed(range(size)):
                    for x in range(size):
                        color = bitmap.GetPixel(x, y)
                        pixels.extend((int(color.B), int(color.G), int(color.R), int(color.A)))
                header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
                frames.append(header + pixels + bytes(((size + 31) // 32) * 4 * size))
            finally:
                bitmap.Dispose()
    finally:
        image.Dispose()
    entries = bytearray()
    offset = 6 + 16 * len(frames)
    for size, frame in zip(SIZES, frames, strict=True):
        entries.extend(struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(frame), offset))
        offset += len(frame)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + entries + b"".join(frames))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "packaging/app.png")
    parser.add_argument("--output", type=Path, default=ROOT / "packaging/app.ico")
    args = parser.parse_args()
    make_icon(args.source, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
