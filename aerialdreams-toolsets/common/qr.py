"""QR PNG generation with zero third-party dependencies.

The encoder is Project Nayuki's ``qrcodegen.py`` (MIT, vendored next to this
file); the PNG writer below is stdlib ``zlib`` + ``struct`` only, so the QR
works even in an isolated interpreter (``python -I``) where Pillow/qrcode are
not importable.
"""

from __future__ import annotations

import struct
import zlib

from . import qrcodegen


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + kind + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))


def write_qr_png(text: str, path: str, scale: int = 8, border: int = 4,
                 ecl=None) -> str:
    """Render ``text`` as a black-on-white QR PNG at ``path``."""
    ecc = ecl or qrcodegen.QrCode.Ecc.MEDIUM
    qr = qrcodegen.QrCode.encode_text(text, ecc)
    size = qr.get_size()
    dim = (size + border * 2) * scale
    raw = bytearray()
    for y in range(dim):
        raw.append(0)  # PNG filter: none
        my = y // scale - border
        inside_y = 0 <= my < size
        for x in range(dim):
            mx = x // scale - border
            dark = inside_y and 0 <= mx < size and qr.get_module(mx, my)
            raw.append(0 if dark else 255)
    ihdr = struct.pack(">IIBBBBB", dim, dim, 8, 0, 0, 0, 0)  # 8-bit grayscale
    png = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr)
           + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + _png_chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)
    return path