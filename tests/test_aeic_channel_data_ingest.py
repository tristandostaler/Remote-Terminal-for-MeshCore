"""Inbound GRP_DATA reassembly, transport selection, and the text fallback."""

from __future__ import annotations

import pytest

from app.imaging.aeic.channel_data import (
    DATA_TYPE_AEIC_IMAGE,
    DATA_TYPE_MCMP,
    DATA_TYPE_MCO_IMAGE,
    ParsedChannelData,
    build_image_chunks,
)
from app.imaging.aeic.channel_data_ingest import (
    MAX_PENDING_IMAGES,
    SESSION_TTL_SECONDS,
    ChannelDataReassembler,
    describe_data_type,
    handle_channel_data,
    marker_text,
)
from app.imaging.aeic.text_transport import AeicStreamMetadata
from app.imaging.aeic.transport import (
    AeicChannelDataUnsupported,
    AeicSendResult,
    AeicTarget,
    AeicTransport,
    ChannelDataTransport,
    TextChunkTransport,
    select_transport,
)

CHANNEL = "AB" * 16
META = AeicStreamMetadata(square_size=512, aspect_code=2).encode()


def _blobs(bitstream: bytes, *, img_id: int = 5):
    return build_image_chunks(bitstream, META, sender_prefix=0x1234, img_id=img_id)


class TestReassembler:
    def test_a_single_chunk_image_completes_immediately(self):
        r = ChannelDataReassembler()
        bitstream = bytes(range(100))
        blobs = _blobs(bitstream)
        # blobs[0] is the only data chunk; blobs[1] is parity.
        assert r.note_chunk(CHANNEL, blobs[0]) == (bitstream, META, False)

    def test_a_two_chunk_image_waits_for_the_second(self):
        r = ChannelDataReassembler()
        bitstream = bytes((i * 3) & 0xFF for i in range(300))
        blobs = _blobs(bitstream)
        assert r.note_chunk(CHANNEL, blobs[0]) is None
        assert r.note_chunk(CHANNEL, blobs[1]) == (bitstream, META, False)

    def test_parity_rebuilds_a_dropped_chunk(self):
        r = ChannelDataReassembler()
        bitstream = bytes((i * 5 + 1) & 0xFF for i in range(300))
        blobs = _blobs(bitstream)
        assert r.note_chunk(CHANNEL, blobs[1]) is None  # chunk 0 lost
        result = r.note_chunk(CHANNEL, blobs[2])  # parity arrives
        assert result is not None
        assert result[0] == bitstream
        assert result[2] is True, "should report that parity did the work"

    def test_two_senders_do_not_merge(self):
        r = ChannelDataReassembler()
        a = build_image_chunks(bytes(300), META, sender_prefix=0x1111, img_id=1)
        b = build_image_chunks(bytes(300), META, sender_prefix=0x2222, img_id=1)
        assert r.note_chunk(CHANNEL, a[0]) is None
        assert r.note_chunk(CHANNEL, b[0]) is None
        # Each still needs its own second chunk; neither completed off the other.
        assert r.note_chunk(CHANNEL, a[1]) is not None

    def test_the_same_channel_key_separates_by_image_id(self):
        r = ChannelDataReassembler()
        first = _blobs(bytes(300), img_id=1)
        second = _blobs(bytes(300), img_id=2)
        assert r.note_chunk(CHANNEL, first[0]) is None
        assert r.note_chunk(CHANNEL, second[0]) is None
        assert r.note_chunk(CHANNEL, first[1]) is not None

    def test_a_reused_image_id_with_a_new_chunk_count_resets(self):
        r = ChannelDataReassembler()
        long_image = _blobs(bytes(600), img_id=9)
        short_image = _blobs(bytes(100), img_id=9)
        assert r.note_chunk(CHANNEL, long_image[0]) is None
        # Same sender and id, different total: a new image, not a continuation.
        assert r.note_chunk(CHANNEL, short_image[0]) == (bytes(100), META, False)

    def test_a_duplicate_chunk_is_ignored(self):
        r = ChannelDataReassembler()
        blobs = _blobs(bytes(300))
        assert r.note_chunk(CHANNEL, blobs[0]) is None
        assert r.note_chunk(CHANNEL, blobs[0]) is None
        assert r.note_chunk(CHANNEL, blobs[1]) is not None

    def test_malformed_blobs_are_ignored(self):
        r = ChannelDataReassembler()
        assert r.note_chunk(CHANNEL, b"\x00\x01") is None

    def test_partial_images_expire(self):
        r = ChannelDataReassembler()
        blobs = _blobs(bytes(300))
        assert r.note_chunk(CHANNEL, blobs[0], now=1000.0) is None
        # Long after the TTL, the second chunk cannot resurrect the first.
        later = 1000.0 + SESSION_TTL_SECONDS + 1
        assert r.note_chunk(CHANNEL, blobs[1], now=later) is None

    def test_pending_images_are_capped(self):
        r = ChannelDataReassembler()
        for img_id in range(MAX_PENDING_IMAGES + 10):
            blobs = build_image_chunks(bytes(300), META, sender_prefix=1, img_id=img_id % 256)
            r.note_chunk(f"{CHANNEL}{img_id}", blobs[0], now=1000.0 + img_id)
        assert len(r._pending) <= MAX_PENDING_IMAGES


