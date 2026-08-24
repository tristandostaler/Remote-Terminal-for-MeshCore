"""The AEIC transport seam.

This is the file to read when migrating to ``CMD_SEND_CHANNEL_DATA`` (62): it
pins the contract both transports share, so an implementation of
:class:`ChannelDataTransport` that satisfies these is a drop-in.

Needs no model and no optional extra -- the seam is pure framing and dispatch.
"""

from __future__ import annotations

import os

import pytest

from app.imaging.aeic.text_transport import (
    DEFAULT_MESSAGE_BUDGET,
    AeicStreamMetadata,
    parse_chunk,
    reassemble,
)
from app.imaging.aeic.transport import (
    CHANNEL_DATA_TRANSPORT,
    CMD_SEND_CHANNEL_DATA,
    DATA_TYPE_AEIC_IMAGE,
    TEXT_TRANSPORT,
    UNKNOWN_RADIO_NAME_BYTES,
    AeicTarget,
    AeicTransportUnavailable,
    ChannelDataTransport,
    TextChunkTransport,
    channel_data_transport_available,
    resolve_message_budget,
    select_transport,
)

SQUARE = AeicStreamMetadata(square_size=512, aspect_code=2)


def make_target(sent: list[str], *, budget: int = DEFAULT_MESSAGE_BUDGET) -> AeicTarget:
    async def emit_text(chunk: str) -> str:
        sent.append(chunk)
        return chunk

    return AeicTarget(
        conversation_type="PRIV",
        conversation_key="aa" * 32,
        emit_text=emit_text,
        message_budget=budget,
    )


class TestTextChunkTransport:
    @pytest.mark.asyncio
    async def test_emits_one_message_for_a_small_bitstream(self):
        sent: list[str] = []
        result = await TextChunkTransport().send(
            os.urandom(117), SQUARE, make_target(sent), session_id=7
        )
        assert result.transport == TEXT_TRANSPORT
        assert result.chunk_count == 1
        assert result.payload_bytes == 117
        assert len(sent) == 1

    @pytest.mark.asyncio
    async def test_round_trips_through_the_emitted_messages(self):
        """What the emitter saw must reassemble to exactly what was sent."""
        payload = os.urandom(209)
        sent: list[str] = []
        result = await TextChunkTransport().send(payload, SQUARE, make_target(sent), session_id=9)
        parsed = [parse_chunk(chunk) for chunk in sent]
        assert all(chunk is not None for chunk in parsed)
        assert reassemble({c.index: c.payload for c in parsed}, result.chunk_count) == payload

    @pytest.mark.asyncio
    async def test_emits_chunks_in_index_order(self):
        """Chunk 0 carries the metadata byte, so it has to arrive first for the
        receiver to open a session at all."""
        sent: list[str] = []
        await TextChunkTransport().send(os.urandom(400), SQUARE, make_target(sent), session_id=3)
        indexes = [parse_chunk(chunk).index for chunk in sent]  # type: ignore[union-attr]
        assert indexes == sorted(indexes) == list(range(len(sent)))
        assert parse_chunk(sent[0]).metadata is not None  # type: ignore[union-attr]

    @pytest.mark.asyncio
    async def test_never_exceeds_the_targets_budget(self):
        for budget in (156, 144, 122, 60):
            sent: list[str] = []
            await TextChunkTransport().send(
                os.urandom(209), SQUARE, make_target(sent, budget=budget), session_id=1
            )
            assert all(len(chunk.encode()) <= budget for chunk in sent), budget

    @pytest.mark.asyncio
    async def test_collects_whatever_the_emitter_returned(self):
        """The route needs the Message rows back to anchor the session to one."""
        sent: list[str] = []
        result = await TextChunkTransport().send(
            os.urandom(209), SQUARE, make_target(sent), session_id=2
        )
        assert result.emitted == sent

    @pytest.mark.asyncio
    async def test_refuses_a_target_with_no_emitter(self):
        target = AeicTarget(conversation_type="PRIV", conversation_key="aa" * 32)
        with pytest.raises(AeicTransportUnavailable, match="emit_text"):
            await TextChunkTransport().send(os.urandom(117), SQUARE, target)

    @pytest.mark.asyncio
    async def test_generates_a_session_id_when_none_is_given(self):
        sent: list[str] = []
        result = await TextChunkTransport().send(os.urandom(117), SQUARE, make_target(sent))
        assert 0 <= result.session_id <= 36**2 - 1
        assert parse_chunk(sent[0]).session_id == result.session_id  # type: ignore[union-attr]

    def test_is_always_available(self):
        assert TextChunkTransport().available is True


