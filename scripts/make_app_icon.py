"""Generate the packaged ledger icon as a 32-bit ICO without an imaging dependency."""

import struct
import zlib
from pathlib import Path


def make_icon() -> bytes:
    size = 32
    pixels = bytearray()
    for y in reversed(range(size)):
        for x in range(size):
            color = (32, 38, 48, 255)
            if 6 <= x < 12 and 18 <= y < 26:
                color = (53, 167, 139, 255)
            elif 13 <= x < 19 and 12 <= y < 26:
                color = (74, 143, 223, 255)
            elif 20 <= x < 26 and 6 <= y < 26:
                color = (236, 187, 67, 255)
            pixels.extend((color[2], color[1], color[0], color[3]))
    bitmap = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, len(pixels), 0, 0, 0, 0)
    bitmap += pixels + bytes(size * 4)
    return (
        struct.pack("<HHH", 0, 1, 1)
        + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(bitmap), 22)
        + bitmap
    )


def make_png(size: int = 256) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = bytearray()
    for y in range(size):
        pixels.append(0)
        for x in range(size):
            column, row = x * 32 // size, y * 32 // size
            color = (32, 38, 48, 255)
            if 6 <= column < 12 and 18 <= row < 26:
                color = (53, 167, 139, 255)
            elif 13 <= column < 19 and 12 <= row < 26:
                color = (74, 143, 223, 255)
            elif 20 <= column < 26 and 6 <= row < 26:
                color = (236, 187, 67, 255)
            pixels.extend(color)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(pixels)))
        + chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "backend/src/htd/adapters/desktop/resources/app.ico"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(make_icon())
    source = Path(__file__).resolve().parents[1] / "packaging/app.png"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(make_png())
