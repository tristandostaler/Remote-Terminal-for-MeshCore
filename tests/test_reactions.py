"""MeshCore Open Advanced compatible emoji reactions.

Wire-format and hash tests are pinned to ``tests/fixtures/reaction_hash_vectors.json``,
generated from a C transcription of the Dart SDK string-hash reference
(runtime/vm/hash.h) -- the algorithm behind Dart VM ``String.hashCode``, which
MCO Advanced reaction hashes are built on. The integration tests prove reaction
payloads are stored hidden, applied to their target, deduped across echoes, and
kept out of every conversation surface.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.models import Message
from app.reactions import (
    QUICK_REACTION_EMOJIS,
    REACTION_EMOJIS,
    ReactionInfo,
    apply_reaction,
    clean_channel_body_for_hash,
    compute_reaction_hash,
    dart_string_hash,
    emoji_to_index_hex,
    encode_reaction,
    extract_reaction_from_stored_text,
    hash_inputs_for_message,
    parse_reaction,
    split_channel_sender_text,
)
from app.repository import ChannelRepository, ContactRepository, MessageRepository

VECTORS_PATH = Path(__file__).parent / "fixtures" / "reaction_hash_vectors.json"

CHANNEL_KEY = "AB" * 16
CONTACT_PUB = "cd" * 32
ROOM_PUB = "ee" * 32
TS = 1756300000


class TestDartHashGoldenVectors:
    def test_string_hashes_match_dart_reference(self):
        vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
        for vector in vectors["string_hashes"]:
            assert dart_string_hash(vector["text"]) == vector["dart_hash"], vector["text"]

    def test_reaction_hashes_match_dart_reference(self):
        vectors = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))
        for vector in vectors["reaction_hashes"]:
            got = compute_reaction_hash(vector["timestamp"], vector["sender_name"], vector["text"])
            assert got == vector["reaction_hash"], vector

    def test_empty_string_hashes_to_one(self):
        # FinalizeHash maps 0 to 1 so a hash is never zero.
        assert dart_string_hash("") == 1


class TestWireFormat:
    def test_emoji_table_matches_mco_advanced(self):
        assert len(REACTION_EMOJIS) == 184
        assert REACTION_EMOJIS[:6] == QUICK_REACTION_EMOJIS
        assert REACTION_EMOJIS[0] == "👍"
        assert REACTION_EMOJIS[1] == "❤️"

    def test_encoding_uses_first_occurrence(self):
        # 👍 also sits in the gestures block (index 0x46); indexOf picks 0x00.
        assert emoji_to_index_hex("👍") == "00"
        assert emoji_to_index_hex("❤️") == "01"
        assert emoji_to_index_hex("🚀") == format(183, "02x")
        assert emoji_to_index_hex("🦄") is None

    def test_parse_accepts_duplicate_index(self):
        assert parse_reaction("r:0000:46").emoji == "👍"

    def test_parse_rejects_invalid_forms(self):
        assert parse_reaction("r:1a2b:00") == ReactionInfo("1a2b", "👍")
        assert parse_reaction("R:1a2b:00") is None  # uppercase marker
        assert parse_reaction("r:1A2B:00") is None  # uppercase hash
        assert parse_reaction("r:1a2b:ff") is None  # index out of range
        assert parse_reaction("r:1a2:00") is None  # short hash
        assert parse_reaction("r:1a2b:00 ") is None  # trailing junk
        assert parse_reaction("hello") is None

    def test_encode_round_trips(self):
        text = encode_reaction("beef", emoji_to_index_hex("🔥"))
        parsed = parse_reaction(text)
        assert parsed == ReactionInfo("beef", "🔥")

    def test_reactions_are_never_mcmp_wrapped(self):
        from app.compression.mcmp import is_framed_payload

        assert is_framed_payload("r:1a2b:00")
        assert not is_framed_payload("regular text")


class TestSenderSplit:
    """Port-parity with MCO Advanced's _splitSenderText."""

    def test_standard_forms(self):
        assert split_channel_sender_text("Alice: hello") == ("Alice", "hello")
        assert split_channel_sender_text("Alice:hello") == ("Alice", "hello")
        assert split_channel_sender_text("no colon") == ("Unknown", "no colon")
        assert split_channel_sender_text("trailing:") == ("Unknown", "trailing:")
        assert split_channel_sender_text(":lead") == ("Unknown", ":lead")

    def test_bracket_names_are_rejected(self):
        assert split_channel_sender_text("[bot]: hi") == ("Unknown", "[bot]: hi")

    def test_bare_reaction_splits_as_sender_r(self):
        # Parity blind spot: a nameless node's channel reaction is not
        # recognizable -- MCO Advanced splits it the same way.
        assert split_channel_sender_text("r:1a2b:00") == ("r", "1a2b:00")
        assert extract_reaction_from_stored_text("CHAN", "r:1a2b:00") is None

    def test_fifty_unit_budget_counts_utf16_units(self):
        assert split_channel_sender_text("x" * 49 + ": body") == ("x" * 49, "body")
        assert split_channel_sender_text("x" * 50 + ": body") == ("Unknown", "x" * 50 + ": body")
        # 25 astral emoji are 50 UTF-16 units: over budget despite 25 chars.
        astral = "💀" * 25
        assert split_channel_sender_text(astral + ": body") == ("Unknown", astral + ": body")


