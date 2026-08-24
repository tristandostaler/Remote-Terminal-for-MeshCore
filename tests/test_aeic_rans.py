"""The AEIC rANS coder must be BYTE-IDENTICAL to the C++ reference.

This is the most important test file in the AEIC port. The coder is synchronous
with the entropy model: the decoder re-runs the network to reproduce the exact
symbol probabilities the encoder used, so one differing byte does not raise --
it desynchronises the coder and silently emits a corrupt latent that decodes to
a sharp, plausible, WRONG image.

So these assertions are on exact bytes and exact digests, never on "close
enough", and the expected values come from meshcore-open's golden vectors, which
were produced by the reference C++ implementation rather than by any port.

Stdlib only: runs without the optional ``aeic`` extra.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from app.imaging.aeic.rans import (
    RansDecoder,
    RansEncoder,
    RansFormatError,
    build_rans_container,
    parse_rans_container,
)
from app.imaging.aeic.tables import parse_entropy_tables
from tests.aeic_fixtures import (
    TABLES_PATH,
    GoldenCase,
    image_cases,
    manifest,
    synthetic_cases,
)


@pytest.fixture(scope="module")
def tables():
    return parse_entropy_tables(TABLES_PATH.read_bytes())


def _encode(tables, case: GoldenCase) -> bytes:
    encoder = RansEncoder(tables)
    for symbols, indexes, group in case.pairs:
        encoder.encode_with_indexes(symbols, indexes, group)
    return encoder.finish()


@pytest.mark.parametrize("case", synthetic_cases(), ids=lambda c: c.label)
def test_synthetic_vector_encodes_to_the_exact_reference_bytes(tables, case: GoldenCase):
    assert _encode(tables, case) == case.expected_bitstream


@pytest.mark.parametrize("case", image_cases(), ids=lambda c: c.label)
def test_image_vector_encodes_to_the_exact_reference_bytes(tables, case: GoldenCase):
    """Five coder calls (z, y0..y3) accumulating into two sub-streams."""
    assert _encode(tables, case) == case.expected_bitstream


@pytest.mark.parametrize("case", synthetic_cases() + image_cases(), ids=lambda c: c.label)
def test_encoded_digest_and_substream_layout_match_the_manifest(tables, case: GoldenCase):
    """Digest and sub-stream split, not just the bytes.

    A same-length container with a different split would still be wrong, and the
    manifest records the split the reference produced.
    """
    encoded = _encode(tables, case)
    assert hashlib.sha256(encoded).hexdigest() == case.expected_sha256
    assert encoded[0] == case.container_flag
    assert [len(part) for part in parse_rans_container(encoded)] == case.substream_sizes


@pytest.mark.parametrize("case", synthetic_cases() + image_cases(), ids=lambda c: c.label)
def test_decoding_the_reference_bytes_recovers_every_coded_symbol(tables, case: GoldenCase):
    """Round-trip from the REFERENCE bytes, not from our own output.

    Only coded positions are comparable: an index below zero is not coded at all,
    so the encoder emits nothing and the decoder yields a literal 0 while the
    golden array still holds the original value.
    """
    decoder = RansDecoder(tables, case.expected_bitstream)
    for symbols, indexes, group in case.pairs:
        decoded = decoder.decode_stream(indexes, group)
        assert len(decoded) == len(symbols)
        coded = [
            (got, want)
            for got, want, index in zip(decoded, symbols, indexes, strict=True)
            if index >= 0
        ]
        assert all(got == want for got, want in coded)


def test_skipped_positions_decode_to_zero_and_consume_nothing(tables):
    """The `index < 0` contract, pinned against the manifest's own digest."""
    record = next(r for r in manifest()["synthetic"] if r["name"] == "y_skip_indexes")
    case = next(c for c in synthetic_cases() if c.label == "y_skip_indexes")
    symbols, indexes, group = case.pairs[0]
    assert sum(1 for index in indexes if index < 0) == record["n_skipped"]

    decoded = RansDecoder(tables, case.expected_bitstream).decode_stream(indexes, group)
    assert all(decoded[i] == 0 for i, index in enumerate(indexes) if index < 0)
    flat = struct.pack(f"<{len(decoded)}h", *decoded)
    assert hashlib.sha256(flat).hexdigest() == record["expected_decode_sha256"]


def test_table_file_is_the_one_the_manifest_describes():
    """Guards against a fixture drifting away from its manifest."""
    data = manifest()
    raw = TABLES_PATH.read_bytes()
    assert len(raw) == data["table_bytes"]
    assert hashlib.sha256(raw).hexdigest() == data["table_sha256"]


