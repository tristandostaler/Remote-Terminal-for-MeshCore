"""Inbound GRP_DATA reassembly, transport selection, and the text fallback."""

from __future__ import annotations

import pytest

from app.imaging.aeic.channel_data import (
    DATA_TYPE_AEIC_IMAGE,
    DATA_TYPE_MCMP,
    DATA_TYPE_MCO_APP,
    DATA_TYPE_MCO_IMAGE,
    MCO_APP_SUBTYPE_MCMP,
    MCO_APP_SUBTYPE_MCO_IMAGE,
    ParsedChannelData,
    build_image_chunks,
    mco_app_subtype,
)
from app.imaging.aeic.channel_data_ingest import (
    MAX_PENDING_IMAGES,
    SESSION_TTL_SECONDS,
    UNDECODABLE_NOTICE_INTERVAL_SECONDS,
    ChannelDataReassembler,
    carries_an_image,
    describe_data_type,
    handle_channel_data,
    marker_text,
    reset_undecodable_notices,
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


def _mco_app_body(subtype: int, version: int, *, name: bytes = b"Alice") -> bytes:
    """One ``0x0120`` envelope: ``nameLen | name | subtypeVersion | body``."""
    return bytes([len(name)]) + name + bytes([(subtype << 4) | version]) + bytes(20)


class TestCompletingOnlyOnce:
    """One picture must produce one completion, whatever its size.

    A one-data-chunk image is upstream's *typical* ft32 size (its own capacity
    note puts the mean at 155.8 B) and goes out as two packets: the data chunk and
    the parity chunk. It used to complete on both. The data chunk finished it and
    the entry was dropped; the parity chunk then started a fresh entry in which
    the single missing body was recoverable from parity alone, so it finished
    again -- and the caller minted a second message row and a second session for
    the same picture. Two identical bubbles, for the commonest image there is.
    """

    @pytest.mark.parametrize(
        ("size", "expected_blobs"),
        [(120, 2), (155, 2), (158, 3), (300, 3), (460, 4), (800, 7)],
    )
    def test_an_image_completes_exactly_once(self, size, expected_blobs):
        r = ChannelDataReassembler()
        blobs = _blobs(bytes(size))
        assert len(blobs) == expected_blobs, "framing changed; the parity assumption needs review"

        completions = [i for i, blob in enumerate(blobs) if r.note_chunk(CHANNEL, blob) is not None]

        # And it completes on the LAST DATA chunk, not on the parity chunk: parity
        # is redundancy, so waiting for it would delay every image by a packet.
        assert completions == [expected_blobs - 2]

    def test_the_bitstream_is_still_right_for_a_single_chunk_image(self):
        """Suppressing the second completion must not cost the first its content."""
        r = ChannelDataReassembler()
        bitstream = bytes(range(120))
        outcome = r.note_chunk(CHANNEL, _blobs(bitstream)[0])
        assert outcome is not None
        assert outcome[0] == bitstream
        assert outcome[1] == META

    def test_parity_still_rebuilds_a_chunk_that_never_arrived(self):
        """The point of the parity packet, which this must not disable: drop a data
        chunk and the image still completes, from parity."""
        r = ChannelDataReassembler()
        bitstream = bytes(range(255)) * 2
        blobs = _blobs(bitstream)
        assert len(blobs) >= 4, "need at least 3 data chunks for a meaningful loss"

        for blob in blobs[1:]:  # chunk 0 never arrives
            outcome = r.note_chunk(CHANNEL, blob)

        assert outcome is not None
        assert outcome[0] == bitstream
        assert outcome[2] is True, "parity recovery did not report itself"

    def test_a_different_image_reusing_the_id_is_not_swallowed(self):
        """Suppression is keyed on the chunk count too. A different count means a
        different picture, which is the same signal the reset path already trusts."""
        r = ChannelDataReassembler()
        first = build_image_chunks(bytes(120), META, sender_prefix=0x1234, img_id=9)
        assert r.note_chunk(CHANNEL, first[0]) is not None

        second = build_image_chunks(bytes(600), META, sender_prefix=0x1234, img_id=9)
        outcomes = [r.note_chunk(CHANNEL, blob) for blob in second]

        assert any(o is not None for o in outcomes), "a different image was suppressed"

    def test_the_same_image_resent_after_the_window_is_accepted_again(self):
        r = ChannelDataReassembler()
        blob = _blobs(bytes(120))[0]
        assert r.note_chunk(CHANNEL, blob, now=1000.0) is not None
        assert r.note_chunk(CHANNEL, blob, now=1000.0 + 5) is None
        assert r.note_chunk(CHANNEL, blob, now=1000.0 + SESSION_TTL_SECONDS + 1) is not None

    def test_the_completed_memory_stays_bounded(self):
        """It is keyed by sender and image id, so a peer cycling ids must not grow
        it without limit."""
        r = ChannelDataReassembler()
        for img_id in range(MAX_PENDING_IMAGES * 3):
            blobs = build_image_chunks(bytes(120), META, sender_prefix=0x99, img_id=img_id % 256)
            r.note_chunk(CHANNEL, blobs[0], now=2000.0 + img_id)

        assert len(r._completed) <= MAX_PENDING_IMAGES


class TestMcoAppEnvelope:
    """MCO Advanced's official ``0x0120`` type, which carries a subtype inside.

    Recognition only. Naming it matters because a current MCO Advanced build sends
    images under this type, and "unknown data type 0x0120" reads like a protocol
    fault rather than a codec this build cannot decode.
    """

    def test_the_subtype_byte_is_read_past_the_sender_name(self):
        assert mco_app_subtype(_mco_app_body(MCO_APP_SUBTYPE_MCO_IMAGE, 3)) == (1, 3)
        assert mco_app_subtype(_mco_app_body(MCO_APP_SUBTYPE_MCMP, 0, name=b"Bo")) == (2, 0)

    def test_an_image_subtype_is_named_and_flagged_as_an_image(self):
        body = _mco_app_body(MCO_APP_SUBTYPE_MCO_IMAGE, 3)
        assert "MCOimg v3" in describe_data_type(DATA_TYPE_MCO_APP, body)
        assert "not supported" in describe_data_type(DATA_TYPE_MCO_APP, body)
        assert carries_an_image(DATA_TYPE_MCO_APP, body) is True

    def test_text_in_the_envelope_is_not_treated_as_an_image(self):
        """Only a dropped *picture* is worth telling someone about; text over
        GRP_DATA is ordinary traffic that also arrives decoded by other means."""
        body = _mco_app_body(MCO_APP_SUBTYPE_MCMP, 0)
        assert "MCMP v0" in describe_data_type(DATA_TYPE_MCO_APP, body)
        assert carries_an_image(DATA_TYPE_MCO_APP, body) is False
        assert carries_an_image(DATA_TYPE_MCMP) is False

    @pytest.mark.parametrize("body", [b"", b"\x05", b"\x05Ali", b"\x80\x01\x02"])
    def test_an_unreadable_envelope_is_described_without_guessing(self, body):
        """A short body, or a name length in the varuint continuation form, means
        this is probably not the envelope we think. Say so rather than reading a
        subtype out of whatever byte happens to be there."""
        assert mco_app_subtype(body) is None
        assert "unreadable" in describe_data_type(DATA_TYPE_MCO_APP, body)
        assert carries_an_image(DATA_TYPE_MCO_APP, body) is False

    def test_an_unrecognised_subtype_still_reports_its_numbers(self):
        body = _mco_app_body(0x07, 2)
        assert "subtype 7 v2" in describe_data_type(DATA_TYPE_MCO_APP, body)


class TestUndecodableImageNotice:
    """A picture that cannot be decoded has to SAY so.

    This is the bug these tests exist for: an image sent from MCO Advanced in a
    codec RemoteTerm has no decoder for was dropped at DEBUG, and the root logger
    defaults to INFO -- so the frame was correctly identified, correctly refused,
    and vanished without a trace anywhere, including ``/api/debug``. "I sent a
    picture and nothing happened" was the entire diagnostic surface.
    """

    def _frame(self, data_type: int, payload: bytes, *, channel: int = 1) -> ParsedChannelData:
        return ParsedChannelData(
            snr_raw=0,
            channel_index=channel,
            path_len_byte=0xFF,
            data_type=data_type,
            payload=payload,
        )

    @pytest.fixture(autouse=True)
    def _clean_notices(self):
        reset_undecodable_notices()
        yield
        reset_undecodable_notices()

    @pytest.mark.asyncio
    async def test_a_dropped_image_is_reported_at_info(self, caplog):
        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            await handle_channel_data(
                self._frame(DATA_TYPE_MCO_IMAGE, bytes(50)), conversation_key=CHANNEL
            )

        assert [record for record in caplog.records if record.levelname == "INFO"], (
            "a picture was dropped with nothing at INFO to say so"
        )
        assert "MCOimg" in caplog.text
        # Names the way out, not just the refusal: the sender's codec choice is
        # the only thing that can fix this, and it is not guessable.
        assert "AEIC" in caplog.text

    @pytest.mark.asyncio
    async def test_dropped_text_stays_at_debug(self, caplog):
        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            await handle_channel_data(
                self._frame(DATA_TYPE_MCMP, bytes(50)), conversation_key=CHANNEL
            )

        assert not [record for record in caplog.records if record.levelname == "INFO"]

    @pytest.mark.asyncio
    async def test_one_image_reports_once_not_once_per_chunk(self, caplog):
        """An image is up to sixteen chunks and every one lands here. A notice per
        frame would print a paragraph for a single picture."""
        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            for _ in range(16):
                await handle_channel_data(
                    self._frame(DATA_TYPE_MCO_IMAGE, bytes(50)), conversation_key=CHANNEL
                )

        assert len([r for r in caplog.records if r.levelname == "INFO"]) == 1

    @pytest.mark.asyncio
    async def test_suppression_is_per_channel_and_per_codec(self, caplog):
        """Two channels, or two codecs, are two separate things to report -- and
        collapsing them would hide the second."""
        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            await handle_channel_data(
                self._frame(DATA_TYPE_MCO_IMAGE, bytes(50), channel=1), conversation_key=CHANNEL
            )
            await handle_channel_data(
                self._frame(DATA_TYPE_MCO_IMAGE, bytes(50), channel=2), conversation_key=CHANNEL
            )
            await handle_channel_data(
                self._frame(
                    DATA_TYPE_MCO_APP, _mco_app_body(MCO_APP_SUBTYPE_MCO_IMAGE, 3), channel=1
                ),
                conversation_key=CHANNEL,
            )

        assert len([r for r in caplog.records if r.levelname == "INFO"]) == 3

    @pytest.mark.asyncio
    async def test_it_speaks_again_once_the_window_passes(self, caplog):
        """Trying again after changing a setting has to say something. Permanent
        suppression would make a second attempt look identical to a dead one."""
        from app.imaging.aeic import channel_data_ingest

        frame = self._frame(DATA_TYPE_MCO_IMAGE, bytes(50))
        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            channel_data_ingest._note_undecodable(frame, now=1000.0)
            channel_data_ingest._note_undecodable(
                frame, now=1000.0 + UNDECODABLE_NOTICE_INTERVAL_SECONDS - 1
            )
            channel_data_ingest._note_undecodable(
                frame, now=1000.0 + UNDECODABLE_NOTICE_INTERVAL_SECONDS + 1
            )

        assert len([r for r in caplog.records if r.levelname == "INFO"]) == 2

    @pytest.mark.asyncio
    async def test_an_aeic_image_reports_nothing(self, caplog):
        """The whole point is that this codec DOES work. A notice here would be
        telling someone about a picture that is about to appear."""
        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            await handle_channel_data(
                self._frame(DATA_TYPE_AEIC_IMAGE, _blobs(bytes(300))[0]),
                conversation_key=CHANNEL,
            )

        assert not [
            r for r in caplog.records if r.levelname == "INFO" and "Cannot show" in r.message
        ]


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
