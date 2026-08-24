"""The ``aei1:`` text framing that carries AEIC images as ordinary messages.

The framing is what makes the feature fit: an AEIC bitstream is 117-209 bytes,
basE91 expands it by ~23%, and a MeshCore message carries 156 -- so a 512px
colour photo is one or two messages. These tests pin that budget, because a
regression that pushes a typical photo from one message to two doubles its
airtime, and one that overflows 156 bytes silently truncates it on the radio.

Stdlib only: runs without the optional ``aeic`` extra.
"""

from __future__ import annotations

import os

import pytest

from app.compression import decode_base91, encode_base91
from app.compression.mcmp import MeshCompressorError
from app.imaging.aeic.text_transport import (
    ASPECT_CODES,
    ASPECT_UNKNOWN,
    DEFAULT_MESSAGE_BUDGET,
    FIRST_HEADER_CHARS,
    HEADER_CHARS,
    MAX_CHUNKS,
    PREFIX,
    RATE_WIRE_STANDARD,
    AeicStreamMetadata,
    AeicTextFormatError,
    aspect_code_for,
    chunk_capacities,
    encode_chunks,
    is_aeic_chunk,
    new_session_id,
    parse_chunk,
    reassemble,
)

SQUARE = AeicStreamMetadata(square_size=512, rate_code=RATE_WIRE_STANDARD, aspect_code=0)


def frame(payload: bytes, *, budget: int = DEFAULT_MESSAGE_BUDGET, session_id: int = 42):
    return encode_chunks(payload, SQUARE, session_id=session_id, message_budget=budget)


def round_trip(chunks: list[str]) -> bytes | None:
    parsed = [parse_chunk(chunk) for chunk in chunks]
    assert all(chunk is not None for chunk in parsed)
    return reassemble({c.index: c.payload for c in parsed}, parsed[0].total)  # type: ignore[union-attr]


class TestBase91:
    """basE91 is shared with MCMP; AEIC relies on it for arbitrary BINARY."""

    @pytest.mark.parametrize("length", [0, 1, 2, 3, 12, 13, 14, 117, 156, 209, 512])
    def test_round_trips_binary_at_every_awkward_length(self, length):
        data = os.urandom(length)
        assert decode_base91(encode_base91(data)) == data

    def test_expansion_stays_near_23_percent(self):
        """The whole one-or-two-message claim rests on this ratio."""
        for length in (117, 166, 209):
            assert len(encode_base91(os.urandom(length))) == pytest.approx(
                length * 1.2308, rel=0.02
            )

    def test_output_avoids_characters_a_message_body_cannot_carry(self):
        text = encode_base91(os.urandom(4096))
        for forbidden in (" ", "'", "\\", "-"):
            assert forbidden not in text

    def test_rejects_a_character_outside_the_alphabet(self):
        with pytest.raises(MeshCompressorError):
            decode_base91("abc def")


