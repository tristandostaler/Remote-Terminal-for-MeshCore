"""The binary GRP_DATA transport: chunk framing, XOR parity, reassembly.

This is the interoperable wire format, ported from meshcore-open's
``lib/services/image_chunk_transport.dart``. Getting a field offset or a nibble
order wrong here does not fail loudly -- it hands the AEIC decoder a
plausible-looking wrong bitstream -- so these tests pin the layout byte by byte
against the constants upstream publishes, not against our own implementation.
"""

from __future__ import annotations

import pytest

from app.imaging.aeic.channel_data import (
    BLOB_BYTES,
    BODY_BYTES,
    CHUNK_ZERO_IMAGE_BYTES,
    CMD_SEND_CHANNEL_DATA,
    DATA_TYPE_AEIC_IMAGE,
    HEADER_BYTES,
    MAX_DATA_CHUNKS,
    METADATA_BYTES,
    OUT_PATH_UNKNOWN,
    RESP_CODE_CHANNEL_DATA_RECV,
    ChannelDataFormatError,
    PendingImage,
    assemble,
    build_chunk_blob,
    build_image_chunks,
    build_parity_body,
    build_send_command,
    parse_channel_data_frame,
    parse_chunk_blob,
    recover_missing_body,
    sender_prefix_for,
    split_image_bodies,
)

SELF_KEY = bytes.fromhex("ab" * 32)
META = 0x20  # aspect 2 (4:3) | resolution 0 (512) | rate 0 (ft32)


class TestConstantsMatchUpstream:
    """Every one of these is quoted from upstream's own header comment."""

    def test_blob_and_header_sizes(self):
        assert BLOB_BYTES == 163
        assert HEADER_BYTES == 4
        assert BODY_BYTES == 158  # 163 - 4 - 1 parity length byte
        assert METADATA_BYTES == 1

    def test_chunk_zero_carries_one_byte_less_image_data(self):
        assert CHUNK_ZERO_IMAGE_BYTES == 157

    def test_protocol_numbers(self):
        assert CMD_SEND_CHANNEL_DATA == 62
        assert RESP_CODE_CHANNEL_DATA_RECV == 27
        assert DATA_TYPE_AEIC_IMAGE == 0xAE1C
        assert OUT_PATH_UNKNOWN == 0xFF
        assert MAX_DATA_CHUNKS == 15

    def test_upstream_capacity_claims_hold(self):
        """Upstream's own worked example: ft32 max 209 B is two chunks."""
        assert len(split_image_bodies(bytes(209), META)) == 2
        assert len(split_image_bodies(bytes(155), META)) == 1
        # 157 + 158 = 315 is the two-chunk ceiling.
        assert len(split_image_bodies(bytes(315), META)) == 2
        assert len(split_image_bodies(bytes(316), META)) == 3


class TestChunkHeader:
    def test_index_is_the_high_nibble_and_total_the_low(self):
        """The nibble order is the easiest thing to get backwards."""
        blob = build_chunk_blob(sender_prefix=0x1234, img_id=7, index=2, total=5, body=b"xyz")
        assert blob[3] == (2 << 4) | 5
        parsed = parse_chunk_blob(blob)
        assert parsed is not None
        assert parsed.index == 2
        assert parsed.total == 5

    def test_sender_prefix_is_big_endian_from_the_public_key(self):
        key = bytes.fromhex("1234") + bytes(30)
        assert sender_prefix_for(key) == 0x1234
        blob = build_chunk_blob(sender_prefix=0x1234, img_id=0, index=0, total=1, body=b"a")
        assert blob[0] == 0x12
        assert blob[1] == 0x34

    def test_index_equal_to_total_marks_parity(self):
        blob = build_chunk_blob(sender_prefix=1, img_id=1, index=3, total=3, body=b"p")
        parsed = parse_chunk_blob(blob)
        assert parsed is not None and parsed.is_parity

    def test_a_data_chunk_is_not_parity(self):
        blob = build_chunk_blob(sender_prefix=1, img_id=1, index=2, total=3, body=b"d")
        parsed = parse_chunk_blob(blob)
        assert parsed is not None and not parsed.is_parity

    @pytest.mark.parametrize(
        "blob",
        [
            pytest.param(b"", id="empty"),
            pytest.param(b"\x00\x01\x02\x11", id="header with no body"),
            pytest.param(b"\x00\x01\x02\x00extra", id="total 0"),
            pytest.param(b"\x00\x01\x02\x51body", id="index 5 beyond total 1"),
        ],
    )
    def test_malformed_blobs_return_none(self, blob):
        assert parse_chunk_blob(blob) is None

    def test_a_blob_never_exceeds_the_wire_limit(self):
        chunks = build_image_chunks(bytes(600), META, sender_prefix=1, img_id=1)
        assert all(len(blob) <= BLOB_BYTES for blob in chunks)


class TestParity:
    def test_parity_body_is_length_xor_then_body_xor(self):
        bodies = [bytes([1, 2, 3]), bytes([4, 5])]
        parity = build_parity_body(bodies)
        assert parity[0] == (3 ^ 2)
        assert parity[1] == (1 ^ 4)
        assert parity[2] == (2 ^ 5)
        assert parity[3] == 3
        assert len(parity) == BODY_BYTES + 1

    def test_a_full_image_emits_data_chunks_then_parity(self):
        blobs = build_image_chunks(bytes(300), META, sender_prefix=9, img_id=3)
        parsed = [parse_chunk_blob(b) for b in blobs]
        assert [p.index for p in parsed] == [0, 1, 2]
        assert parsed[-1].is_parity
        assert not parsed[0].is_parity

    def test_parity_can_be_suppressed(self):
        blobs = build_image_chunks(bytes(300), META, sender_prefix=9, img_id=3, with_parity=False)
        assert len(blobs) == 2
        assert not any(parse_chunk_blob(b).is_parity for b in blobs)


