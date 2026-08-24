"""Bots sending AEIC images.

A bot's image send goes: bytes -> Pillow square -> AEIC encode -> transport ->
``_dispatch_send``. That last hop is deliberate: image chunks ride the bot's own
send path, so they obey the same TX spacing, moderation and test-capture rules as
any reply, and nothing about the bot API changes when the binary 0xAE1C transport
replaces the text one.

The framing tests need no model (a stub transport stands in). The round-trip test
needs the installed bundle and skips without it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.bots.api import BotContext
from app.imaging.aeic.bundle import AeicBundle
from app.imaging.aeic.prepare import RGB_BYTES_EXPECTED, pillow_available
from app.imaging.aeic.text_transport import (
    DEFAULT_MESSAGE_BUDGET,
    AeicStreamMetadata,
    aspect_code_for,
    parse_chunk,
    reassemble,
)
from app.imaging.aeic.transport import AeicSendResult, AeicTransport

MODEL_DIR = Path(os.environ.get("AEIC_MODEL_DIR", "data/models/aeic"))
PEER = "cc" * 32


def _codec_ready() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return AeicBundle(root=MODEL_DIR).supports_decode and pillow_available()


requires_codec = pytest.mark.skipif(
    not _codec_ready(),
    reason=f"needs the AEIC bundle in {MODEL_DIR}, onnxruntime and Pillow",
)


def make_context(*, is_dm: bool = True, channel_key: str | None = None) -> BotContext:
    return BotContext(
        bot_id="test-bot",
        bot_name="test-bot",
        settings={},
        state={},
        origin_is_dm=is_dm,
        origin_sender_key=PEER if is_dm else None,
        origin_channel_key=channel_key,
        is_test=True,
    )


class StubTransport(AeicTransport):
    """Records what it was asked to send without needing the codec."""

    name = "stub"

    def __init__(self) -> None:
        self.calls: list[tuple[bytes, AeicStreamMetadata, str]] = []

    @property
    def available(self) -> bool:
        return True

    async def send(self, bitstream, metadata, target, *, session_id=None):
        self.calls.append((bitstream, metadata, target.conversation_key))
        self.budget = target.message_budget
        self.emit = target.emit_text
        return AeicSendResult(
            transport=self.name,
            session_id=session_id or 0,
            chunk_count=1,
            payload_bytes=len(bitstream),
            emitted=[None],
        )


class TestApiSurface:
    def test_the_image_send_methods_exist_on_the_context(self):
        ctx = make_context()
        for name in ("reply_image", "send_image", "send_dm_image"):
            assert callable(getattr(ctx, name)), name

    @pytest.mark.asyncio
    async def test_refuses_empty_data_before_doing_any_work(self):
        with pytest.raises(ValueError, match="no image data"):
            await make_context().reply_image(b"")

    @pytest.mark.asyncio
    async def test_a_dm_reply_with_no_origin_sender_is_refused(self):
        ctx = BotContext(
            bot_id="b", bot_name="b", settings={}, state={}, origin_is_dm=True, is_test=True
        )
        with pytest.raises(ValueError, match="destination"):
            await ctx.reply_image(bytes(RGB_BYTES_EXPECTED))

    @pytest.mark.asyncio
    async def test_send_image_rejects_an_unknown_channel(self, test_db):
        with pytest.raises(ValueError, match="unknown channel"):
            await make_context(is_dm=False).send_image("#nope", bytes(RGB_BYTES_EXPECTED))


class TestTargetConstruction:
    """What the bot API hands the transport -- the seam's inputs."""

    @pytest.mark.asyncio
    async def test_a_dm_reply_targets_the_origin_sender_with_the_full_budget(self, monkeypatch):
        stub = StubTransport()
        monkeypatch.setattr("app.imaging.aeic.transport.select_transport", lambda *_a, **_k: stub)
        monkeypatch.setattr("app.imaging.aeic.service.select_transport", lambda *_a, **_k: stub)
        monkeypatch.setattr(
            "app.imaging.aeic.service.AeicService.encode_rgb",
            _fake_encode,
        )
        ctx = make_context()
        used = await ctx.reply_image(bytes(RGB_BYTES_EXPECTED))
        assert used == 1
        assert stub.calls and stub.calls[0][2] == PEER
        assert stub.budget == DEFAULT_MESSAGE_BUDGET

    @pytest.mark.asyncio
    async def test_a_channel_reply_reserves_room_for_the_sender_prefix(self, monkeypatch):
        stub = StubTransport()
        monkeypatch.setattr("app.imaging.aeic.service.select_transport", lambda *_a, **_k: stub)
        monkeypatch.setattr("app.imaging.aeic.service.AeicService.encode_rgb", _fake_encode)
        ctx = make_context(is_dm=False, channel_key="AB" * 16)
        await ctx.reply_image(bytes(RGB_BYTES_EXPECTED))
        assert stub.budget < DEFAULT_MESSAGE_BUDGET

    @pytest.mark.asyncio
    async def test_records_the_source_aspect_it_was_given(self, monkeypatch):
        stub = StubTransport()
        monkeypatch.setattr("app.imaging.aeic.service.select_transport", lambda *_a, **_k: stub)
        monkeypatch.setattr("app.imaging.aeic.service.AeicService.encode_rgb", _fake_encode)
        ctx = make_context()
        await ctx.reply_image(bytes(RGB_BYTES_EXPECTED), source_width=1920, source_height=1080)
        assert stub.calls[0][1].aspect_code == aspect_code_for(1920, 1080)