class TestReplyCleaning:
    def test_mention_is_stripped(self):
        assert clean_channel_body_for_hash("@[Bob] the reply") == "the reply"

    def test_exact_quote_line_is_stripped(self):
        assert clean_channel_body_for_hash("@[Bob] >fragment...\nthe reply") == "the reply"

    def test_empty_fragment_is_kept(self):
        # resolveReply requires a non-empty fragment (newline index > 1).
        assert clean_channel_body_for_hash("@[Bob] >\nx") == ">\nx"

    def test_plain_body_untouched(self):
        assert clean_channel_body_for_hash("no mention here") == "no mention here"

    def test_channel_hash_uses_cleaned_reply_body(self):
        msg = _message(
            1,
            msg_type="CHAN",
            text="Bob: @[Alice] >you said this\nI agree entirely",
            sender_name="Bob",
        )
        inputs = hash_inputs_for_message(msg, is_room=False, our_name=None)
        assert inputs == (TS, "Bob", "I agree entirely")


def _message(
    msg_id: int,
    *,
    msg_type: str = "PRIV",
    text: str = "hello",
    sender_name: str | None = None,
    outgoing: bool = False,
    sender_timestamp: int | None = TS,
    conversation_key: str = CONTACT_PUB,
) -> Message:
    return Message(
        id=msg_id,
        type=msg_type,
        conversation_key=conversation_key,
        text=text,
        sender_timestamp=sender_timestamp,
        received_at=TS,
        outgoing=outgoing,
        sender_name=sender_name,
    )


class TestHashInputs:
    def test_channel_message_splits_stored_text(self):
        msg = _message(1, msg_type="CHAN", text="Alice: Hello world", sender_name="Alice")
        assert hash_inputs_for_message(msg, is_room=False, our_name=None) == (
            TS,
            "Alice",
            "Hello world",
        )

    def test_channel_unparseable_sender_hashes_as_unknown(self):
        msg = _message(1, msg_type="CHAN", text="no sender prefix here")
        assert hash_inputs_for_message(msg, is_room=False, our_name=None) == (
            TS,
            "Unknown",
            "no sender prefix here",
        )

    def test_direct_one_to_one_has_no_sender(self):
        msg = _message(1, text="Hello world", sender_name="Alice")
        assert hash_inputs_for_message(msg, is_room=False, our_name="Me") == (
            TS,
            None,
            "Hello world",
        )

    def test_room_incoming_uses_stored_sender_name(self):
        msg = _message(1, text="post body", sender_name="Author")
        assert hash_inputs_for_message(msg, is_room=True, our_name="Me") == (
            TS,
            "Author",
            "post body",
        )

    def test_room_outgoing_uses_our_name(self):
        msg = _message(1, text="my post", outgoing=True)
        assert hash_inputs_for_message(msg, is_room=True, our_name="Me") == (TS, "Me", "my post")

    def test_message_without_timestamp_is_unhashable(self):
        msg = _message(1, sender_timestamp=None)
        assert hash_inputs_for_message(msg, is_room=False, our_name=None) is None


def _channel_reaction_text(sender: str, target_ts: int, target_sender: str, target_body: str):
    """Wire text a client would send to react 👍 to a channel message."""
    target_hash = compute_reaction_hash(target_ts, target_sender, target_body)
    return f"{sender}: {encode_reaction(target_hash, '00')}"


