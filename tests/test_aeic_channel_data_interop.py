"""Cross-compatibility: our GRP_DATA framing against MCO Advanced's own vectors.

``tests/test_aeic_channel_data.py`` checks the port against upstream's published
*constants*. This file goes one step further and replays upstream's **unit test**
-- ``test/image_chunk_transport_test.dart`` on meshcore-open's
``rename-mco-advanced`` branch -- case for case, using the same deterministic
payload generator, the same sender prefixes, the same image ids and the same
expected byte offsets.

Why bother, when the port already has tests? Because those tests were written
against the port. These were written against the *other implementation*, before
this one existed. If a future edit here drifts from the Dart, upstream's own
assertions are what fail -- which is exactly the failure mode that matters: an
image that MCO Advanced renders as garbage rather than a picture.

Every expectation below is transcribed from that Dart file. Where it says
``expect(blob[0], 0x12)``, so does this. Nothing here is derived from
``app.imaging.aeic.channel_data``; that is the point.
"""

from __future__ import annotations

import pytest

from app.imaging.aeic.channel_data import (
    BODY_BYTES,
    CHUNK_ZERO_IMAGE_BYTES,
    HEADER_BYTES,
    MAX_DATA_CHUNKS,
    ChannelDataFormatError,
    build_image_chunks,
    parse_chunk_blob,
)
from app.imaging.aeic.channel_data_ingest import ChannelDataReassembler
from app.imaging.aeic.text_transport import (
    RATE_WIRE_HIGH,
    RATE_WIRE_STANDARD,
    AeicStreamMetadata,
)


# ``payloadOf`` from the Dart test, verbatim:
#   List<int>.generate(length, (i) => (i * 37 + seed * 11) & 0xFF)
def payload_of(length: int, seed: int = 7) -> bytes:
    return bytes((i * 37 + seed * 11) & 0xFF for i in range(length))


STD_META = AeicStreamMetadata(rate_code=RATE_WIRE_STANDARD)
HIGH_META = AeicStreamMetadata(rate_code=RATE_WIRE_HIGH)

SENDER_A = 0x1234
SENDER_B = 0xBEEF

MAX_PAYLOAD_BYTES = CHUNK_ZERO_IMAGE_BYTES + (MAX_DATA_CHUNKS - 1) * BODY_BYTES
"""Upstream's ``kImageMaxPayloadBytes``: ``157 + 14 * 158``."""


def feed(reassembler: ChannelDataReassembler, blobs, *, key: str = "chan"):
    """Upstream's ``feed`` helper: push blobs in order, keep the first result."""
    result = None
    for blob in blobs:
        outcome = reassembler.note_chunk(key, blob)
        if result is None:
            result = outcome
    return result


class TestUpstreamChunkingVectors:
    """``group('chunking')`` in image_chunk_transport_test.dart."""

    def test_upstream_max_payload_arithmetic(self):
        # expect(kImageMaxPayloadBytes, 157 + 14 * 158);
        assert MAX_PAYLOAD_BYTES == 157 + 14 * 158

    @pytest.mark.parametrize(
        ("length", "expected_chunks"),
        [
            (1, 1),
            (CHUNK_ZERO_IMAGE_BYTES, 1),
            (CHUNK_ZERO_IMAGE_BYTES + 1, 2),
            (CHUNK_ZERO_IMAGE_BYTES + BODY_BYTES, 2),
            (CHUNK_ZERO_IMAGE_BYTES + BODY_BYTES + 1, 3),
        ],
    )
    def test_one_two_and_three_chunk_payloads_produce_the_right_shapes(
        self, length, expected_chunks
    ):
        blobs = build_image_chunks(
            payload_of(length), STD_META.encode(), sender_prefix=SENDER_A, img_id=3
        )
        # dataChunkCount == expectedChunks, and blobs.length == that + parity.
        assert len(blobs) == expected_chunks + 1
        assert all(len(blob) <= 163 for blob in blobs)

    def test_header_fields_are_on_the_wire_where_the_spec_says(self):
        # The Dart test's exact case: 400 bytes, highMeta, senderA, imgId 0x5A.
        blobs = build_image_chunks(
            payload_of(400), HIGH_META.encode(), sender_prefix=SENDER_A, img_id=0x5A
        )
        assert len(blobs) == 3 + 1  # 3 data chunks + parity
        for i, blob in enumerate(blobs):
            assert blob[0] == 0x12
            assert blob[1] == 0x34
            assert blob[2] == 0x5A
            assert (blob[3] >> 4) & 0x0F == i
            assert blob[3] & 0x0F == 3
        last = parse_chunk_blob(blobs[-1])
        assert last is not None and last.is_parity
        assert len(blobs[3]) > HEADER_BYTES
        # Chunk 0 carries the metadata byte first.
        assert blobs[0][HEADER_BYTES] == HIGH_META.encode()

    def test_rejects_a_payload_larger_than_the_framing_can_address(self):
        with pytest.raises(ChannelDataFormatError):
            build_image_chunks(
                payload_of(MAX_PAYLOAD_BYTES + 1),
                STD_META.encode(),
                sender_prefix=SENDER_A,
                img_id=1,
            )