class TestChannelDataTransport:
    """The not-yet-implemented binary transport."""

    def test_is_unavailable_on_a_build_without_command_62(self):
        assert channel_data_transport_available() is False
        assert ChannelDataTransport().available is False

    @pytest.mark.asyncio
    async def test_send_explains_what_is_missing_rather_than_failing_obscurely(self):
        sent: list[str] = []
        with pytest.raises(AeicTransportUnavailable) as excinfo:
            await ChannelDataTransport().send(os.urandom(117), SQUARE, make_target(sent))
        detail = str(excinfo.value)
        assert str(CMD_SEND_CHANNEL_DATA) in detail
        assert "not implemented" in detail

    def test_wire_constants_match_the_upstream_framing(self):
        """Pinned from lib/services/image_chunk_transport.dart. These are what an
        implementation has to honour, so a typo here would silently misframe."""
        assert DATA_TYPE_AEIC_IMAGE == 0xAE1C
        assert ChannelDataTransport.BLOB_BYTES == 163
        assert ChannelDataTransport.HEADER_BYTES == 4
        assert ChannelDataTransport.PARITY_LENGTH_BYTES == 1
        # body = blob - header - the parity chunk's length byte
        assert ChannelDataTransport.BODY_BYTES == 158
        # `total` is 4 bits and must be >= 1
        assert ChannelDataTransport.MAX_DATA_CHUNKS == 15

    def test_binary_framing_would_beat_text_for_a_typical_image(self):
        """The reason to migrate, as an assertion rather than a comment.

        A 156-byte bitstream (the measured ft32 mean) is one binary chunk but two
        basE91 text messages, because basE91 expands by ~23%.
        """
        mean_bitstream = 156
        assert mean_bitstream <= ChannelDataTransport.BODY_BYTES  # one chunk
        text_chars = int(mean_bitstream * 1.2308)
        assert text_chars > DEFAULT_MESSAGE_BUDGET - 10  # more than one message


class TestTransportSelection:
    def test_falls_back_to_text_while_the_binary_one_is_unavailable(self):
        transport = select_transport()
        assert transport.name == TEXT_TRANSPORT
        assert transport.available

    def test_can_be_forced_to_text(self):
        assert select_transport(prefer_binary=False).name == TEXT_TRANSPORT

    def test_prefers_binary_the_moment_it_becomes_available(self, monkeypatch):
        """The migration switch. When `meshcore` grows command 62 the probe goes
        true and selection follows on its own -- no call site changes."""
        monkeypatch.setattr(ChannelDataTransport, "available", property(lambda self: True))
        assert select_transport().name == CHANNEL_DATA_TRANSPORT

    def test_the_probe_is_conservative_about_an_unknown_library(self, monkeypatch):
        """It must never claim the transport works when the send would fail: a
        wrongly-optimistic probe wastes an encode and confuses the user."""
        import app.imaging.aeic.transport as transport_module

        monkeypatch.setattr(
            transport_module,
            "channel_data_transport_available",
            lambda: False,
        )
        assert transport_module.channel_data_transport_available() is False


class TestMessageBudget:
    @pytest.mark.asyncio
    async def test_a_dm_gets_the_full_budget(self):
        assert await resolve_message_budget("PRIV") == DEFAULT_MESSAGE_BUDGET

    @pytest.mark.asyncio
    async def test_a_channel_reserves_room_for_the_sender_prefix(self):
        """A channel message carries "sender: " inside the encrypted payload."""
        budget = await resolve_message_budget("CHAN")
        assert budget == DEFAULT_MESSAGE_BUDGET - UNKNOWN_RADIO_NAME_BYTES - 2
        assert budget < DEFAULT_MESSAGE_BUDGET

    @pytest.mark.asyncio
    async def test_uses_the_real_radio_name_when_it_can_read_one(self):
        class FakeRadio:
            def radio_operation(self, _label):
                class Ctx:
                    async def __aenter__(self):
                        class MC:
                            self_info = {"name": "MyNode"}

                        return MC()

                    async def __aexit__(self, *_):
                        return False

                return Ctx()

        budget = await resolve_message_budget("CHAN", radio_manager=FakeRadio())
        assert budget == DEFAULT_MESSAGE_BUDGET - len(b"MyNode") - 2

    @pytest.mark.asyncio
    async def test_falls_back_pessimistically_when_the_radio_errors(self):
        """Over-reserving costs one extra message; under-reserving gets the
        message truncated by the radio, which corrupts the whole image."""

        class BrokenRadio:
            def radio_operation(self, _label):
                raise RuntimeError("no radio")

        budget = await resolve_message_budget("CHAN", radio_manager=BrokenRadio())
        assert budget == DEFAULT_MESSAGE_BUDGET - UNKNOWN_RADIO_NAME_BYTES - 2


class TestMcmpNonInterference:
    """An ``aei1:`` chunk must survive a conversation that has MCMP enabled.

    MCMP compresses outbound text only when the result is smaller. basE91 output
    is high-entropy, so it never is -- and the chunk goes out verbatim. Were that
    to change, the receiver would still be fine (ingest MCMP-decodes before
    parsing chunks), but the compose budget would no longer match the wire.
    """

    @pytest.mark.parametrize("size", [117, 156, 209])
    def test_mcmp_leaves_chunks_untouched(self, size):
        from app.compression import encode_outbound
        from app.imaging.aeic.text_transport import encode_chunks

        for chunk in encode_chunks(os.urandom(size), SQUARE, session_id=5):
            for version in (2, 3):
                assert encode_outbound(chunk, version=version) == chunk