class TestChannelReactionIngest:
    @pytest.mark.asyncio
    async def test_reaction_hides_and_applies_to_target(self, test_db, captured_broadcasts):
        from app.services.messages import create_message_from_decrypted

        broadcasts, mock_broadcast = captured_broadcasts

        target_id = await create_message_from_decrypted(
            packet_id=1,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text="Hello world",
            timestamp=TS,
            broadcast_fn=mock_broadcast,
        )
        assert target_id is not None
        broadcasts.clear()

        reaction_id = await create_message_from_decrypted(
            packet_id=2,
            channel_key=CHANNEL_KEY,
            sender="Bob",
            message_text=encode_reaction(compute_reaction_hash(TS, "Alice", "Hello world"), "00"),
            timestamp=TS + 60,
            broadcast_fn=mock_broadcast,
        )
        assert reaction_id is not None

        # The reaction row exists but is flagged and hidden from listings.
        stored_reaction = await MessageRepository.get_by_id(reaction_id)
        assert stored_reaction is not None and stored_reaction.is_reaction
        listed = await MessageRepository.get_all(conversation_key=CHANNEL_KEY)
        assert [m.id for m in listed] == [target_id]

        # The target carries the emoji, and the update was broadcast.
        target = await MessageRepository.get_by_id(target_id)
        assert target.reactions == {"👍": 1}
        reaction_events = [b for b in broadcasts if b["type"] == "message_reaction"]
        assert len(reaction_events) == 1
        assert reaction_events[0]["data"] == {
            "message_id": target_id,
            "conversation_key": CHANNEL_KEY,
            "type": "CHAN",
            "reactions": {"👍": 1},
        }
        # No visible-message broadcast for the reaction row itself.
        assert not [b for b in broadcasts if b["type"] == "message"]

    @pytest.mark.asyncio
    async def test_flood_echo_does_not_double_count(self, test_db, captured_broadcasts):
        from app.services.messages import create_message_from_decrypted

        _, mock_broadcast = captured_broadcasts

        target_id = await create_message_from_decrypted(
            packet_id=1,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text="Hello world",
            timestamp=TS,
            broadcast_fn=mock_broadcast,
        )
        reaction_body = encode_reaction(compute_reaction_hash(TS, "Alice", "Hello world"), "00")
        for packet_id in (2, 3):  # the same reaction heard twice (repeater echo)
            await create_message_from_decrypted(
                packet_id=packet_id,
                channel_key=CHANNEL_KEY,
                sender="Bob",
                message_text=reaction_body,
                timestamp=TS + 60,
                broadcast_fn=mock_broadcast,
            )

        target = await MessageRepository.get_by_id(target_id)
        assert target.reactions == {"👍": 1}

    @pytest.mark.asyncio
    async def test_two_reactors_count_separately(self, test_db, captured_broadcasts):
        from app.services.messages import create_message_from_decrypted

        _, mock_broadcast = captured_broadcasts

        target_id = await create_message_from_decrypted(
            packet_id=1,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text="Hello world",
            timestamp=TS,
            broadcast_fn=mock_broadcast,
        )
        reaction_body = encode_reaction(compute_reaction_hash(TS, "Alice", "Hello world"), "00")
        for packet_id, reactor in ((2, "Bob"), (3, "Carol")):
            await create_message_from_decrypted(
                packet_id=packet_id,
                channel_key=CHANNEL_KEY,
                sender=reactor,
                message_text=reaction_body,
                timestamp=TS + 60 + packet_id,
                broadcast_fn=mock_broadcast,
            )

        target = await MessageRepository.get_by_id(target_id)
        assert target.reactions == {"👍": 2}

    @pytest.mark.asyncio
    async def test_reaction_matches_newest_on_hash_collision(self, test_db, captured_broadcasts):
        from app.services.messages import create_message_from_decrypted

        _, mock_broadcast = captured_broadcasts

        # Identical sender/timestamp/first-5 on different packets: same hash.
        older = await create_message_from_decrypted(
            packet_id=1,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text="Hello world",
            timestamp=TS,
            broadcast_fn=mock_broadcast,
        )
        newer = await create_message_from_decrypted(
            packet_id=2,
            channel_key=CHANNEL_KEY,
            sender="Alice",
            message_text="Hello again",  # same first 5 units "Hello"
            timestamp=TS,
            broadcast_fn=mock_broadcast,
        )
        await create_message_from_decrypted(
            packet_id=3,
            channel_key=CHANNEL_KEY,
            sender="Bob",
            message_text=encode_reaction(compute_reaction_hash(TS, "Alice", "Hello world"), "00"),
            timestamp=TS + 5,
            broadcast_fn=mock_broadcast,
        )

        assert (await MessageRepository.get_by_id(newer)).reactions == {"👍": 1}
        assert (await MessageRepository.get_by_id(older)).reactions is None

    @pytest.mark.asyncio
    async def test_unmatched_reaction_is_stored_but_lost(self, test_db, captured_broadcasts):
        from app.services.messages import create_message_from_decrypted

        broadcasts, mock_broadcast = captured_broadcasts

        reaction_id = await create_message_from_decrypted(
            packet_id=1,
            channel_key=CHANNEL_KEY,
            sender="Bob",
            message_text="r:dead:00",
            timestamp=TS,
            broadcast_fn=mock_broadcast,
        )
        assert reaction_id is not None
        assert not [b for b in broadcasts if b["type"] in ("message", "message_reaction")]
        assert await MessageRepository.get_all(conversation_key=CHANNEL_KEY) == []


