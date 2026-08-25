"""Server-side MCMP decode on ingest.

Compressed bodies arrive as ordinary text behind an ``mcmp2:``/``mcmp3:`` prefix.
These tests prove the shared channel and DM ingest points decode them to
plaintext before storage, so the DB (and therefore search, bots and the UI) see
the real message — while non-MCMP text passes through untouched.
"""

import pytest

from app.compression.mcmp import MeshCompressor, encode_v3_text
from app.repository import MessageRepository, RawPacketRepository

CHANNEL_KEY = "ABC123DEF456ABC123DEF456ABC12345"
CONTACT_PUB = "a1b2c3d3ba9f5fa8705b9845fe11cc6f01d1d49caaf4d122ac7121663c5beec7"
SENDER_TIMESTAMP = 1700000000

# Long enough to actually compress (short strings are sent as plaintext).
LONG_TEXT = "Battery at 40%, switching to power save and checking channel five for traffic."


@pytest.fixture(scope="module")
def compressor() -> MeshCompressor:
    c = MeshCompressor()
    c.load_from_path()
    return c


class TestChannelIngestDecode:
    @pytest.mark.asyncio
    async def test_v2_channel_body_is_decoded(self, test_db, compressor):
        from app.packet_processor import create_message_from_decrypted

        wire = compressor.encode_if_smaller(LONG_TEXT)
        assert wire.startswith("mcmp2:")  # guard: the sample really compressed

        packet_id, _ = await RawPacketRepository.create(b"chan_v2", SENDER_TIMESTAMP)

        msg_id = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text=wire,
            timestamp=SENDER_TIMESTAMP,
        )
        assert msg_id is not None
        stored = await MessageRepository.get_by_id(msg_id)
        assert stored is not None
        assert stored.text == f"Alice: {LONG_TEXT}"

    @pytest.mark.asyncio
    async def test_v3_channel_body_is_decoded(self, test_db, compressor):
        from app.packet_processor import create_message_from_decrypted

        wire = encode_v3_text(compressor, LONG_TEXT, timestamp=SENDER_TIMESTAMP)
        assert wire.startswith("mcmp3:")

        packet_id, _ = await RawPacketRepository.create(b"chan_v3", SENDER_TIMESTAMP)

        msg_id = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=CHANNEL_KEY,
            sender="Bob",
            message_text=wire,
            timestamp=SENDER_TIMESTAMP,
        )
        assert msg_id is not None
        stored = await MessageRepository.get_by_id(msg_id)
        assert stored is not None
        assert stored.text == f"Bob: {LONG_TEXT}"

    @pytest.mark.asyncio
    async def test_plain_channel_body_passthrough(self, test_db, compressor):
        from app.packet_processor import create_message_from_decrypted

        packet_id, _ = await RawPacketRepository.create(b"chan_plain", SENDER_TIMESTAMP)

        msg_id = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=CHANNEL_KEY,
            sender="Carol",
            message_text="just a normal message",
            timestamp=SENDER_TIMESTAMP,
        )
        assert msg_id is not None
        stored = await MessageRepository.get_by_id(msg_id)
        assert stored is not None
        assert stored.text == "Carol: just a normal message"

    @pytest.mark.asyncio
    async def test_fallback_channel_route_decodes(self, test_db, captured_broadcasts, compressor):
        # The get_msg() drain route must decode at parity with the raw-RF route,
        # or the same message arriving via both paths stores two rows.
        from app.services.messages import create_fallback_channel_message

        _, mock_broadcast = captured_broadcasts
        wire = compressor.encode_if_smaller(LONG_TEXT)
        assert wire.startswith("mcmp2:")

        message = await create_fallback_channel_message(
            conversation_key=CHANNEL_KEY,
            message_text=wire,
            sender_timestamp=SENDER_TIMESTAMP,
            received_at=SENDER_TIMESTAMP,
            path=None,
            path_len=None,
            txt_type=0,
            sender_name="Dave",
            channel_name="#general",
            broadcast_fn=mock_broadcast,
        )
        assert message is not None
        assert message.text == f"Dave: {LONG_TEXT}"

    @pytest.mark.asyncio
    async def test_both_channel_routes_dedup_to_one_row(
        self, test_db, captured_broadcasts, compressor
    ):
        # A compressed message arriving via BOTH the raw-RF and get_msg routes
        # must collapse onto a single stored row (both decode to the same text).
        from app.packet_processor import create_message_from_decrypted
        from app.services.messages import create_fallback_channel_message

        _, mock_broadcast = captured_broadcasts
        wire = compressor.encode_if_smaller(LONG_TEXT)
        packet_id, _ = await RawPacketRepository.create(b"dedup", SENDER_TIMESTAMP)

        first = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=CHANNEL_KEY,
            sender="Erin",
            message_text=wire,
            timestamp=SENDER_TIMESTAMP,
        )
        assert first is not None
        second = await create_fallback_channel_message(
            conversation_key=CHANNEL_KEY,
            message_text=wire,
            sender_timestamp=SENDER_TIMESTAMP,
            received_at=SENDER_TIMESTAMP,
            path=None,
            path_len=None,
            txt_type=0,
            sender_name="Erin",
            channel_name="#general",
            broadcast_fn=mock_broadcast,
        )
        assert second is None  # deduped against the first row
        rows = await MessageRepository.get_all(
            msg_type="CHAN", conversation_key=CHANNEL_KEY.upper(), limit=10
        )
        matching = [r for r in rows if r.text == f"Erin: {LONG_TEXT}"]
        assert len(matching) == 1


