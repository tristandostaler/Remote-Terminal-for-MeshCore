"""Turning an arbitrary image into what the AEIC encoder wants.

The encoder takes exactly ``512 * 512 * 3`` bytes of packed 8-bit RGB. In the
browser flow the page does this (``prepareAeicImage`` in
``frontend/src/services/imageCodec.ts``) and POSTs the pixels, which is why the
server needs no imaging stack for a user send.

A **bot** cannot. It fetches a JPEG over ``ctx.http`` and has bytes, so
something server-side has to decode and resize. That is all this module is.

Pillow is therefore part of the optional ``aeic`` extra rather than a base
dependency, and :func:`prepare_square_rgb` only reaches for it when it actually
has to: raw pixels of exactly the right length pass straight through, so the
browser path and the tests never need it.

## Stretch, not crop

The codec encodes a SQUARE with the whole frame stretched to fit. Nothing outside
the frame is discarded, and the source aspect travels in the metadata byte so the
receiver can letterbox back to it. Cropping here instead would silently throw
away the edges of every non-square photo, and the receiver would have no way to
know it happened.
"""

from __future__ import annotations

import logging

from app.imaging.aeic.onnx_backend import SQUARE_SIZE

logger = logging.getLogger(__name__)

RGB_BYTES_EXPECTED = SQUARE_SIZE * SQUARE_SIZE * 3

MAX_SOURCE_BYTES = 32 * 1024 * 1024
"""Refuse a source image larger than this before handing it to a decoder.

A bot can fetch an arbitrary URL, so this is the guard against a decompression
bomb turning one mesh message into an OOM kill.
"""


class AeicImagePrepareError(ValueError):
    """The supplied bytes could not be turned into a 512x512 RGB square."""


def pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401
    except ImportError:
        return False
    return True


def prepare_square_rgb(data: bytes) -> tuple[bytes, int, int]:
    """``(rgb, source_width, source_height)`` ready for the encoder.

    Accepts either:

    * exactly :data:`RGB_BYTES_EXPECTED` bytes of packed RGB, which is passed
      through untouched and reported as square -- the browser path, and the one
      that needs no Pillow; or
    * any image Pillow can open (JPEG, PNG, WebP, ...), which is decoded,
      converted to RGB and stretched into the square.

    ``source_width``/``source_height`` are the ORIGINAL dimensions, which the
    caller puts in the metadata byte so the receiver can undo the stretch.
    """
    if not data:
        raise AeicImagePrepareError("no image data")
    if len(data) == RGB_BYTES_EXPECTED:
        # Already the encoder's input. Reported square because raw pixels carry
        # no original shape; a caller that knows better passes its own aspect.
        return data, SQUARE_SIZE, SQUARE_SIZE
    if len(data) > MAX_SOURCE_BYTES:
        raise AeicImagePrepareError(
            f"source image is {len(data)} bytes, over the {MAX_SOURCE_BYTES} limit"
        )

    try:
        from PIL import Image
    except ImportError as exc:
        raise AeicImagePrepareError(
            f"an encoded image needs Pillow, which is part of the optional 'aeic' "
            f"extra (`uv sync --extra aeic`). Pass exactly {RGB_BYTES_EXPECTED} "
            f"bytes of {SQUARE_SIZE}x{SQUARE_SIZE} packed RGB to avoid it."
        ) from exc

    import io

    try:
        with Image.open(io.BytesIO(data)) as source:
            source_width, source_height = source.size
            # Convert before resizing: resampling a palette or 16-bit image
            # directly gives Pillow license to pick its own intermediate.
            square = source.convert("RGB").resize(
                (SQUARE_SIZE, SQUARE_SIZE), Image.Resampling.LANCZOS
            )
            rgb = square.tobytes()
    except AeicImagePrepareError:
        raise
    except Exception as exc:
        raise AeicImagePrepareError(f"could not decode the image: {exc}") from exc

    if len(rgb) != RGB_BYTES_EXPECTED:  # pragma: no cover - Pillow guarantees this
        raise AeicImagePrepareError(f"decoded {len(rgb)} bytes, expected {RGB_BYTES_EXPECTED}")
    if source_width <= 0 or source_height <= 0:  # pragma: no cover
        raise AeicImagePrepareError(f"bad source dimensions {source_width}x{source_height}")
    return rgb, source_width, source_height
