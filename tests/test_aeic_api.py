"""The AEIC selector endpoint, session storage, and inbound chunk reassembly.

None of these need the model bundle or onnxruntime: the codec is behind an
availability gate, and everything below the gate -- the per-conversation
selector, the session tables, and reassembling ``aei1:`` chunks back into a
bitstream -- is what this file covers.

The reassembly tests matter disproportionately. There are three message ingest
routes, and an image whose chunks arrive on a route that does not feed the
reassembler simply never appears, with nothing logged as an error.
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

from app.compression import decode_base91
from app.imaging.aeic.ingest import aeic_body, note_inbound_chunk
from app.imaging.aeic.service import aeic_service
from app.imaging.aeic.text_transport import AeicStreamMetadata, aspect_code_for, encode_chunks
from app.models import ImageCodecSelectionRequest
from app.repository import (
    AeicImageRepository,
    ChannelRepository,
    ContactRepository,
    MessageRepository,
)
from app.repository.aeic_image import (
    OUTGOING_PREFIX,
    outgoing_session_key,
)
from app.repository.aeic_image import session_key as make_session_key
from app.routers.settings import set_image_codec

PEER = "aa" * 32
CHANNEL_KEY = "AB" * 16


def _chunks(payload: bytes, *, session_id: int = 7, width: int = 4032, height: int = 3024):
    metadata = AeicStreamMetadata(square_size=512, aspect_code=aspect_code_for(width, height))
    return encode_chunks(payload, metadata, session_id=session_id)


async def _store_message(text: str) -> int:
    """Persist a real message row and return its id.

    A session's ``message_id`` is a foreign key into ``messages``, so the tests
    create actual rows rather than inventing ids. That also mirrors production
    ordering: all three ingest routes store the message first and only then feed
    the reassembler.
    """
    message_id = await MessageRepository.create(
        msg_type="PRIV",
        text=text,
        received_at=1_700_000_000,
        conversation_key=PEER,
        sender_key=PEER,
    )
    assert message_id is not None
    return message_id


class TestCodecSelectionEndpoint:
    @pytest.mark.asyncio
    async def test_defaults_to_ie4_for_a_new_contact(self, test_db):
        await ContactRepository.upsert({"public_key": PEER, "name": "Alice"})
        contact = await ContactRepository.get_by_key(PEER)
        assert contact is not None and contact.image_codec == "ie4"

    @pytest.mark.asyncio
    async def test_sets_ie4_explicitly_on_a_contact(self, test_db):
        await ContactRepository.upsert({"public_key": PEER, "name": "Alice"})
        response = await set_image_codec(
            ImageCodecSelectionRequest(type="contact", id=PEER, codec="ie4")
        )
        assert response.codec == "ie4"
        contact = await ContactRepository.get_by_key(PEER)
        assert contact is not None and contact.image_codec == "ie4"

    @pytest.mark.asyncio
    async def test_sets_ie4_explicitly_on_a_channel(self, test_db):
        await ChannelRepository.upsert(CHANNEL_KEY, "Test")
        response = await set_image_codec(
            ImageCodecSelectionRequest(type="channel", id=CHANNEL_KEY, codec="ie4")
        )
        assert response.codec == "ie4"
        channel = await ChannelRepository.get_by_key(CHANNEL_KEY)
        assert channel is not None and channel.image_codec == "ie4"

    @pytest.mark.asyncio
    async def test_missing_contact_returns_404(self, test_db):
        with pytest.raises(HTTPException) as excinfo:
            await set_image_codec(
                ImageCodecSelectionRequest(type="contact", id="ff" * 32, codec="ie4")
            )
        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_channel_returns_404(self, test_db):
        with pytest.raises(HTTPException) as excinfo:
            await set_image_codec(
                ImageCodecSelectionRequest(type="channel", id="CD" * 16, codec="ie4")
            )
        assert excinfo.value.status_code == 404

    @pytest.mark.asyncio
    async def test_selecting_aeic_without_the_model_is_refused_with_a_reason(self, test_db):
        """A 503 carrying the sentence to show the user, rather than a silent
        selection that fails at the first send."""
        from app.imaging.aeic.service import aeic_service

        await ContactRepository.upsert({"public_key": PEER, "name": "Alice"})
        if aeic_service.unavailable_reason(for_decode=False) is None:
            pytest.skip("the AEIC bundle is installed on this machine, so it is selectable")
        with pytest.raises(HTTPException) as excinfo:
            await set_image_codec(ImageCodecSelectionRequest(type="contact", id=PEER, codec="aeic"))
        assert excinfo.value.status_code == 503
        assert excinfo.value.detail


class TestBodyExtraction:
    def test_strips_a_channel_sender_prefix(self):
        assert aeic_body("Alice: aei1000100xyz", "Alice") == "aei1000100xyz"

    def test_leaves_a_direct_message_alone(self):
        assert aeic_body("aei1000100xyz", None) == "aei1000100xyz"

    def test_leaves_text_that_does_not_start_with_the_prefix(self):
        assert aeic_body("Bob: aei1000100xyz", "Alice") == "Bob: aei1000100xyz"


class TestInboundReassembly:
    @pytest.mark.asyncio
    async def test_ignores_a_message_that_is_not_an_aeic_chunk(self, test_db):
        assert (
            await note_inbound_chunk(
                text="just a normal message",
                message_id=await _store_message("just a normal message"),
                conversation_type="PRIV",
                conversation_key=PEER,
                peer_public_key=PEER,
            )
            is False
        )

    @pytest.mark.asyncio
    async def test_a_single_chunk_image_reassembles_immediately(self, test_db):
        payload = os.urandom(117)
        chunks = _chunks(payload, session_id=11)
        assert len(chunks) == 1

        message_id = await _store_message(chunks[0])
        handled = await note_inbound_chunk(
            text=chunks[0],
            message_id=message_id,
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        assert handled is True

        session = await AeicImageRepository.get(make_session_key(PEER, 11))
        assert session is not None
        assert session["state"] == "complete"
        assert bytes(session["bitstream"]) == payload
        assert session["message_id"] == message_id

    @pytest.mark.asyncio
    async def test_a_two_chunk_image_reassembles_only_once_both_arrive(self, test_db):
        payload = os.urandom(209)
        chunks = _chunks(payload, session_id=12)
        assert len(chunks) == 2
        key = make_session_key(PEER, 12)

        first_message_id = await _store_message(chunks[0])
        await note_inbound_chunk(
            text=chunks[0],
            message_id=first_message_id,
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        partial = await AeicImageRepository.get(key)
        assert partial is not None
        assert partial["bitstream"] is None
        assert len(partial["chunks"]) == 1

        await note_inbound_chunk(
            text=chunks[1],
            message_id=await _store_message(chunks[1]),
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        complete = await AeicImageRepository.get(key)
        assert complete is not None
        assert bytes(complete["bitstream"]) == payload
        # The first message owns the bubble, so it keeps the message id.
        assert complete["message_id"] == first_message_id

    @pytest.mark.asyncio
    async def test_reassembles_regardless_of_chunk_order(self, test_db):
        payload = os.urandom(209)
        chunks = _chunks(payload, session_id=13)
        # Chunk 0 must still arrive: it carries the metadata byte. A later chunk
        # seen first is dropped, and the sender's chunk 0 opens the session.
        for text in (chunks[0], chunks[1]):
            await note_inbound_chunk(
                text=text,
                message_id=None,
                conversation_type="PRIV",
                conversation_key=PEER,
                peer_public_key=PEER,
            )
        session = await AeicImageRepository.get(make_session_key(PEER, 13))
        assert session is not None and bytes(session["bitstream"]) == payload

    @pytest.mark.asyncio
    async def test_a_continuation_chunk_before_chunk_zero_is_dropped(self, test_db):
        """Nothing to anchor it to: chunk 0 carries the metadata byte."""
        chunks = _chunks(os.urandom(209), session_id=14)
        handled = await note_inbound_chunk(
            text=chunks[1],
            message_id=await _store_message(chunks[1]),
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        assert handled is True  # recognised as ours, just not stored
        assert await AeicImageRepository.get(make_session_key(PEER, 14)) is None

    @pytest.mark.asyncio
    async def test_records_the_metadata_the_sender_declared(self, test_db):
        chunks = _chunks(os.urandom(117), session_id=15, width=1080, height=1920)
        await note_inbound_chunk(
            text=chunks[0],
            message_id=await _store_message(chunks[0]),
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        session = await AeicImageRepository.get(make_session_key(PEER, 15))
        assert session is not None
        assert session["square_size"] == 512
        assert session["aspect_code"] == aspect_code_for(1080, 1920)
        assert session["rate_code"] == 0

    @pytest.mark.asyncio
    async def test_a_channel_chunk_has_its_sender_prefix_stripped(self, test_db):
        payload = os.urandom(117)
        chunks = _chunks(payload, session_id=16)
        await note_inbound_chunk(
            text=f"Alice: {chunks[0]}",
            message_id=await _store_message(f"Alice: {chunks[0]}"),
            conversation_type="CHAN",
            conversation_key=CHANNEL_KEY,
            peer_public_key=PEER,
            sender_name="Alice",
        )
        session = await AeicImageRepository.get(make_session_key(PEER, 16))
        assert session is not None and bytes(session["bitstream"]) == payload

    @pytest.mark.asyncio
    async def test_two_senders_reusing_one_session_id_do_not_merge(self, test_db):
        """The wire session id is 2 base36 chars, so it is unique per SENDER.
        A merge would produce a corrupt image, which no lower layer can detect.
        """
        other = "bb" * 32
        first, second = os.urandom(117), os.urandom(117)
        await note_inbound_chunk(
            text=_chunks(first, session_id=20)[0],
            message_id=await _store_message(_chunks(first, session_id=20)[0]),
            conversation_type="CHAN",
            conversation_key=CHANNEL_KEY,
            peer_public_key=PEER,
        )
        await note_inbound_chunk(
            text=_chunks(second, session_id=20)[0],
            message_id=await _store_message(_chunks(second, session_id=20)[0]),
            conversation_type="CHAN",
            conversation_key=CHANNEL_KEY,
            peer_public_key=other,
        )
        mine = await AeicImageRepository.get(make_session_key(PEER, 20))
        theirs = await AeicImageRepository.get(make_session_key(other, 20))
        assert mine is not None and bytes(mine["bitstream"]) == first
        assert theirs is not None and bytes(theirs["bitstream"]) == second

    @pytest.mark.asyncio
    async def test_a_duplicate_chunk_is_stored_once(self, test_db):
        """The same message can reach us via raw RF and the get_msg drain."""
        payload = os.urandom(209)
        chunks = _chunks(payload, session_id=21)
        # One row per distinct chunk; replaying chunk 0 reuses its id, which is
        # exactly what happens when a message reaches us on two routes.
        ids = {
            chunks[0]: await _store_message(chunks[0]),
            chunks[1]: await _store_message(chunks[1]),
        }
        for text in (chunks[0], chunks[0], chunks[1]):
            await note_inbound_chunk(
                text=text,
                message_id=ids[text],
                conversation_type="PRIV",
                conversation_key=PEER,
                peer_public_key=PEER,
            )
        session = await AeicImageRepository.get(make_session_key(PEER, 21))
        assert session is not None
        assert len(session["chunks"]) == 2
        assert bytes(session["bitstream"]) == payload

    @pytest.mark.asyncio
    async def test_broadcasts_progress_for_each_chunk(self, test_db):
        events: list[tuple[str, dict]] = []
        chunks = _chunks(os.urandom(209), session_id=22)
        for text in chunks:
            await note_inbound_chunk(
                text=text,
                message_id=await _store_message(text),
                conversation_type="PRIV",
                conversation_key=PEER,
                peer_public_key=PEER,
                broadcast_fn=lambda kind, data: events.append((kind, data)),
            )
        assert [kind for kind, _ in events] == ["aeic_image_session"] * 2
        assert [data["received"] for _, data in events] == [1, 2]
        assert all(data["total"] == 2 for _, data in events)

    @pytest.mark.asyncio
    async def test_the_reassembled_bitstream_is_the_base91_of_the_joined_chunks(self, test_db):
        """basE91 is stateful across the stream, so the join must precede the
        decode. Decoding chunk by chunk would corrupt every boundary."""
        payload = os.urandom(209)
        chunks = _chunks(payload, session_id=23)
        for text in chunks:
            await note_inbound_chunk(
                text=text,
                message_id=await _store_message(text),
                conversation_type="PRIV",
                conversation_key=PEER,
                peer_public_key=PEER,
            )
        session = await AeicImageRepository.get(make_session_key(PEER, 23))
        assert session is not None
        joined = "".join(session["chunks"][i] for i in range(session["total_chunks"]))
        assert decode_base91(joined) == payload
        assert bytes(session["bitstream"]) == payload


async def _complete_session(session_id: int, payload: bytes | None = None) -> str:
    """Create a reassembled session directly, bypassing ingest.

    The storage tests must NOT go through :func:`note_inbound_chunk`: on a
    machine with the model bundle installed, a completed session schedules a real
    background decode, which then races these assertions by overwriting the very
    fields they check.
    """
    key = make_session_key(PEER, session_id)
    await AeicImageRepository.create_session(
        key=key,
        message_id=None,
        direction="incoming",
        conversation_type="PRIV",
        conversation_key=PEER,
        peer_public_key=PEER,
        square_size=512,
        aspect_code=0,
        rate_code=0,
        total_chunks=1,
        state="receiving",
    )
    await AeicImageRepository.store_bitstream(key, payload or os.urandom(117))
    return key


class TestSessionStorage:
    @pytest.mark.asyncio
    async def test_conflicting_metadata_for_one_key_is_refused(self, test_db):
        key = make_session_key(PEER, 30)
        common = {
            "key": key,
            "message_id": None,
            "direction": "incoming",
            "conversation_type": "PRIV",
            "conversation_key": PEER,
            "peer_public_key": PEER,
            "square_size": 512,
            "aspect_code": 0,
            "rate_code": 0,
            "state": "receiving",
        }
        await AeicImageRepository.create_session(total_chunks=2, **common)
        with pytest.raises(ValueError, match="conflicts"):
            await AeicImageRepository.create_session(total_chunks=3, **common)

    @pytest.mark.asyncio
    async def test_lookup_by_message_id(self, test_db):
        chunks = _chunks(os.urandom(117), session_id=31)
        message_id = await _store_message(chunks[0])
        await note_inbound_chunk(
            text=chunks[0],
            message_id=message_id,
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        session = await AeicImageRepository.get_by_message(message_id)
        assert session is not None
        assert session["session_key"] == make_session_key(PEER, 31)

    @pytest.mark.asyncio
    async def test_a_decode_error_is_recorded_for_the_ui(self, test_db):
        key = await _complete_session(32)
        await AeicImageRepository.store_decode_error(key, "model missing")
        session = await AeicImageRepository.get(key)
        assert session is not None and session["decode_error"] == "model missing"

    @pytest.mark.asyncio
    async def test_storing_a_png_clears_a_previous_error(self, test_db):
        """The retry path: an image received before the model was installed is
        held as a bitstream and decoded later."""
        key = await _complete_session(33)
        await AeicImageRepository.store_decode_error(key, "model missing")
        await AeicImageRepository.store_png(key, b"\x89PNG\r\n\x1a\n")
        session = await AeicImageRepository.get(key)
        assert session is not None
        assert session["decode_error"] is None
        assert session["state"] == "decoded"
        assert bytes(session["png"]).startswith(b"\x89PNG")


class TestOutgoingSessionKeys:
    """Keys for images WE sent must not reuse the 2-base36 wire session id.

    That id has 1296 values because all it has to be is unique per sender inside
    one receiver's reassembly window. As a local storage key it collided at
    roughly 14% for twenty photos a day, and the collision was silent: the
    second send passed ``create_session``'s metadata check, overwrote the first's
    bitstream, and ``COALESCE(message_id, ?)`` kept the first message on the row
    -- so the older bubble rendered the newer picture.
    """

    def test_keys_are_distinct_per_message(self):
        assert outgoing_session_key(11) != outgoing_session_key(12)

    def test_the_key_is_stable_for_one_message(self):
        assert outgoing_session_key(11) == outgoing_session_key(11)

    def test_a_send_with_no_message_row_still_gets_a_unique_key(self):
        """A bot send dropped before a row existed must still be storable."""
        assert outgoing_session_key(None) != outgoing_session_key(None)

    def test_outgoing_keys_cannot_collide_with_an_inbound_one(self):
        """The ``self`` prefix is not hex, so no peer key can produce it."""
        assert not set(OUTGOING_PREFIX) <= set("0123456789abcdef")

    @pytest.mark.asyncio
    async def test_two_sends_do_not_overwrite_each_other(self, test_db):
        """The regression itself, at the storage layer."""
        first = await _store_message("aei1000011first")
        second = await _store_message("aei1000011second")
        common = {
            "direction": "outgoing",
            "conversation_type": "PRIV",
            "conversation_key": PEER,
            "peer_public_key": PEER,
            "square_size": 512,
            "aspect_code": 2,
            "rate_code": 0,
            "total_chunks": 1,
            "state": "complete",
        }
        for message_id, payload in ((first, b"FIRST"), (second, b"SECOND")):
            key = outgoing_session_key(message_id)
            await AeicImageRepository.create_session(key=key, message_id=message_id, **common)
            await AeicImageRepository.store_bitstream(key, payload)

        one = await AeicImageRepository.get_by_message(first)
        two = await AeicImageRepository.get_by_message(second)
        assert one is not None and two is not None
        assert bytes(one["bitstream"]) == b"FIRST"
        assert bytes(two["bitstream"]) == b"SECOND"


class TestUndecodableSessionsExplainThemselves:
    """A session that CANNOT be decoded must say so on the row.

    Without it the row reads ``decoded=false, decode_error=null``, which the UI
    cannot distinguish from "the 5 s synthesis pass is still running" -- so a
    server without onnxruntime left every received image polling once a second
    for a full minute before giving up.
    """

    @pytest.mark.asyncio
    async def test_the_reason_is_stored_when_the_codec_is_unavailable(self, test_db, monkeypatch):
        monkeypatch.setattr(
            aeic_service, "unavailable_reason", lambda *, for_decode: "no onnxruntime here"
        )
        message_id = await _store_message("aei1")
        chunks = _chunks(b"x" * 40, session_id=40)
        assert len(chunks) == 1
        await note_inbound_chunk(
            text=chunks[0],
            message_id=message_id,
            conversation_type="PRIV",
            conversation_key=PEER,
            peer_public_key=PEER,
        )
        session = await AeicImageRepository.get(make_session_key(PEER, 40))
        assert session is not None
        # The bitstream is still kept: it can be decoded later, from this row.
        assert session["bitstream"] is not None
        assert session["decode_error"] == "no onnxruntime here"


class TestDecodeReleasesSessionsOnFailure:
    """The synthesis session must be released even when the decode raises.

    This is the memory contract, not tidy-up, and it applies to the in-process
    fallback -- the path whose process outlives the decode. ``decode_session``
    swallows a decode failure, so anything still held stays held for the life of
    the server, and the next decode then stacks the entropy graph on top of the
    synthesis session.
    """

    class _Backend:
        def __init__(self, fail: bool) -> None:
            self.fail = fail
            self.released_entropy = 0
            self.released_decoder = 0

        def release_entropy_sessions(self) -> None:
            self.released_entropy += 1

        def release_decoder_session(self) -> None:
            self.released_decoder += 1

        def decode_latent_to_rgb(self, y_hat):
            if self.fail:
                raise RuntimeError("synthesis blew up")
            return b"\x00" * (512 * 512 * 3)

    class _Codec:
        def decode_to_latent(self, bitstream: bytes):
            return object()

    def _wire(self, monkeypatch, *, fail: bool):
        backend = self._Backend(fail)
        monkeypatch.setattr(aeic_service, "_require_ready", lambda **_: None)
        monkeypatch.setattr(aeic_service, "_get_backend", lambda: backend)
        monkeypatch.setattr(aeic_service, "_codec", lambda **_: self._Codec())
        return backend

    def test_a_failed_synthesis_still_releases_the_decoder(self, monkeypatch):
        backend = self._wire(monkeypatch, fail=True)
        with pytest.raises(RuntimeError, match="synthesis blew up"):
            aeic_service._decode_in_process(b"whatever")
        assert backend.released_decoder == 1
        assert backend.released_entropy >= 1

    def test_a_successful_decode_releases_both_halves(self, monkeypatch):
        backend = self._wire(monkeypatch, fail=False)
        png = aeic_service._decode_in_process(b"whatever")
        assert png.startswith(b"\x89PNG")
        assert backend.released_decoder == 1
        # Released before synthesis is created, and again in the finally.
        assert backend.released_entropy == 2