async def _fake_encode(self, rgb):  # noqa: ANN001 - monkeypatched method
    assert len(rgb) == RGB_BYTES_EXPECTED
    return b"\x11" * 117


@requires_codec
class TestRealSend:
    @pytest.mark.asyncio
    async def test_a_bot_reply_image_captures_parsable_chunks_in_test_mode(self):
        """Test mode captures sends instead of transmitting, so this exercises
        the whole encode + framing path without a radio."""
        ctx = make_context()
        used = await ctx.reply_image(bytes(RGB_BYTES_EXPECTED))
        assert used >= 1
        assert len(ctx.captured_sends) == used
        for capture in ctx.captured_sends:
            assert capture["is_dm"] is True
            assert capture["destination"] == PEER
            assert len(capture["text"].encode()) <= DEFAULT_MESSAGE_BUDGET
            assert parse_chunk(capture["text"]) is not None

    @pytest.mark.asyncio
    async def test_the_captured_chunks_reassemble_to_the_encoded_bitstream(self):
        """Sender-side round trip: what a receiver would rebuild from the wire."""
        ctx = make_context()
        await ctx.reply_image(bytes(RGB_BYTES_EXPECTED))
        parsed = [parse_chunk(c["text"]) for c in ctx.captured_sends]
        assert all(p is not None for p in parsed)
        bitstream = reassemble({p.index: p.payload for p in parsed}, parsed[0].total)
        assert bitstream is not None and len(bitstream) > 0
        assert parsed[0].metadata is not None
        assert parsed[0].metadata.square_size == 512

    @pytest.mark.asyncio
    @pytest.mark.skipif(not pillow_available(), reason="needs Pillow")
    async def test_sends_an_encoded_image_and_keeps_its_aspect(self):
        """The actual bot flow: bytes from ctx.http, not raw pixels."""
        import io as _io

        from PIL import Image

        buffer = _io.BytesIO()
        Image.new("RGB", (1920, 1080), (30, 60, 90)).save(buffer, format="JPEG")
        ctx = make_context()
        used = await ctx.reply_image(buffer.getvalue())
        assert used >= 1
        first = parse_chunk(ctx.captured_sends[0]["text"])
        assert first is not None and first.metadata is not None
        # 16:9 detected from the JPEG itself, no explicit dimensions passed.
        assert first.metadata.aspect_code == aspect_code_for(1920, 1080)

    @pytest.mark.asyncio
    async def test_a_channel_send_stays_within_the_smaller_budget(self, test_db):
        from app.repository import ChannelRepository

        await ChannelRepository.upsert("AB" * 16, "Test")
        ctx = make_context(is_dm=False, channel_key="AB" * 16)
        await ctx.send_image("AB" * 16, bytes(RGB_BYTES_EXPECTED))
        assert ctx.captured_sends
        for capture in ctx.captured_sends:
            assert capture["is_dm"] is False
            # Budget is reduced for the "sender: " prefix the firmware adds.
            assert len(capture["text"].encode()) < DEFAULT_MESSAGE_BUDGET