class TestDirectReactionIngest:
    async def _store_incoming_reaction(self, mock_broadcast, conversation_key, target_hash):
        from app.services.dm_ingest import ingest_fallback_direct_message

        return await ingest_fallback_direct_message(
            conversation_key=conversation_key,
            text=encode_reaction(target_hash, "01"),
            sender_timestamp=TS + 120,
            received_at=TS + 120,
            path=None,
            path_len=None,
            txt_type=0,
            signature=None,
            sender_name=None,
            sender_key=conversation_key,
            broadcast_fn=mock_broadcast,
        )

    @pytest.mark.asyncio
    async def test_incoming_dm_reaction_targets_our_outgoing_message(
        self, test_db, captured_broadcasts
    ):
        broadcasts, mock_broadcast = captured_broadcasts
        await ContactRepository.upsert({"public_key": CONTACT_PUB, "name": "Peer", "type": 1})

        # Same timestamp and text in both directions: the direction rule, not
        # the hash, must pick our outgoing message.
        incoming_id = await MessageRepository.create(
            msg_type="PRIV",
            text="Hello world",
            conversation_key=CONTACT_PUB,
            sender_timestamp=TS,
            received_at=TS,
            outgoing=False,
        )
        outgoing_id = await MessageRepository.create(
            msg_type="PRIV",
            text="Hello world",
            conversation_key=CONTACT_PUB,
            sender_timestamp=TS,
            received_at=TS + 1,
            outgoing=True,
        )
        broadcasts.clear()

        target_hash = compute_reaction_hash(TS, None, "Hello world")
        stored = await self._store_incoming_reaction(mock_broadcast, CONTACT_PUB, target_hash)
        assert stored is not None and stored.is_reaction

        assert (await MessageRepository.get_by_id(outgoing_id)).reactions == {"❤️": 1}
        assert (await MessageRepository.get_by_id(incoming_id)).reactions is None
        assert not [b for b in broadcasts if b["type"] == "message"]

    @pytest.mark.asyncio
    async def test_room_reaction_matches_by_author_name(self, test_db, captured_broadcasts):
        broadcasts, mock_broadcast = captured_broadcasts
        await ContactRepository.upsert({"public_key": ROOM_PUB, "name": "TheRoom", "type": 3})

        post_id = await MessageRepository.create(
            msg_type="PRIV",
            text="room post body",
            conversation_key=ROOM_PUB,
            sender_timestamp=TS,
            received_at=TS,
            outgoing=False,
            sender_name="Author",
        )
        broadcasts.clear()

        target_hash = compute_reaction_hash(TS, "Author", "room post body")
        stored = await self._store_incoming_reaction(mock_broadcast, ROOM_PUB, target_hash)
        assert stored is not None and stored.is_reaction
        assert (await MessageRepository.get_by_id(post_id)).reactions == {"❤️": 1}


class TestConversationSurfacesHideReactions:
    @pytest.mark.asyncio
    async def test_unread_counts_and_last_times_skip_reaction_rows(self, test_db):
        await ChannelRepository.upsert(key=CHANNEL_KEY, name="#general")

        await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: Hello world",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS,
            received_at=TS,
            sender_name="Alice",
        )
        await MessageRepository.create(
            msg_type="CHAN",
            text="Bob: r:1a2b:00",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS + 100,
            received_at=TS + 100,
            sender_name="Bob",
            is_reaction=True,
        )

        data = await MessageRepository.get_unread_counts(None)
        state_key = f"channel-{CHANNEL_KEY}"
        assert data["counts"][state_key] == 1
        assert data["last_message_times"][state_key] == TS

    @pytest.mark.asyncio
    async def test_get_around_never_returns_reaction_rows(self, test_db):
        target_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: Hello world",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS,
            received_at=TS,
        )
        reaction_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Bob: r:1a2b:00",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS + 1,
            received_at=TS + 1,
            is_reaction=True,
        )

        messages, _, _ = await MessageRepository.get_around(target_id)
        assert [m.id for m in messages] == [target_id]
        missing, _, _ = await MessageRepository.get_around(reaction_id)
        assert missing == []


