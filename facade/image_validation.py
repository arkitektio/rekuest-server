"""The rules an image asset is held to: a pure function over the file's bytes.

Only the header is read. An image is a PNG, a JPEG or a WebP; nothing else is one, an
SVG least of all, since it is a document that can carry scripts.
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass

IMAGE_FORMATS = ("png", "jpeg", "webp")


@dataclass(frozen=True)
class ImagePolicy:
    """The limits an image is held to when it is created."""

    max_size: int = 10 * 1024 * 1024
    max_side: int = 8192


class ImageRefused(ValueError):
    """An image that may not enter the store, with the code clients switch on."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImageReport:
    """What validation learned about an accepted image."""

    ref: str
    size: int
    format: str
    width: int
    height: int


def image_header(data: bytes) -> tuple[str, int, int] | None:
    """Format, width and height from a PNG, JPEG or WebP header; ``None`` for anything else."""
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        offset = 2
        while offset + 9 < len(data):
            if data[offset] != 0xFF:
                return None
            marker = data[offset + 1]
            (length,) = struct.unpack(">H", data[offset + 2 : offset + 4])
            if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                height, width = struct.unpack(">HH", data[offset + 5 : offset + 9])
                return "jpeg", width, height
            offset += 2 + length
        return None
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        kind = data[12:16]
        if kind == b"VP8X":
            return "webp", 1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little")
        if kind == b"VP8 ":
            width, height = struct.unpack("<HH", data[26:30])
            return "webp", width & 0x3FFF, height & 0x3FFF
        if kind == b"VP8L":
            (bits,) = struct.unpack("<I", data[21:25])
            return "webp", 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def validate_image(data: bytes, ref: str, policy: ImagePolicy | None = None) -> ImageReport:
    """Check ``data`` against ``ref`` and ``policy``; return what it is or raise ``ImageRefused``."""
    policy = policy or ImagePolicy()
    if len(data) > policy.max_size:
        raise ImageRefused("too_large", f"the image is {len(data)} bytes, over the limit of {policy.max_size}")
    actual = "sha256:" + hashlib.sha256(data).hexdigest()
    if actual != ref:
        raise ImageRefused("hash_mismatch", f"the uploaded bytes hash to {actual}, not {ref}")
    header = image_header(data)
    if header is None:
        raise ImageRefused("bad_header", f"an image is a PNG, a JPEG or a WebP file ({', '.join(IMAGE_FORMATS)})")
    format, width, height = header
    if not (0 < width <= policy.max_side and 0 < height <= policy.max_side):
        raise ImageRefused("over_budget", f"the image is {width}x{height}, over the limit of {policy.max_side} a side")
    return ImageReport(ref=ref, size=len(data), format=format, width=width, height=height)
