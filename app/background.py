"""The dashboard's background picture (Settings → Display → Background).

One picture at a time, stored as ``data/background.<ext>`` with its original bytes.
Uploads are checked from the bytes themselves, not the file name or the browser's
claimed type: only JPEG, PNG or WebP, at most ``MAX_BYTES``, with sane dimensions.
No image library is needed: the format and size are read from the file header.
"""

from __future__ import annotations

import struct
from pathlib import Path

from .storage import make_private

MAX_BYTES = 12 * 1024 * 1024
MIN_SIDE, MAX_SIDE = 320, 12000
TYPES = {"jpeg": ("image/jpeg", "jpg"), "png": ("image/png", "png"), "webp": ("image/webp", "webp")}


class BackgroundError(ValueError):
    pass


def _jpeg_size(b: bytes) -> tuple[int, int] | None:
    i = 2
    while i + 9 < len(b):
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        length = struct.unpack(">H", b[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            h, w = struct.unpack(">HH", b[i + 5:i + 9])
            return w, h
        i += 2 + length
    return None


def _webp_size(b: bytes) -> tuple[int, int] | None:
    chunk = b[12:16]
    if chunk == b"VP8 " and len(b) >= 30:
        w, h = struct.unpack("<HH", b[26:30])
        return w & 0x3FFF, h & 0x3FFF
    if chunk == b"VP8L" and len(b) >= 25:
        bits = int.from_bytes(b[21:25], "little")
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(b) >= 30:
        return int.from_bytes(b[24:27], "little") + 1, int.from_bytes(b[27:30], "little") + 1
    return None


def inspect(data: bytes) -> tuple[str, int, int]:
    """(kind, width, height) of an uploaded picture, or BackgroundError saying why not."""
    if len(data) > MAX_BYTES:
        raise BackgroundError(f"The picture is too large (at most {MAX_BYTES // (1024 * 1024)} MB).")
    if data[:3] == b"\xff\xd8\xff":
        kind, size = "jpeg", _jpeg_size(data)
    elif data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        kind, size = "png", struct.unpack(">II", data[16:24])
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind, size = "webp", _webp_size(data)
    else:
        raise BackgroundError("Use a JPEG, PNG or WebP picture.")
    if not size:
        raise BackgroundError("That picture couldn't be read.")
    w, h = size
    if min(w, h) < MIN_SIDE or max(w, h) > MAX_SIDE:
        raise BackgroundError(f"The picture should be between {MIN_SIDE} and {MAX_SIDE} pixels on each side (it is {w}×{h}).")
    return kind, w, h


class BackgroundStore:
    def __init__(self, data_dir: Path) -> None:
        self.dir = data_dir

    def current(self) -> tuple[Path, str] | None:
        for kind, (mime, ext) in TYPES.items():
            p = self.dir / f"background.{ext}"
            if p.exists():
                return p, mime
        return None

    def save(self, data: bytes) -> dict:
        kind, w, h = inspect(data)
        mime, ext = TYPES[kind]
        tmp = self.dir / f".background.{ext}.tmp"
        tmp.write_bytes(data)
        make_private(tmp)
        self.remove()
        tmp.replace(self.dir / f"background.{ext}")
        return {"type": mime, "width": w, "height": h, "bytes": len(data)}

    def remove(self) -> bool:
        gone = False
        for _, ext in TYPES.values():
            p = self.dir / f"background.{ext}"
            if p.exists():
                p.unlink()
                gone = True
        return gone