class TestReactEndpoint:
    def _mock_mc(self, name="MyNode"):
        from unittest.mock import AsyncMock

        from meshcore import EventType

        def result(event_type=EventType.MSG_SENT):
            res = MagicMock()
            res.type = event_type
            res.payload = {}
            return res

        mc = MagicMock()
        mc.self_info = {"name": name}
        mc.commands = MagicMock()
        mc.commands.set_flood_scope = AsyncMock(return_value=result())
        mc.commands.send = AsyncMock(return_value=result())
        mc.commands.send_msg = AsyncMock(return_value=result())
        mc.commands.send_chan_msg = AsyncMock(return_value=result())
        mc.commands.add_contact = AsyncMock(return_value=result())
        mc.commands.reset_path = AsyncMock(return_value=result(EventType.OK))
        mc.commands.set_channel = AsyncMock(return_value=result())
        mc.get_contact_by_key_prefix = MagicMock(return_value=None)
        return mc

    @pytest.mark.asyncio
    async def test_react_to_channel_message(self, test_db, captured_broadcasts):
        from app.models import ReactToMessageRequest
        from app.radio import radio_manager
        from app.routers.messages import react_to_message

        broadcasts, mock_broadcast = captured_broadcasts
        await ChannelRepository.upsert(key=CHANNEL_KEY.lower(), name="#general")
        target_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: Hello world",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS,
            received_at=TS,
            sender_name="Alice",
        )

        mc = self._mock_mc()
        with (
            patch("app.routers.messages.radio_manager.require_connected", return_value=mc),
            patch.object(radio_manager, "_meshcore", mc),
            patch("app.routers.messages.broadcast_event", side_effect=mock_broadcast),
            patch(
                "app.services.message_send.broadcast_message"
            ),  # reaction rows never broadcast anyway; belt and suspenders
        ):
            updated = await react_to_message(target_id, ReactToMessageRequest(emoji="🔥"))

        assert updated.id == target_id
        assert updated.reactions == {"🔥": 1}

        # The wire text is the MCO Advanced format, computed from the target.
        sent_text = mc.commands.send_chan_msg.await_args.kwargs["msg"]
        expected_hash = compute_reaction_hash(TS, "Alice", "Hello world")
        assert sent_text == f"r:{expected_hash}:{emoji_to_index_hex('🔥')}"

        # The stored outgoing reaction row is hidden.
        listed = await MessageRepository.get_all(conversation_key=CHANNEL_KEY)
        assert [m.id for m in listed] == [target_id]
        assert [b for b in broadcasts if b["type"] == "message_reaction"]

    @pytest.mark.asyncio
    async def test_react_rejects_own_dm_message(self, test_db):
        from fastapi import HTTPException

        from app.models import ReactToMessageRequest
        from app.routers.messages import react_to_message

        await ContactRepository.upsert({"public_key": CONTACT_PUB, "name": "Peer", "type": 1})
        own_id = await MessageRepository.create(
            msg_type="PRIV",
            text="my own message",
            conversation_key=CONTACT_PUB,
            sender_timestamp=TS,
            received_at=TS,
            outgoing=True,
        )

        with patch("app.routers.messages.radio_manager.require_connected"):
            with pytest.raises(HTTPException) as exc_info:
                await react_to_message(own_id, ReactToMessageRequest(emoji="👍"))
        assert exc_info.value.status_code == 400
        assert "react" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_react_rejects_unknown_emoji(self, test_db):
        from fastapi import HTTPException

        from app.models import ReactToMessageRequest
        from app.routers.messages import react_to_message

        msg_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: Hello",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS,
            received_at=TS,
        )
        with patch("app.routers.messages.radio_manager.require_connected"):
            with pytest.raises(HTTPException) as exc_info:
                await react_to_message(msg_id, ReactToMessageRequest(emoji="🦄"))
        assert exc_info.value.status_code == 400
        assert "table" in exc_info.value.detail.lower()


class TestApplyReactionWindow:
    @pytest.mark.asyncio
    async def test_fallback_target_catches_out_of_window_message(
        self, test_db, captured_broadcasts
    ):
        _, mock_broadcast = captured_broadcasts

        target_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: ancient message",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=TS,
            received_at=TS,
        )
        target = await MessageRepository.get_by_id(target_id)

        with patch("app.reactions.REACTION_MATCH_SCAN_LIMIT", 0):
            updated = await apply_reaction(
                msg_type="CHAN",
                conversation_key=CHANNEL_KEY,
                reaction=ReactionInfo(compute_reaction_hash(TS, "Alice", "ancient message"), "👍"),
                reactor_is_self=True,
                broadcast_fn=mock_broadcast,
                fallback_target=target,
            )
        assert updated is not None and updated.reactions == {"👍": 1}