class TestChunking:
    def test_a_typical_photo_fits_in_one_message(self):
        """117 B is the low end of the measured ft32 range."""
        chunks = frame(os.urandom(117))
        assert len(chunks) == 1
        assert len(chunks[0]) <= DEFAULT_MESSAGE_BUDGET

    @pytest.mark.parametrize(
        ("size", "expected"),
        [
            pytest.param(117, 1, id="measured minimum"),
            pytest.param(156, 2, id="measured mean"),
            pytest.param(209, 2, id="measured maximum"),
        ],
    )
    def test_the_measured_ft32_range_never_needs_more_than_two_messages(self, size, expected):
        assert len(frame(os.urandom(size))) == expected

    @pytest.mark.parametrize("size", [1, 50, 117, 156, 209, 400, 1000])
    def test_round_trips_and_never_exceeds_the_budget(self, size):
        data = os.urandom(size)
        chunks = frame(data)
        assert all(len(chunk) <= DEFAULT_MESSAGE_BUDGET for chunk in chunks)
        assert round_trip(chunks) == data

    def test_a_channel_budget_shrinks_capacity_but_still_round_trips(self):
        """A channel message carries "sender: " inside the encrypted payload."""
        budget = DEFAULT_MESSAGE_BUDGET - len("LongRadioName") - 2
        data = os.urandom(209)
        chunks = frame(data, budget=budget)
        assert all(len(chunk) <= budget for chunk in chunks)
        assert round_trip(chunks) == data

    def test_header_costs_are_what_the_capacities_claim(self):
        first, rest = chunk_capacities(DEFAULT_MESSAGE_BUDGET)
        assert first == DEFAULT_MESSAGE_BUDGET - FIRST_HEADER_CHARS
        assert rest == DEFAULT_MESSAGE_BUDGET - HEADER_CHARS
        # Chunk 0 pays two extra characters for the metadata byte.
        assert FIRST_HEADER_CHARS - HEADER_CHARS == 2

    def test_only_the_first_chunk_carries_metadata(self):
        chunks = frame(os.urandom(400))
        assert len(chunks) > 1
        parsed = [parse_chunk(chunk) for chunk in chunks]
        assert parsed[0].metadata is not None  # type: ignore[union-attr]
        assert all(chunk.metadata is None for chunk in parsed[1:])  # type: ignore[union-attr]

    def test_every_chunk_names_the_same_session_and_total(self):
        """A later chunk has to be attributable without chunk 0 having arrived."""
        chunks = frame(os.urandom(400), session_id=1234)
        for index, chunk in enumerate(chunks):
            parsed = parse_chunk(chunk)
            assert parsed is not None
            assert parsed.session_id == 1234
            assert parsed.index == index
            assert parsed.total == len(chunks)

    def test_refuses_an_empty_bitstream(self):
        with pytest.raises(AeicTextFormatError, match="empty"):
            frame(b"")

    def test_refuses_a_budget_too_small_for_a_header(self):
        with pytest.raises(AeicTextFormatError, match="too small"):
            chunk_capacities(FIRST_HEADER_CHARS)

    def test_refuses_a_payload_needing_more_chunks_than_the_format_addresses(self):
        # `tot` is one base36 character, so 36 chunks is the ceiling.
        too_big = os.urandom(DEFAULT_MESSAGE_BUDGET * (MAX_CHUNKS + 4))
        with pytest.raises(AeicTextFormatError, match="more than"):
            frame(too_big)

    def test_session_ids_are_in_range(self):
        assert all(0 <= new_session_id() <= 36**2 - 1 for _ in range(200))


class TestParsing:
    @pytest.mark.parametrize(
        "text",
        [
            pytest.param("", id="empty"),
            pytest.param("hello world", id="ordinary text"),
            pytest.param("aei1", id="prefix only"),
            pytest.param("aei100", id="no index or total"),
            pytest.param("aei1000", id="no payload"),
            pytest.param("aei10011", id="metadata but no payload"),
            pytest.param("mcmp2:abcdef", id="an MCMP body"),
            pytest.param("IE4:1:1:1:8:8:64", id="an IE4 envelope"),
        ],
    )
    def test_returns_none_for_text_that_is_not_a_chunk(self, text):
        """None, not an exception: this runs on every inbound message, and text
        that merely starts with `aei1` is not an error."""
        assert parse_chunk(text) is None

    def test_rejects_an_index_at_or_past_the_total(self):
        # Layout: aei1 | sid(2) | idx(1) | tot(1) | meta(2) on chunk 0 | payload.
        payload = "A" * 20
        assert parse_chunk("aei1" + "00" + "0" + "2" + "00" + payload) is not None  # 0 of 2
        assert parse_chunk("aei1" + "00" + "1" + "2" + payload) is not None  # 1 of 2
        assert parse_chunk("aei1" + "00" + "2" + "2" + payload) is None  # 2 of 2
        assert parse_chunk("aei1" + "00" + "5" + "2" + payload) is None  # 5 of 2
        assert parse_chunk("aei1" + "00" + "0" + "0" + "00" + payload) is None  # total 0

    def test_rejects_an_unknown_rate_code(self):
        """Guessing a rate would hand the wrong model a bitstream it will happily
        turn into garbage, so an unknown code is a refusal."""
        # Metadata byte 0x02: rate bits = 2, which no build can decode.
        assert parse_chunk("aei1" + "00" + "0" + "1" + "02" + "A" * 20) is None
        # ...while rate 0 and 1 are both accepted.
        assert parse_chunk("aei1" + "00" + "0" + "1" + "00" + "A" * 20) is not None
        assert parse_chunk("aei1" + "00" + "0" + "1" + "01" + "A" * 20) is not None

    def test_is_aeic_chunk_is_a_cheap_prefix_test(self):
        assert is_aeic_chunk(PREFIX + "0001" + "A")
        assert not is_aeic_chunk("hello")