class TestUpstreamRoundTripVectors:
    """``group('round trip')`` -- the blobs we emit must reassemble to the input.

    Upstream feeds them to its own Dart reassembler; here they go through ours.
    Both directions matter, and they share the same vectors, so a framing change
    that broke only one of the two would still be caught.
    """

    @pytest.mark.parametrize("length", [100, 300, 460])
    def test_round_trip_at_upstreams_lengths(self, length):
        payload = payload_of(length)
        blobs = build_image_chunks(payload, HIGH_META.encode(), sender_prefix=SENDER_A, img_id=1)
        result = feed(ChannelDataReassembler(), blobs)
        assert result is not None
        bitstream, metadata_byte, recovered = result
        assert bitstream == payload
        assert metadata_byte == HIGH_META.encode()
        assert AeicStreamMetadata.decode(metadata_byte) == HIGH_META
        assert recovered is False

    def test_parity_completes_an_image_that_is_one_chunk_short(self):
        # payloadOf(300) -> 2 data chunks; drop chunk 1, parity rebuilds it.
        payload = payload_of(300)
        blobs = build_image_chunks(payload, STD_META.encode(), sender_prefix=SENDER_A, img_id=16)
        reassembler = ChannelDataReassembler()
        assert reassembler.note_chunk("chan", blobs[0]) is None
        result = reassembler.note_chunk("chan", blobs[2])  # the parity blob
        assert result is not None
        bitstream, _, recovered = result
        assert bitstream == payload
        assert recovered is True

    def test_duplicate_chunks_are_ignored(self):
        payload = payload_of(460)  # 3 data chunks + parity
        blobs = build_image_chunks(payload, STD_META.encode(), sender_prefix=SENDER_A, img_id=2)
        reassembler = ChannelDataReassembler()
        assert reassembler.note_chunk("chan", blobs[0]) is None
        assert reassembler.note_chunk("chan", blobs[0]) is None  # duplicate
        assert reassembler.note_chunk("chan", blobs[2]) is None
        assert reassembler.note_chunk("chan", blobs[2]) is None  # duplicate
        done = reassembler.note_chunk("chan", blobs[1])
        assert done is not None
        assert done[0] == payload
        assert done[2] is False

    def test_two_senders_interleaved_on_one_channel_do_not_mix(self):
        # Same img_id on purpose: only the sender prefix separates them.
        a, b = payload_of(300, seed=1), payload_of(300, seed=2)
        set_a = build_image_chunks(a, STD_META.encode(), sender_prefix=SENDER_A, img_id=5)
        set_b = build_image_chunks(b, HIGH_META.encode(), sender_prefix=SENDER_B, img_id=5)

        reassembler = ChannelDataReassembler()
        got: list[tuple[bytes, int]] = []
        for blob in (set_a[0], set_b[0], set_a[1], set_b[1]):
            outcome = reassembler.note_chunk("chan", blob)
            if outcome is not None:
                got.append((outcome[0], outcome[1]))

        assert (a, STD_META.encode()) in got
        assert (b, HIGH_META.encode()) in got