class TestIngestRecordsCompressionFacts:
    """Decoding also records what the body arrived compressed as.

    The conversation view renders the codec and ratio from these columns, so a
    received message can show the same badge as one we sent.
    """

    @pytest.mark.asyncio
    async def test_channel_records_codec_and_bytes(self, test_db, compressor):
        from app.packet_processor import create_message_from_decrypted

        wire = compressor.encode_if_smaller(LONG_TEXT)
        packet_id, _ = await RawPacketRepository.create(b"chan_facts", SENDER_TIMESTAMP)

        msg_id = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text=wire,
            timestamp=SENDER_TIMESTAMP,
        )
        assert msg_id is not None
        stored = await MessageRepository.get_by_id(msg_id)
        assert stored is not None
        assert stored.compression == "mcmp2"
        # Measured against the body, not the stored "Alice: ..." text: the
        # firmware adds that prefix outside the compressed payload.
        assert stored.plain_bytes == len(LONG_TEXT.encode("utf-8"))
        assert stored.wire_bytes == len(wire.encode("utf-8"))

    @pytest.mark.asyncio
    async def test_channel_records_nothing_for_plain_text(self, test_db, compressor):
        from app.packet_processor import create_message_from_decrypted

        packet_id, _ = await RawPacketRepository.create(b"chan_plain_facts", SENDER_TIMESTAMP)

        msg_id = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text="short and plain",
            timestamp=SENDER_TIMESTAMP,
        )
        assert msg_id is not None
        stored = await MessageRepository.get_by_id(msg_id)
        assert stored is not None
        assert stored.compression is None
        assert stored.wire_bytes is None

    @pytest.mark.asyncio
    async def test_v3_dm_ratio_excludes_the_container(
        self, test_db, captured_broadcasts, compressor
    ):
        from app.services.dm_ingest import ingest_fallback_direct_message

        _, mock_broadcast = captured_broadcasts
        wire = encode_v3_text(compressor, LONG_TEXT, timestamp=SENDER_TIMESTAMP)

        message = await ingest_fallback_direct_message(
            conversation_key=CONTACT_PUB,
            text=wire,
            sender_timestamp=SENDER_TIMESTAMP,
            received_at=SENDER_TIMESTAMP,
            path=None,
            path_len=None,
            txt_type=0,
            signature=None,
            sender_name="Peer",
            sender_key=CONTACT_PUB,
            broadcast_fn=mock_broadcast,
        )
        assert message is not None
        assert message.compression == "mcmp3"
        assert message.wire_bytes == len(wire.encode("utf-8"))
        # The v3 header is airtime but not part of the ratio, matching meshcore-open.
        assert message.payload_bytes is not None
        assert message.payload_bytes < message.wire_bytes

    @pytest.mark.asyncio
    async def test_incoming_messages_carry_no_send_progress(
        self, test_db, captured_broadcasts, compressor
    ):
        """Attempts and send state describe our own sends only."""
        from app.services.dm_ingest import ingest_fallback_direct_message

        _, mock_broadcast = captured_broadcasts
        message = await ingest_fallback_direct_message(
            conversation_key=CONTACT_PUB,
            text="hello there",
            sender_timestamp=SENDER_TIMESTAMP + 5,
            received_at=SENDER_TIMESTAMP + 5,
            path=None,
            path_len=None,
            txt_type=0,
            signature=None,
            sender_name="Peer",
            sender_key=CONTACT_PUB,
            broadcast_fn=mock_broadcast,
        )
        assert message is not None
        assert message.send_attempts is None
        assert message.send_max_attempts is None
        assert message.send_state is None


