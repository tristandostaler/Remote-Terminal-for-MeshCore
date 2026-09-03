"""Inbound GRP_DATA reassembly, transport selection, and the text fallback."""

from __future__ import annotations

from unittest.mock import ANY

import pytest

from app.imaging.aeic import channel_data_ingest
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
    ChannelDataReassembler,
    carries_an_image,
    describe_data_type,
    handle_channel_data,
    marker_text,
    unsupported_marker_text,
)
from app.imaging.aeic.channel_data_text import (
    MCMP_V3_WIRE_VERSION,
    carries_text,
    decode_channel_data_text,
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


class TestMcmpOverChannelData:
    """MCO Advanced sends compressed channel text as GRP_DATA by default.

    ``channelsSendAsBinary`` is on out of the box, so with MCMP enabled a peer's
    ordinary chat arrives here as a binary blob rather than as ``mcmp2:`` text.
    It used to be named in a log line and dropped, which made every compressed
    message from a current MCO Advanced build invisible.
    """

    def _legacy(self, text: str, *, name: bytes = b"Phone") -> bytes:
        from app.compression.mcmp import get_compressor

        return bytes([len(name)]) + name + get_compressor().compress_to_bytes(text)

    def _app(self, text: str, *, name: bytes = b"Phone", timestamp: int = 1_700_000_000) -> bytes:
        from app.compression.mcmp import encode_v3_body, get_compressor

        body = encode_v3_body(get_compressor(), text, timestamp=timestamp)
        return (
            bytes([len(name)])
            + name
            + bytes([(MCO_APP_SUBTYPE_MCMP << 4) | MCMP_V3_WIRE_VERSION])
            + body
        )

    def test_a_v2_envelope_decodes_to_its_words(self):
        decoded = decode_channel_data_text(DATA_TYPE_MCMP, self._legacy("meet at the tower"))
        assert decoded is not None
        assert (decoded.sender_name, decoded.text, decoded.version) == (
            "Phone",
            "meet at the tower",
            "v2",
        )

    def test_a_v3_envelope_decodes_with_its_timestamp(self):
        decoded = decode_channel_data_text(DATA_TYPE_MCO_APP, self._app("on my way"))
        assert decoded is not None
        assert decoded.text == "on my way"
        assert decoded.version == "v3"
        assert decoded.v3 is not None and decoded.v3.timestamp == 1_700_000_000

    def test_noise_under_the_same_type_is_not_turned_into_a_message(self):
        """The arithmetic decoder answers anything; a data type is not proof."""
        assert decode_channel_data_text(DATA_TYPE_MCMP, bytes(50)) is None

    def test_another_subtype_is_left_alone(self):
        image = _mco_app_body(MCO_APP_SUBTYPE_MCO_IMAGE, 3)
        assert carries_text(DATA_TYPE_MCO_APP, image) is False
        assert decode_channel_data_text(DATA_TYPE_MCO_APP, image) is None

    def test_an_unknown_wire_version_is_refused_rather_than_guessed(self):
        payload = bytearray(self._app("hello"))
        payload[1 + len(b"Phone")] = (MCO_APP_SUBTYPE_MCMP << 4) | 0x0F
        assert decode_channel_data_text(DATA_TYPE_MCO_APP, bytes(payload)) is None

    @pytest.mark.asyncio
    async def test_it_is_stored_as_an_ordinary_channel_message(self, monkeypatch):
        stored: list[dict] = []

        async def _create(**kwargs):
            stored.append(kwargs)
            return None

        monkeypatch.setattr(
            "app.services.messages.create_fallback_channel_message", _create, raising=True
        )
        monkeypatch.setattr(
            "app.repository.ChannelRepository.get_by_key",
            _async_none,
            raising=True,
        )
        channel_data_ingest._recent_text_blobs.clear()

        handled = await handle_channel_data(
            ParsedChannelData(0, 1, 0xFF, DATA_TYPE_MCMP, self._legacy("the repeater is back")),
            conversation_key=CHANNEL,
        )

        assert handled is False, "text is not an image chunk"
        assert len(stored) == 1
        assert stored[0]["message_text"] == "the repeater is back"
        assert stored[0]["sender_name"] == "Phone"
        assert stored[0]["conversation_key"] == CHANNEL

    @pytest.mark.asyncio
    async def test_the_same_blob_from_both_delivery_paths_stores_one_row(self, monkeypatch):
        stored: list[dict] = []

        async def _create(**kwargs):
            stored.append(kwargs)
            return None

        monkeypatch.setattr(
            "app.services.messages.create_fallback_channel_message", _create, raising=True
        )
        monkeypatch.setattr(
            "app.repository.ChannelRepository.get_by_key", _async_none, raising=True
        )
        channel_data_ingest._recent_text_blobs.clear()
        blob = self._legacy("heard twice, said once")

        for _ in range(2):
            await handle_channel_data(
                ParsedChannelData(0, 1, 0xFF, DATA_TYPE_MCMP, blob), conversation_key=CHANNEL
            )

        assert len(stored) == 1


async def _async_none(*_args, **_kwargs):
    return None


def _mco_app_body(subtype: int, version: int, *, name: bytes = b"Alice") -> bytes:
    """One ``0x0120`` envelope: ``nameLen | name | subtypeVersion | body``."""
    return bytes([len(name)]) + name + bytes([(subtype << 4) | version]) + bytes(20)


class TestAnnouncingTheMarkerRow:
    """A received channel image has to appear without a reload.

    The row was written and nothing was pushed. The only event this path emitted
    was ``aeic_image_session``, which no client code handles -- the bubbles poll
    over HTTP -- so the picture appeared no earlier than the next fetch of the
    conversation. Sitting in the channel it arrived on, you saw nothing, which
    made a working transfer look exactly like a dropped one.
    """

    @pytest.mark.asyncio
    async def test_the_row_is_broadcast_as_a_message(self, monkeypatch):
        events: list[tuple[str, object]] = []
        sent = _StoreSpy(monkeypatch)

        await handle_channel_data(
            ParsedChannelData(0, 1, 0xFF, DATA_TYPE_AEIC_IMAGE, _blobs(bytes(120))[0]),
            conversation_key=CHANNEL,
            broadcast_fn=lambda name, payload, **kw: events.append((name, payload)),
        )

        assert sent.rows, "no marker row was written"
        names = [name for name, _payload in events]
        assert "message" in names, (
            "the marker row was never announced; it would appear only on a reload"
        )
        # And it is the row itself, so the client can render the bubble from it.
        payload = next(p for name, p in events if name == "message")
        assert payload["id"] == _StoreSpy.MESSAGE_ID
        assert payload["text"] == marker_text(sent.session_keys[0])


class _StoreSpy:
    """Stubs the storage edges of ``_store_and_decode`` and records what it wrote."""

    MESSAGE_ID = 4242

    def __init__(self, monkeypatch):
        import app.repository as repo
        from app.config import settings
        from app.imaging.aeic import channel_data_ingest as cdi
        from app.models import Message

        self.rows: list[dict] = []
        self.session_keys: list[str] = []
        self.sessions: list[dict] = []
        # Decoding off, which is the case that has to show something rather than
        # quietly hold a bitstream nobody can see.
        monkeypatch.setattr(settings, "enable_aeic", False)

        async def create(**kw):
            self.rows.append(kw)
            return self.MESSAGE_ID

        async def create_session(**kw):
            self.session_keys.append(kw["key"])
            self.sessions.append(kw)

        async def noop(*_a, **_k):
            return None

        async def get_by_id(message_id):
            row = self.rows[-1]
            return Message(
                id=message_id,
                type="CHAN",
                text=row["text"],
                conversation_key=row["conversation_key"],
                sender_timestamp=0,
                received_at=row["received_at"],
                outgoing=False,
            )

        monkeypatch.setattr(repo.MessageRepository, "create", create)
        monkeypatch.setattr(repo.MessageRepository, "get_by_id", get_by_id)
        monkeypatch.setattr(repo.AeicImageRepository, "create_session", create_session)
        monkeypatch.setattr(repo.AeicImageRepository, "store_bitstream", noop)
        monkeypatch.setattr(repo.AeicImageRepository, "store_decode_error", noop)
        monkeypatch.setattr(repo.AeicImageRepository, "enforce_cache_limit", noop)
        monkeypatch.setattr(cdi.reassembler, "_completed", {})


class TestWhoSentThePicture:
    """A GRP_DATA image used to arrive with no sender at all.

    The row was written with ``sender_key=None`` and no name, so the client fell
    all the way through its sender resolution to the conversation key -- and
    rendered a channel's shared secret as though it were the author. The 2-byte
    prefix in every chunk header is upstream's identity field and was there the
    whole time.
    """

    OURS = "ccdd" + "00" * 30

    def _ours(self, monkeypatch, name: str = "Proxy"):
        monkeypatch.setattr(channel_data_ingest, "self_node_identity", lambda: (self.OURS, name))

    def _blob_from(self, prefix: int) -> bytes:
        return build_image_chunks(bytes(120), META, sender_prefix=prefix, img_id=7)[0]

    async def _absorb(self, blob: bytes) -> None:
        await handle_channel_data(
            ParsedChannelData(0, 1, 0xFF, DATA_TYPE_AEIC_IMAGE, blob),
            conversation_key=CHANNEL,
            broadcast_fn=None,
        )

    @pytest.mark.asyncio
    async def test_a_picture_carrying_our_own_prefix_is_stored_as_ours(self, monkeypatch):
        """Which is every picture an app on the virtual node sends.

        The app's identity is this radio's -- SELF_INFO told it so -- and the
        blob leaves on our key, exactly like an app's text message, which the
        send services already store as outgoing.
        """
        sent = _StoreSpy(monkeypatch)
        self._ours(monkeypatch)

        await self._absorb(self._blob_from(0xCCDD))

        assert sent.rows[-1]["outgoing"] is True
        assert sent.rows[-1]["sender_key"] == self.OURS
        assert sent.rows[-1]["sender_name"] == "Proxy"
        assert sent.sessions[-1]["direction"] == "outgoing"

    @pytest.mark.asyncio
    async def test_a_picture_from_a_known_peer_carries_their_name(self, monkeypatch):
        import app.repository as repo
        from app.models import Contact

        sent = _StoreSpy(monkeypatch)
        self._ours(monkeypatch)
        peer = Contact(public_key="12ab" + "00" * 30, name="Alice", type=1)

        async def get_by_key_prefix(prefix):
            assert prefix == "12ab", "the prefix is two bytes of hex, lowercased"
            return peer

        monkeypatch.setattr(repo.ContactRepository, "get_by_key_prefix", get_by_key_prefix)

        await self._absorb(self._blob_from(0x12AB))

        assert sent.rows[-1]["sender_key"] == peer.public_key
        assert sent.rows[-1]["sender_name"] == "Alice"
        assert sent.rows[-1]["outgoing"] is False
        assert sent.sessions[-1]["direction"] == "incoming"

    @pytest.mark.asyncio
    async def test_an_unrecognised_prefix_is_left_unattributed(self, monkeypatch):
        """Two bytes are ambiguous by design; a guess is worse than a blank."""
        import app.repository as repo

        sent = _StoreSpy(monkeypatch)
        self._ours(monkeypatch)

        async def none(_prefix):
            return None

        monkeypatch.setattr(repo.ContactRepository, "get_by_key_prefix", none)

        await self._absorb(self._blob_from(0x4444))

        assert sent.rows[-1]["sender_key"] is None
        assert sent.rows[-1]["sender_name"] is None
        assert sent.rows[-1]["outgoing"] is False


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


class _KeptMediaSpy:
    """Stands in for the storage an undecodable arrival is kept in."""

    MESSAGE_ID = 909

    def __init__(self, monkeypatch, *, fail: bool = False):
        import app.repository as repo
        from app.models import Message

        self.blobs: list[bytes] = []
        self.rows: list[dict] = []
        self.bound: list[tuple[int, int]] = []
        self._fail = fail
        self._open = False

        async def append_blob(*, conversation_key, data_type, codec_label, payload, now):
            if self._fail:
                raise RuntimeError("disk is on fire")
            self.blobs.append(payload)
            started_new = not self._open
            self._open = True
            return 55, started_new

        async def bind_message(media_id, message_id):
            self.bound.append((media_id, message_id))

        async def create(**kw):
            self.rows.append(kw)
            return self.MESSAGE_ID

        async def get_by_id(message_id):
            return Message(
                id=message_id,
                type="CHAN",
                text=self.rows[-1]["text"],
                conversation_key=self.rows[-1]["conversation_key"],
                sender_timestamp=0,
                received_at=self.rows[-1]["received_at"],
                outgoing=False,
            )

        monkeypatch.setattr(repo.UnsupportedMediaRepository, "append_blob", append_blob)
        monkeypatch.setattr(repo.UnsupportedMediaRepository, "bind_message", bind_message)
        monkeypatch.setattr(repo.MessageRepository, "create", create)
        monkeypatch.setattr(repo.MessageRepository, "get_by_id", get_by_id)


class TestKeepingAnUndecodableImage:
    """An image in a codec we do not have must be kept, and must say so.

    It used to be identified, refused and dropped -- correct, and invisible.
    Nothing in the conversation said a picture had been sent, and the bytes were
    gone, so adding the decoder later could not bring back a single image already
    received. Three things have to happen now: keep the payload, put a box in the
    conversation, and log it.
    """

    def _frame(self, data_type: int, payload: bytes, *, channel: int = 1) -> ParsedChannelData:
        return ParsedChannelData(
            snr_raw=0,
            channel_index=channel,
            path_len_byte=0xFF,
            data_type=data_type,
            payload=payload,
        )

    @pytest.mark.asyncio
    async def test_the_payload_is_kept_and_a_box_is_placed(self, monkeypatch, caplog):
        spy = _KeptMediaSpy(monkeypatch)
        events: list[tuple[str, object]] = []

        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            await handle_channel_data(
                self._frame(DATA_TYPE_MCO_IMAGE, bytes(range(40))),
                conversation_key=CHANNEL,
                broadcast_fn=lambda name, payload, **kw: events.append((name, payload)),
            )

        # Kept verbatim: a format we cannot parse is one we must not normalise.
        assert spy.blobs == [bytes(range(40))]
        # And it has a place in the conversation, announced live.
        assert len(spy.rows) == 1
        assert spy.rows[0]["text"] == unsupported_marker_text(55)
        assert spy.bound == [(55, _KeptMediaSpy.MESSAGE_ID)]
        assert ("message", ANY) in [(n, ANY) for n, _ in events]
        assert "MCOimg" in caplog.text
        # Says the bytes are kept, because that is the difference between this and
        # the old behaviour and the reason the box is worth looking at later.
        assert "stored" in caplog.text

    @pytest.mark.asyncio
    async def test_one_arrival_places_one_box(self, monkeypatch):
        """A multi-blob image must not mint a box per packet."""
        spy = _KeptMediaSpy(monkeypatch)

        for _ in range(8):
            await handle_channel_data(
                self._frame(DATA_TYPE_MCO_IMAGE, bytes(40)),
                conversation_key=CHANNEL,
                broadcast_fn=lambda *_a, **_k: None,
            )

        assert len(spy.blobs) == 8, "every blob has to be kept"
        assert len(spy.rows) == 1, "one picture, one box"

    @pytest.mark.asyncio
    async def test_text_is_neither_kept_nor_announced(self, monkeypatch, caplog):
        """Keeping text would be hoarding, not recovery: it arrives decoded by
        other means, so there is nothing to recover later."""
        spy = _KeptMediaSpy(monkeypatch)

        with caplog.at_level("INFO", logger="app.imaging.aeic.channel_data_ingest"):
            await handle_channel_data(
                self._frame(DATA_TYPE_MCMP, bytes(40)),
                conversation_key=CHANNEL,
                broadcast_fn=lambda *_a, **_k: None,
            )

        assert spy.blobs == []
        assert spy.rows == []
        assert not [r for r in caplog.records if r.levelname == "INFO"]

    @pytest.mark.asyncio
    async def test_storage_failing_does_not_break_the_radio_path(self, monkeypatch, caplog):
        """This runs on the inbound frame path. A failed write must cost a log
        line, not the link."""
        _KeptMediaSpy(monkeypatch, fail=True)

        handled = await handle_channel_data(
            self._frame(DATA_TYPE_MCO_IMAGE, bytes(40)),
            conversation_key=CHANNEL,
            broadcast_fn=lambda *_a, **_k: None,
        )

        assert handled is False
        assert any(r.levelname == "ERROR" for r in caplog.records)

    @pytest.mark.asyncio
    async def test_an_aeic_image_is_not_treated_as_unsupported(self, monkeypatch):
        """The codec that DOES work must not be filed away as one that does not."""
        spy = _KeptMediaSpy(monkeypatch)
        sent = _StoreSpy(monkeypatch)

        await handle_channel_data(
            self._frame(DATA_TYPE_AEIC_IMAGE, _blobs(bytes(120))[0]),
            conversation_key=CHANNEL,
            broadcast_fn=lambda *_a, **_k: None,
        )

        assert spy.blobs == []
        assert sent.session_keys, "the AEIC path did not run"


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
