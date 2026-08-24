"""Turning arbitrary image bytes into the encoder's 512x512 RGB square.

The browser does this itself for a user send, so this module exists for the bot
path: a bot fetches a JPEG over ``ctx.http`` and cannot decode it. Pillow is part
of the optional ``aeic`` extra and is only reached for when the input is not
already raw pixels, which is what the pass-through test pins.
"""

from __future__ import annotations

import io

import pytest

from app.imaging.aeic.prepare import (
    MAX_SOURCE_BYTES,
    RGB_BYTES_EXPECTED,
    AeicImagePrepareError,
    pillow_available,
    prepare_square_rgb,
)

SQUARE_SIZE = 512

requires_pillow = pytest.mark.skipif(
    not pillow_available(), reason="Pillow is part of the optional 'aeic' extra"
)


def make_image(width: int, height: int, fmt: str = "PNG") -> bytes:
    from PIL import Image

    image = Image.new("RGB", (width, height))
    for x in range(width):
        for y in range(height):
            image.putpixel((x, y), ((x * 7) % 256, (y * 11) % 256, ((x + y) * 3) % 256))
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return buffer.getvalue()


class TestRawPassthrough:
    def test_exact_rgb_is_returned_untouched_and_needs_no_pillow(self):
        """The browser path: pixels straight through, no decoder involved."""
        rgb = bytes(RGB_BYTES_EXPECTED)
        out, width, height = prepare_square_rgb(rgb)
        assert out is rgb
        assert (width, height) == (SQUARE_SIZE, SQUARE_SIZE)

    def test_expected_length_matches_the_encoder_contract(self):
        assert RGB_BYTES_EXPECTED == SQUARE_SIZE * SQUARE_SIZE * 3 == 786_432

    def test_rejects_empty_input(self):
        with pytest.raises(AeicImagePrepareError, match="no image data"):
            prepare_square_rgb(b"")

    def test_rejects_bytes_that_are_neither_pixels_nor_an_image(self):
        with pytest.raises(AeicImagePrepareError, match="could not decode"):
            prepare_square_rgb(b"definitely not an image")

    def test_refuses_an_oversized_source_before_decoding_it(self):
        """A bot can fetch an arbitrary URL, so this is the decompression-bomb
        guard -- it must fire on the byte count, before any decoder runs."""
        with pytest.raises(AeicImagePrepareError, match="over the"):
            prepare_square_rgb(b"\x00" * (MAX_SOURCE_BYTES + 1))


@requires_pillow
class TestDecodeAndSquare:
    def test_decodes_a_png_into_the_square(self):
        rgb, width, height = prepare_square_rgb(make_image(64, 64))
        assert len(rgb) == RGB_BYTES_EXPECTED
        assert (width, height) == (64, 64)

    def test_decodes_a_jpeg(self):
        rgb, width, height = prepare_square_rgb(make_image(80, 60, "JPEG"))
        assert len(rgb) == RGB_BYTES_EXPECTED
        assert (width, height) == (80, 60)

    @pytest.mark.parametrize(
        ("width", "height"),
        [
            pytest.param(64, 48, id="4:3 landscape"),
            pytest.param(48, 64, id="3:4 portrait"),
            pytest.param(100, 100, id="square"),
            pytest.param(160, 90, id="16:9"),
            pytest.param(1, 40, id="degenerate sliver"),
        ],
    )
    def test_always_produces_the_square_and_reports_the_source_shape(self, width, height):
        """The source shape is what the metadata byte carries, so the receiver
        can undo the stretch. Losing it would render every photo squashed."""
        rgb, reported_w, reported_h = prepare_square_rgb(make_image(width, height))
        assert len(rgb) == RGB_BYTES_EXPECTED
        assert (reported_w, reported_h) == (width, height)

    def test_stretches_rather_than_crops(self):
        """A crop would silently discard the edges of every non-square photo and
        the receiver would have no way to know."""
        from PIL import Image

        # A wide image with a distinctive right-hand edge. After a stretch that
        # column is still present; after a centre crop it would be gone.
        image = Image.new("RGB", (256, 64), (0, 0, 0))
        for y in range(64):
            image.putpixel((255, y), (255, 0, 0))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")

        rgb, _w, _h = prepare_square_rgb(buffer.getvalue())
        # Right-most column of the square should carry the red edge.
        row_stride = SQUARE_SIZE * 3
        last_pixel = rgb[row_stride - 3 : row_stride]
        assert last_pixel[0] > 100, "the source's right edge did not survive"

    def test_converts_a_palette_image_to_rgb(self):
        from PIL import Image

        image = Image.new("P", (32, 32))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        rgb, _w, _h = prepare_square_rgb(buffer.getvalue())
        assert len(rgb) == RGB_BYTES_EXPECTED

    def test_drops_alpha_rather_than_failing(self):
        from PIL import Image

        buffer = io.BytesIO()
        Image.new("RGBA", (32, 32), (10, 20, 30, 128)).save(buffer, format="PNG")
        rgb, _w, _h = prepare_square_rgb(buffer.getvalue())
        assert len(rgb) == RGB_BYTES_EXPECTED

    def test_an_image_that_happens_to_be_512_square_still_decodes(self):
        """A 512x512 PNG is not RGB_BYTES_EXPECTED bytes long, so it must take
        the decode path rather than being mistaken for raw pixels."""
        rgb, width, height = prepare_square_rgb(make_image(512, 512))
        assert len(rgb) == RGB_BYTES_EXPECTED
        assert (width, height) == (512, 512)
