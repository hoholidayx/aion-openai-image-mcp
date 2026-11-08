"""Bounded PNG inputs and outputs shared by the MCP layer and adapters."""

import base64
from io import BytesIO
import struct
import zlib

from PIL import Image

MAX_REFERENCE = 20 * 1024**2
MAX_REFERENCES_TOTAL = 32 * 1024**2
MAX_RESULT = 32 * 1024**2
MAX_REQUEST_BODY = 48 * 1024**2
PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"


def png_dimensions(data: bytes, limit: int = MAX_RESULT) -> tuple[int, int]:
    if not isinstance(data, bytes) or len(data) > limit or not data.startswith(PNG_HEADER):
        raise ValueError("Expected PNG within the byte limit")
    view = memoryview(data)
    offset = 8
    has_data = False
    while offset + 12 <= len(data):
        length, kind = struct.unpack_from(">I4s", view, offset)
        end = offset + 8 + length
        if end + 4 > len(data) or zlib.crc32(view[offset + 4:end]) != struct.unpack_from(">I", view, end)[0]:
            raise ValueError("PNG has a damaged chunk")
        offset = end + 4
        if kind in {b"acTL", b"fdAT", b"fcTL"}:
            raise ValueError("Animated PNG is unsupported")
        if kind == b"IDAT" and length:
            has_data = True
        if kind == b"IEND":
            if not has_data or length or offset != len(data):
                raise ValueError("Invalid PNG final chunk")
            break
    else:
        raise ValueError("PNG is missing its final chunk")
    try:
        with Image.open(BytesIO(data), formats=["PNG"]) as image:
            width, height = image.size
            if not (0 < width <= 8192 and 0 < height <= 8192 and width * height <= 32_000_000):
                raise ValueError("PNG dimensions exceed the limit")
            image.load()
    except (OSError, SyntaxError, EOFError, struct.error, zlib.error, Image.DecompressionBombError) as exc:
        raise ValueError("Invalid PNG") from exc
    return width, height


def decode_base64(encoded: str, limit: int) -> bytes:
    if len(encoded) > 4 * ((limit + 2) // 3):
        raise ValueError("Base64 payload exceeds byte limit")
    data = base64.b64decode(encoded, validate=True)
    if len(data) > limit:
        raise ValueError("Decoded payload exceeds byte limit")
    return data


def validate_references(images: list[str]) -> None:
    remaining = MAX_REFERENCES_TOTAL
    for encoded in images:
        limit = min(remaining, MAX_REFERENCE)
        data = decode_base64(encoded, limit)
        png_dimensions(data, limit)
        remaining -= len(data)