class TestDirectMessageIngestDecode:
    @pytest.mark.asyncio
    async def test_v2_dm_body_is_decoded(self, test_db, captured_broadcasts, compressor):
        from app.services.dm_ingest import ingest_fallback_direct_message

        _, mock_broadcast = captured_broadcasts
        wire = compressor.encode_if_smaller(LONG_TEXT)
        assert wire.startswith("mcmp2:")

        message = await ingest_fallback_direct_message(
            conversation_key=CONTACT_PUB,
            text=wire,
            sender_timestamp=SENDER_TIMESTAMP,
            received_at=SENDER_TIMESTAMP,
            path=None,
            path_len=None,
            txt_type=0,
            signature=None,
            sender_name="Peer",
            sender_key=CONTACT_PUB,
            broadcast_fn=mock_broadcast,
        )
        assert message is not None
        assert message.text == LONG_TEXT

    @pytest.mark.asyncio
    async def test_v3_dm_body_is_decoded(self, test_db, captured_broadcasts, compressor):
        from app.services.dm_ingest import ingest_fallback_direct_message

        _, mock_broadcast = captured_broadcasts
        wire = encode_v3_text(compressor, LONG_TEXT, timestamp=SENDER_TIMESTAMP)
        assert wire.startswith("mcmp3:")

        message = await ingest_fallback_direct_message(
            conversation_key=CONTACT_PUB,
            text=wire,
            sender_timestamp=SENDER_TIMESTAMP + 1,
            received_at=SENDER_TIMESTAMP + 1,
            path=None,
            path_len=None,
            txt_type=0,
            signature=None,
            sender_name="Peer",
            sender_key=CONTACT_PUB,
            broadcast_fn=mock_broadcast,
        )
        assert message is not None
        assert message.text == LONG_TEXT

    @pytest.mark.asyncio
    async def test_plain_dm_body_passthrough(self, test_db, captured_broadcasts, compressor):
        from app.services.dm_ingest import ingest_fallback_direct_message

        _, mock_broadcast = captured_broadcasts
        message = await ingest_fallback_direct_message(
            conversation_key=CONTACT_PUB,
            text="hello there",
            sender_timestamp=SENDER_TIMESTAMP + 2,
            received_at=SENDER_TIMESTAMP + 2,
            path=None,
            path_len=None,
            txt_type=0,
            signature=None,
            sender_name="Peer",
            sender_key=CONTACT_PUB,
            broadcast_fn=mock_broadcast,
        )
        assert message is not None
        assert message.text == "hello there"