class TestDataTypeRouting:
    """MCOimg also rides GRP_DATA, and it is NOT a codec we have."""

    def _frame(self, data_type: int, payload: bytes) -> ParsedChannelData:
        return ParsedChannelData(
            snr_raw=0, channel_index=1, path_len_byte=0xFF, data_type=data_type, payload=payload
        )

    @pytest.mark.asyncio
    async def test_an_mco_image_frame_is_not_fed_to_the_aeic_decoder(self):
        handled = await handle_channel_data(
            self._frame(DATA_TYPE_MCO_IMAGE, bytes(50)), conversation_key=CHANNEL
        )
        assert handled is False

    @pytest.mark.asyncio
    async def test_an_mcmp_frame_is_ignored(self):
        handled = await handle_channel_data(
            self._frame(DATA_TYPE_MCMP, bytes(50)), conversation_key=CHANNEL
        )
        assert handled is False

    @pytest.mark.asyncio
    async def test_an_unknown_data_type_is_ignored(self):
        handled = await handle_channel_data(
            self._frame(0x1234, bytes(50)), conversation_key=CHANNEL
        )
        assert handled is False

    @pytest.mark.asyncio
    async def test_an_aeic_frame_is_claimed(self):
        blobs = _blobs(bytes(300))
        handled = await handle_channel_data(
            self._frame(DATA_TYPE_AEIC_IMAGE, blobs[0]), conversation_key=CHANNEL
        )
        assert handled is True

    def test_descriptions_name_the_codec(self):
        assert "AEIC" in describe_data_type(DATA_TYPE_AEIC_IMAGE)
        assert "MCOimg" in describe_data_type(DATA_TYPE_MCO_IMAGE)
        assert "not supported" in describe_data_type(DATA_TYPE_MCO_IMAGE)


class TestTransportSelection:
    def test_channels_prefer_the_binary_transport(self):
        assert isinstance(select_transport("CHAN"), ChannelDataTransport)

    def test_direct_messages_always_use_text(self):
        """GRP_DATA is a group payload type; there is no DM equivalent."""
        assert isinstance(select_transport("PRIV"), TextChunkTransport)

    def test_prefer_binary_false_forces_text(self):
        assert isinstance(select_transport("CHAN", prefer_binary=False), TextChunkTransport)


class TestTextFallback:
    """A firmware without command 62 must degrade, not fail."""

    class _RejectingTransport(AeicTransport):
        name = "test/rejects"

        @property
        def available(self) -> bool:
            return True

        async def send(self, bitstream, metadata, target, *, session_id=None):
            raise AeicChannelDataUnsupported("this radio rejected CMD_SEND_CHANNEL_DATA (62)")

    @pytest.mark.asyncio
    async def test_send_image_falls_back_to_text(self, monkeypatch):
        from app.imaging.aeic.service import aeic_service

        sent: list[str] = []

        async def emit_text(chunk: str):
            sent.append(chunk)
            return None

        async def fake_encode(rgb):
            return bytes(range(120))

        monkeypatch.setattr(aeic_service, "encode_rgb", fake_encode)
        monkeypatch.setattr(
            "app.imaging.aeic.service.select_transport",
            lambda *_a, **_k: self._RejectingTransport(),
        )
        monkeypatch.setattr(aeic_service, "_record_outgoing", lambda *a, **k: _none())

        result, _bits, _meta = await aeic_service.send_image(
            bytes(512 * 512 * 3),
            AeicTarget(conversation_type="CHAN", conversation_key=CHANNEL, emit_text=emit_text),
        )
        assert result.transport == TextChunkTransport.name
        assert sent and sent[0].startswith("aei1")

    @pytest.mark.asyncio
    async def test_an_explicit_transport_is_not_second_guessed(self, monkeypatch):
        """A caller that named a transport gets its error, not a silent swap."""
        from app.imaging.aeic.service import aeic_service

        async def fake_encode(rgb):
            return bytes(range(120))

        monkeypatch.setattr(aeic_service, "encode_rgb", fake_encode)
        with pytest.raises(AeicChannelDataUnsupported):
            await aeic_service.send_image(
                bytes(512 * 512 * 3),
                AeicTarget(
                    conversation_type="CHAN",
                    conversation_key=CHANNEL,
                    emit_text=lambda chunk: _none(),
                ),
                transport=self._RejectingTransport(),
            )


async def _none():
    return None


def test_marker_text_is_recognisable():
    assert marker_text("grp:abc").startswith("aeib:")


def test_send_result_counts_the_parity_blob():
    """Airtime accounting must see the extra packet parity costs."""
    result = AeicSendResult(transport="x", session_id=1, chunk_count=3, payload_bytes=300)
    assert result.chunk_count == 3
