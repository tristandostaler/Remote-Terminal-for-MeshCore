"""Minimal PNG writer for decoded AEIC images.

The synthesis decoder hands back ``512 * 512 * 3`` bytes of packed 8-bit RGB.
Serving that raw would work, but a PNG is cacheable, renders in an ``<img>`` and
saves like any other picture -- and a truecolour, non-interlaced, single-``IDAT``
PNG is about forty lines of :mod:`zlib` and :mod:`struct`, which is a far better
trade than adding Pillow (and its wheels for five architectures) to the
dependency tree for one encode.

Only what is needed is implemented: 8-bit RGB, filter type 0, no ancillary
chunks. This is not a general PNG library and should not grow into one.
"""

from __future__ import annotations

import struct
import zlib

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_BIT_DEPTH = 8
_COLOR_TYPE_RGB = 2


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def encode_png(rgb: bytes, width: int, height: int, *, level: int = 6) -> bytes:
    """Wrap packed 8-bit RGB in a PNG container.

    ``rgb`` must be exactly ``width * height * 3`` bytes, row-major, top row
    first. ``level`` is the zlib compression level; 6 is zlib's default and the
    right trade here, since a neural-decoded photo has almost no exactly
    repeating runs for a higher level to find.
    """
    stride = width * 3
    if width <= 0 or height <= 0:
        raise ValueError(f"bad PNG dimensions {width}x{height}")
    if len(rgb) != stride * height:
        raise ValueError(
            f"expected {stride * height} bytes for {width}x{height} RGB, got {len(rgb)}"
        )

    header = struct.pack(">IIBBBBB", width, height, _BIT_DEPTH, _COLOR_TYPE_RGB, 0, 0, 0)
    # Filter type 0 ("None") prefixed to every scanline. Real filters would buy
    # a few percent on a photo and cost a per-pixel Python loop; not worth it.
    raw = bytearray()
    for row in range(height):
        raw.append(0)
        raw += rgb[row * stride : (row + 1) * stride]

    return (
        _SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), level))
        + _chunk(b"IEND", b"")
    )
