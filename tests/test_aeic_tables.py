"""The AEIC CDF table parser.

Validation here is strict on purpose. A table set that disagrees with the
checkpoint does not fail at decode time -- it desynchronises rANS and yields a
sharp, plausible, wrong image with no error anywhere. Refusing a file that is not
exactly the expected shape is the only cheap defence, so these tests pin both the
happy path against the real shipped file and every rejection.

Stdlib only: runs without the optional ``aeic`` extra.
"""

from __future__ import annotations

import struct

import pytest

from app.imaging.aeic.tables import (
    MAGIC,
    EntropyTableFormatError,
    parse_entropy_tables,
)
from tests.aeic_fixtures import TABLES_PATH, manifest


@pytest.fixture(scope="module")
def raw() -> bytes:
    return TABLES_PATH.read_bytes()


@pytest.fixture(scope="module")
def tables(raw):
    return parse_entropy_tables(raw)


def test_parses_the_shipped_ft32_table_header(tables):
    assert tables.version == 1
    assert tables.precision == 16
    assert tables.bypass_precision == 2
    assert tables.stream_parts == 2
    assert len(tables.groups) == 2


def test_group_shapes_match_the_manifest(tables):
    """Group 0 is z (the entropy bottleneck), group 1 is y (Gaussian conditional).

    The order is part of the bitstream format -- it comes from ``add_cdf`` call
    order in the reference -- so a swap here would be a wire-format break.
    """
    described = manifest()["table_groups"]
    assert [g["group"] for g in described] == ["z", "y"]
    for group, expected in zip(tables.groups, described, strict=True):
        assert group.num_cdfs == expected["rows"]
        assert group.cdf_width == expected["width"]
        assert min(group.cdf_length) == expected["cdf_length_min"]
        assert max(group.cdf_length) == expected["cdf_length_max"]
        assert min(group.offset) == expected["offset_min"]
        assert max(group.offset) == expected["offset_max"]
        assert max(group.quantized_cdf) == expected["cdf_max"]


def test_every_row_is_monotonic_within_its_valid_length(tables):
    """A non-monotonic CDF row would make the decoder's binary search meaningless."""
    for index, group in enumerate(tables.groups):
        for row in range(group.num_cdfs):
            n = group.cdf_length[row]
            values = [group.cdf_at(row, col) for col in range(n)]
            assert values == sorted(values), f"group {index} row {row} is not monotonic"
            assert values[0] == 0


def test_index_quantizer_constants_match_the_entropy_layer(tables):
    """The table file and :mod:`app.imaging.aeic.entropy` must agree.

    ``entropy`` hardcodes these so the four-stage loop does not need a table file
    just to quantize a scale; if the two ever diverged, the encoder and decoder
    would pick different CDF rows and the stream would desynchronise.
    """
    from app.imaging.aeic.entropy import (
        LOG_SCALE_MIN,
        LOG_SCALE_STEP,
        SCALE_FLOOR,
        SCALE_THRESHOLD,
        SCALES_LEVELS,
    )

    quantizer = tables.index_quantizer
    assert quantizer.scales_levels == SCALES_LEVELS
    # The file carries these as float64; the entropy layer deliberately holds
    # them as float32, because torch evaluated the reference expression on a
    # float32 tensor with float32 scalars and the port has to match that -- so
    # they agree to float32 precision, which is the agreement that matters.
    assert quantizer.log_scale_min == pytest.approx(float(LOG_SCALE_MIN), rel=1e-7)
    assert quantizer.log_scale_step == pytest.approx(float(LOG_SCALE_STEP), rel=1e-7)
    assert quantizer.scale_threshold == pytest.approx(float(SCALE_THRESHOLD), rel=1e-7)
    assert quantizer.scale_floor == pytest.approx(float(SCALE_FLOOR), rel=1e-7)
    # The first table entry is exp(log_scale_min) == 0.11, the last is 256.
    assert quantizer.scale_table[0] == pytest.approx(0.11, rel=1e-6)
    assert quantizer.scale_table[-1] == pytest.approx(256.0, rel=1e-6)


class TestRejections:
    def test_rejects_a_short_file(self):
        with pytest.raises(EntropyTableFormatError, match="too short"):
            parse_entropy_tables(b"AEICCDF\x01")

    def test_rejects_bad_magic(self, raw):
        with pytest.raises(EntropyTableFormatError, match="bad magic"):
            parse_entropy_tables(b"NOPE\x00\x00\x00\x00" + raw[8:])

    def test_rejects_an_unsupported_version(self, raw):
        mutated = bytearray(raw)
        struct.pack_into("<I", mutated, 8, 2)
        with pytest.raises(EntropyTableFormatError, match="unsupported version 2"):
            parse_entropy_tables(bytes(mutated))

    @pytest.mark.parametrize(
        ("offset", "value", "message"),
        [
            pytest.param(12, 17, "bad precision", id="precision above 16"),
            pytest.param(12, 0, "bad precision", id="zero precision"),
            pytest.param(16, 16, "bad bypassPrecision", id="bypass equals precision"),
            pytest.param(16, 0, "bad bypassPrecision", id="zero bypass"),
            pytest.param(20, 0, "bad streamParts", id="zero stream parts"),
            pytest.param(20, 17, "bad streamParts", id="too many stream parts"),
            pytest.param(24, 0, "bad numGroups", id="zero groups"),
        ],
    )
    def test_rejects_out_of_range_header_fields(self, raw, offset, value, message):
        mutated = bytearray(raw)
        struct.pack_into("<I", mutated, offset, value)
        with pytest.raises(EntropyTableFormatError, match=message):
            parse_entropy_tables(bytes(mutated))

    def test_rejects_a_truncated_body(self, raw):
        with pytest.raises(EntropyTableFormatError, match="truncated"):
            parse_entropy_tables(raw[: len(raw) // 2])

    def test_rejects_trailing_data(self, raw):
        with pytest.raises(EntropyTableFormatError, match="trailing data"):
            parse_entropy_tables(raw + b"\x00")

    def test_rejects_a_missing_end_trailer(self, raw):
        assert raw.endswith(b"END\x00")
        with pytest.raises(EntropyTableFormatError, match="END"):
            parse_entropy_tables(raw[:-4] + b"XXX\x00")

    def test_rejects_a_row_whose_length_exceeds_its_width(self, raw):
        """cdf_length[row] > width would read padding as probability mass."""
        # Group 0's block starts right after the 28-byte header: rows, width,
        # then the cdf_length array.
        rows, width = struct.unpack_from("<2I", raw, 28)
        mutated = bytearray(raw)
        struct.pack_into("<i", mutated, 36, width + 1)
        with pytest.raises(EntropyTableFormatError, match="cdfLength"):
            parse_entropy_tables(bytes(mutated))
        assert rows > 0  # sanity: we located a real block


def test_magic_constant_is_the_documented_bytes():
    assert MAGIC == b"AEICCDF\x01"