class TestReassembly:
    def _feed(self, bitstream: bytes, drop: int | None = None):
        blobs = build_image_chunks(bitstream, META, sender_prefix=0x1234, img_id=42)
        parsed = [parse_chunk_blob(b) for b in blobs]
        total = parsed[0].total
        entry = PendingImage(total=total)
        for chunk in parsed:
            if drop is not None and chunk.index == drop and not chunk.is_parity:
                continue
            if chunk.is_parity:
                entry.parity_body = chunk.body
            else:
                entry.bodies[chunk.index] = chunk.body
        return entry

    @pytest.mark.parametrize("size", [1, 100, 157, 158, 209, 315, 316, 600])
    def test_round_trip_without_loss(self, size):
        bitstream = bytes((i * 7 + 3) & 0xFF for i in range(size))
        entry = self._feed(bitstream)
        assert entry.is_complete
        result = assemble(entry)
        assert result is not None
        assert result == (bitstream, META)

    @pytest.mark.parametrize("size", [209, 315, 600])
    @pytest.mark.parametrize("drop", [0, 1])
    def test_single_loss_is_recovered_from_parity(self, size, drop):
        bitstream = bytes((i * 11 + 5) & 0xFF for i in range(size))
        entry = self._feed(bitstream, drop=drop)
        assert not entry.is_complete
        assert entry.is_recoverable
        rebuilt = recover_missing_body(entry)
        assert rebuilt is not None
        entry.bodies[drop] = rebuilt
        assert assemble(entry) == (bitstream, META)

    def test_the_last_short_chunk_is_recoverable(self):
        """The length XOR exists precisely so the short tail can come back."""
        bitstream = bytes(200)  # chunk 1 is 43 bytes, not full
        entry = self._feed(bitstream, drop=1)
        rebuilt = recover_missing_body(entry)
        assert rebuilt is not None and len(rebuilt) == 200 - CHUNK_ZERO_IMAGE_BYTES
        entry.bodies[1] = rebuilt
        assert assemble(entry) == (bitstream, META)

    def test_two_losses_are_not_recoverable(self):
        entry = self._feed(bytes(600), drop=0)
        entry.bodies.pop(1, None)
        assert not entry.is_recoverable
        assert recover_missing_body(entry) is None

    def test_a_corrupt_parity_length_is_rejected_not_truncated(self):
        """Upstream's guard. Without it a flipped bit yields a SHORT image
        reported as complete, which is worse than failing: the caller cannot
        tell the bytes are wrong."""
        entry = self._feed(bytes(600), drop=0)
        assert entry.parity_body is not None
        corrupted = bytearray(entry.parity_body)
        corrupted[0] = (corrupted[0] - 1) & 0xFF  # not a full-length chunk 0
        entry.parity_body = bytes(corrupted)
        assert recover_missing_body(entry) is None

    def test_assemble_waits_when_chunk_zero_is_missing(self):
        entry = self._feed(bytes(300))
        entry.bodies.pop(0)
        assert assemble(entry) is None


class TestCompanionFrames:
    def test_send_command_layout(self):
        frame = build_send_command(3, DATA_TYPE_AEIC_IMAGE, b"blob")
        assert frame[0] == CMD_SEND_CHANNEL_DATA
        assert frame[1] == 3
        assert frame[2] == OUT_PATH_UNKNOWN
        # Data type is little-endian on the wire.
        assert frame[3] == DATA_TYPE_AEIC_IMAGE & 0xFF
        assert frame[4] == (DATA_TYPE_AEIC_IMAGE >> 8) & 0xFF
        assert frame[5:] == b"blob"

    def test_send_command_refuses_an_oversized_blob(self):
        with pytest.raises(ChannelDataFormatError, match="exceeds"):
            build_send_command(0, DATA_TYPE_AEIC_IMAGE, bytes(BLOB_BYTES + 1))

    def test_inbound_frame_parsing(self):
        payload = b"chunkbytes"
        frame = (
            bytes(
                [
                    RESP_CODE_CHANNEL_DATA_RECV,
                    0xF0,  # SNR, signed -16 -> -4.0 dB
                    0,
                    0,
                    2,  # channel index
                    0xFF,  # direct path
                    DATA_TYPE_AEIC_IMAGE & 0xFF,
                    (DATA_TYPE_AEIC_IMAGE >> 8) & 0xFF,
                    len(payload),
                ]
            )
            + payload
        )
        parsed = parse_channel_data_frame(frame)
        assert parsed is not None
        assert parsed.channel_index == 2
        assert parsed.data_type == DATA_TYPE_AEIC_IMAGE
        assert parsed.payload == payload
        assert parsed.snr_db == -4.0
        assert not parsed.arrived_by_flood

    @pytest.mark.parametrize(
        "frame",
        [
            pytest.param(b"", id="empty"),
            pytest.param(bytes([26, 0, 0, 0, 0, 0, 0, 0, 0]), id="a different resp code"),
            pytest.param(bytes([RESP_CODE_CHANNEL_DATA_RECV, 0, 0, 0, 0, 0, 0, 0, 9]), id="short"),
        ],
    )
    def test_non_frames_return_none(self, frame):
        assert parse_channel_data_frame(frame) is None