class TestStreamMetadata:
    def test_round_trips_the_metadata_byte(self):
        for aspect in range(16):
            metadata = AeicStreamMetadata(square_size=512, aspect_code=aspect)
            decoded = AeicStreamMetadata.decode(metadata.encode())
            assert decoded == metadata

    def test_packs_into_a_single_byte(self):
        assert 0 <= AeicStreamMetadata(square_size=1024, aspect_code=15).encode() <= 255

    def test_layout_matches_mco_advanced(self):
        """aspect(4) | resolution(2) | rate(2), the same byte the binary
        GRP_DATA transport puts in its chunk 0 -- so a future bridge between the
        two transports needs no second definition."""
        byte = AeicStreamMetadata(square_size=256, rate_code=1, aspect_code=5).encode()
        assert (byte >> 4) & 0x0F == 5
        assert (byte >> 2) & 0x03 == 1  # 256 is resolution code 1
        assert byte & 0x03 == 1

    def test_rejects_a_square_size_the_byte_cannot_name(self):
        with pytest.raises(AeicTextFormatError, match="not representable"):
            AeicStreamMetadata(square_size=640).encode()

    def test_decode_refuses_an_unknown_rate(self):
        assert AeicStreamMetadata.decode(0b0000_0010) is None
        assert AeicStreamMetadata.decode(0b0000_0011) is None

    @pytest.mark.parametrize(
        ("width", "height", "expected"),
        [
            pytest.param(1000, 1000, 0, id="square"),
            pytest.param(4032, 3024, 2, id="4:3 landscape"),
            pytest.param(1920, 1080, 5, id="16:9 landscape"),
            pytest.param(1080, 1920, 12, id="9:16 portrait"),
            pytest.param(3024, 4032, 9, id="3:4 portrait"),
            pytest.param(5000, 500, ASPECT_UNKNOWN, id="extreme panorama"),
            pytest.param(0, 100, ASPECT_UNKNOWN, id="degenerate"),
        ],
    )
    def test_aspect_code_picks_the_nearest_ratio_in_log_space(self, width, height, expected):
        assert aspect_code_for(width, height) == expected

    def test_aspect_is_symmetric_between_landscape_and_portrait(self):
        """Compared in log space so 4:3 and 3:4 are equally far from square."""
        for w, h in ((4032, 3024), (1920, 1080), (3, 2)):
            landscape = ASPECT_CODES[aspect_code_for(w, h)]
            portrait = ASPECT_CODES[aspect_code_for(h, w)]
            assert landscape == portrait[::-1]

    def test_unknown_and_square_both_render_square(self):
        assert AeicStreamMetadata(aspect_code=0).is_square
        assert AeicStreamMetadata(aspect_code=ASPECT_UNKNOWN).is_square
        assert not AeicStreamMetadata(aspect_code=5).is_square
        assert AeicStreamMetadata(aspect_code=ASPECT_UNKNOWN).aspect_ratio == 1.0


class TestReassembly:
    def test_returns_none_while_a_chunk_is_missing(self):
        chunks = frame(os.urandom(400))
        parsed = [parse_chunk(chunk) for chunk in chunks]
        partial = {c.index: c.payload for c in parsed[:-1]}  # type: ignore[union-attr]
        assert reassemble(partial, len(chunks)) is None

    def test_decodes_the_concatenation_not_the_chunks(self):
        """basE91 is stateful across the stream: decoding per chunk would corrupt
        every boundary, so this is the check that the join happens first."""
        data = os.urandom(400)
        chunks = frame(data)
        assert len(chunks) > 1
        parsed = [parse_chunk(chunk) for chunk in chunks]
        joined = reassemble({c.index: c.payload for c in parsed}, len(chunks))  # type: ignore[union-attr]
        assert joined == data
        # Per-chunk decoding does not reconstruct the payload.
        per_chunk = b"".join(decode_base91(c.payload) for c in parsed)  # type: ignore[union-attr]
        assert per_chunk != data

    def test_is_order_independent(self):
        data = os.urandom(400)
        parsed = [parse_chunk(chunk) for chunk in frame(data)]
        shuffled = dict(reversed([(c.index, c.payload) for c in parsed]))  # type: ignore[union-attr]
        assert reassemble(shuffled, len(parsed)) == data