class TestTheSentImageIsRecordedLocally:
    """A bot's own photo has to render as a picture in the operator's view too.

    The local AEIC session is anchored to the sent message's id, so the send path
    has to hand that id back. When ``_dispatch_send`` returned None
    unconditionally the session was written with ``message_id=NULL``, nothing
    could look it up by message, and the operator's own conversation showed raw
    ``aei1:`` basE91 while the recipient saw the photo.
    """

    class _Sent:
        """Stands in for the stored ``Message`` the real send path returns."""

        def __init__(self, message_id: int) -> None:
            self.id = message_id

    @pytest.mark.asyncio
    async def test_dispatch_send_hands_back_the_stored_message(self):
        sent: list[str] = []
        counter = {"next": 100}

        async def send_fn(**kwargs):
            sent.append(kwargs["text"])
            counter["next"] += 1
            return self._Sent(counter["next"])

        ctx = BotContext(
            bot_id="b",
            bot_name="b",
            settings={},
            state={},
            origin_is_dm=True,
            origin_sender_key=PEER,
            is_test=False,
            send_fn=send_fn,
        )
        result = await ctx._dispatch_send(
            is_dm=True,
            destination=PEER,
            channel_key=None,
            text="aei1000011abc",
            flood_scope_override=None,
        )
        assert result is not None and result.id == 101
        assert sent == ["aei1000011abc"]

    @pytest.mark.asyncio
    async def test_the_session_row_carries_the_message_id(self, test_db, monkeypatch):
        """End to end through send_image, with a stub transport and a fake send."""
        from app.imaging.aeic.service import aeic_service
        from app.repository import AeicImageRepository, MessageRepository

        # message_id is a foreign key into messages, so the row has to exist.
        message_id = await MessageRepository.create(
            msg_type="PRIV",
            text="aei1000011payload",
            received_at=1_700_000_000,
            conversation_key=PEER,
            sender_key=PEER,
        )
        assert message_id is not None

        class _Target:
            conversation_type = "PRIV"
            conversation_key = PEER

        class _Transport(AeicTransport):
            name = "test"

            @property
            def available(self) -> bool:
                return True

            async def send(self, bitstream, metadata, target, *, session_id=None):
                message = TestTheSentImageIsRecordedLocally._Sent(message_id)
                return AeicSendResult(
                    transport=self.name,
                    session_id=session_id or 1,
                    chunk_count=1,
                    payload_bytes=len(bitstream),
                    emitted=[message],
                )

        async def fake_encode(rgb):
            return b"bitstream-bytes"

        # monkeypatch rather than assigning on the singleton: these tests share
        # one process-wide aeic_service and can run in any order.
        monkeypatch.setattr(aeic_service, "encode_rgb", fake_encode)
        result, _bits, _meta = await aeic_service.send_image(
            bytes(RGB_BYTES_EXPECTED),
            _Target(),  # type: ignore[arg-type]
            transport=_Transport(),
        )

        assert result.storage_key is not None
        session = await AeicImageRepository.get_by_message(message_id)
        assert session is not None, "the sent photo has no session anchored to its message"
        assert session["session_key"] == result.storage_key
        assert session["direction"] == "outgoing"
        assert bytes(session["bitstream"]) == b"bitstream-bytes"


class TestModerationLeavesImageChunksAlone:
    """The profanity filter must not rewrite a framed image chunk.

    The word list is ``\\b``-anchored and basE91's alphabet is full of non-word
    characters, so a payload CAN contain a bare match. Censoring substitutes
    bytes inside the stream -- same length, different bytes -- and the receiver
    decodes a garbage image with nothing raised. "Drop" mode is worse: it removes
    one chunk of an image whose other chunk already went out.
    """

    def test_censoring_would_corrupt_a_chunk_that_happens_to_match(self):
        """Establishes the hazard is real, not theoretical."""
        from app.bots.moderation import apply_profanity_mode

        chunk = "aei10501" + "AB!shit!CD" + "E" * 130
        assert apply_profanity_mode(chunk, "censor") != chunk
        assert apply_profanity_mode(chunk, "drop") is None

    def test_the_guard_recognises_a_chunk_as_framed(self):
        from app.compression import is_framed_payload

        chunk = "aei10501" + "AB!shit!CD" + "E" * 130
        assert is_framed_payload(chunk)

    @pytest.mark.parametrize("mode", ["censor", "drop"])
    def test_prose_is_still_moderated(self, mode):
        """The guard must not become a hole in moderation for ordinary replies."""
        from app.bots.moderation import apply_profanity_mode
        from app.compression import is_framed_payload

        prose = "that is some shit weather"
        assert not is_framed_payload(prose)
        assert apply_profanity_mode(prose, mode) != prose