class TestContainerFraming:
    """The sub-stream container: ``flag | sizes | sub-streams``."""

    def test_round_trips_a_two_part_container(self):
        parts = [b"a" * 5, b"b" * 7]
        encoded = build_rans_container(parts)
        assert encoded[0] == 0x11  # (2 - 1) << 4 | 1  (2-byte size header)
        assert parse_rans_container(encoded) == parts

    def test_single_part_container_stores_no_size_table(self):
        encoded = build_rans_container([b"x" * 4])
        assert encoded == bytes([0x01]) + b"x" * 4
        assert parse_rans_container(encoded) == [b"x" * 4]

    def test_widens_the_size_header_past_65535(self):
        """Only the sizes actually written decide the width; the last is implied."""
        parts = [b"q" * 70000, b"r" * 5]
        encoded = build_rans_container(parts)
        assert encoded[0] == 0x10  # (2 - 1) << 4 | 0  (4-byte size header)
        assert parse_rans_container(encoded) == parts

    def test_a_last_part_over_65535_still_uses_the_narrow_header(self):
        parts = [b"q" * 5, b"r" * 70000]
        assert build_rans_container(parts)[0] == 0x11
        assert parse_rans_container(build_rans_container(parts)) == parts

    @pytest.mark.parametrize(
        "stream",
        [
            pytest.param(b"", id="empty"),
            pytest.param(bytes([0x11]), id="truncated size table"),
            pytest.param(bytes([0x11, 0xFF, 0xFF]) + b"ab", id="sizes exceed length"),
        ],
    )
    def test_rejects_a_malformed_container(self, stream):
        with pytest.raises(RansFormatError):
            parse_rans_container(stream)

    def test_rejects_more_sub_streams_than_the_flag_can_address(self):
        with pytest.raises(RansFormatError):
            build_rans_container([b"x" * 4] * 17)


class TestEncoderGuards:
    def test_rejects_mismatched_symbol_and_index_lengths(self, tables):
        with pytest.raises(ValueError, match="differ"):
            RansEncoder(tables).encode_with_indexes([1, 2], [0], 0)

    def test_rejects_an_unknown_cdf_group(self, tables):
        with pytest.raises(ValueError, match="no CDF group"):
            RansEncoder(tables).encode_with_indexes([1, 2], [0, 0], 7)

    def test_rejects_an_index_past_the_end_of_the_group(self, tables):
        rows = tables.z_group.num_cdfs
        with pytest.raises(RansFormatError, match="out of range"):
            RansEncoder(tables).encode_with_indexes([0, 0], [rows, rows], 0)

    def test_refuses_to_finish_twice(self, tables):
        encoder = RansEncoder(tables)
        encoder.finish()
        with pytest.raises(RuntimeError, match="already been called"):
            encoder.finish()

    def test_reset_allows_reuse(self, tables):
        case = synthetic_cases()[0]
        encoder = RansEncoder(tables)
        symbols, indexes, group = case.pairs[0]
        encoder.encode_with_indexes(symbols, indexes, group)
        first = encoder.finish()
        encoder.reset()
        encoder.encode_with_indexes(symbols, indexes, group)
        assert encoder.finish() == first


class TestDecoderGuards:
    def test_rejects_a_container_with_the_wrong_sub_stream_count(self, tables):
        # tables.stream_parts is 2; a single-part container cannot be this stream.
        with pytest.raises(RansFormatError, match="expected 2"):
            RansDecoder(tables, build_rans_container([b"x" * 8]))

    def test_rejects_a_sub_stream_too_short_to_hold_a_state(self, tables):
        with pytest.raises(RansFormatError, match="shorter than 4 bytes"):
            RansDecoder(tables, build_rans_container([b"ab", b"cd"]))

    def test_a_truncated_stream_is_rejected_at_parse(self, tables):
        """Truncation usually breaks the size table, so it fails before decoding."""
        case = image_cases()[0]
        truncated = case.expected_bitstream[: len(case.expected_bitstream) // 2]
        with pytest.raises(RansFormatError):
            RansDecoder(tables, truncated)

    def test_reports_exhaustion_rather_than_returning_garbage(self, tables):
        """A structurally valid container with too few bytes must fail loudly.

        This codec's whole risk is that corruption is otherwise silent, so
        running off the end of a sub-stream has to raise rather than keep
        producing plausible symbols.
        """
        # A well-formed container whose sub-streams hold only a state and no
        # coded bytes, decoded for far more symbols than it can supply.
        starved = build_rans_container([bytes(4), bytes(4)])
        decoder = RansDecoder(tables, starved)
        with pytest.raises(RansFormatError, match="exhausted|corrupt"):
            decoder.decode_stream([0] * 4096, 0)
