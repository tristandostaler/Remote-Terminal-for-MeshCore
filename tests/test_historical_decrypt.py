"""Tests for historical decrypt sweeps and the recovered-message state they create.

The sweep loops themselves live in ``tests/test_packets_router.py`` (channel) and
``tests/test_packet_pipeline.py`` (contact). This file covers what recovery means
once the messages exist: they are marked, they read as unread even though they are
old, and the DM route can find the key without the browser holding it.
"""

import time
from unittest.mock import patch

import pytest

from app.keystore import clear_keys, set_private_key
from app.repository import (
    ChannelRepository,
    ContactRepository,
    MessageRepository,
    RawPacketRepository,
)
from app.services.historical_decrypt import (
    ChannelTarget,
    get_status,
    submit_channel_sweep,
    wait_until_idle,
)

CHANNEL_KEY = "AABBCCDDAABBCCDDAABBCCDDAABBCCDD"
CONTACT_KEY = "cc" * 32
# A real 64-byte expanded key (scalar || prefix), as the radio exports it.
PRIVATE_KEY = bytes.fromhex(
    "58BA1940E97099CBB4357C62CE9C7F4B245C94C90D722E67201B989F9FEACF7B"
    "77ACADDB84438514022BDB0FC3140C2501859BE1772AC7B8C7E41DC0F40490A1"
)


class TestRecoveredMessagesAreUnread:
    """A message recovered from an old packet has to be findable.

    Unread state is a ``last_read_at`` comparison, so a message stored with a
    three-week-old ``received_at`` lands below the boundary and would never be
    shown as new. ``recovered_at`` is the second clock the unread queries check.
    """

    @pytest.mark.asyncio
    async def test_recovered_channel_message_counts_as_unread(self, test_db):
        await ChannelRepository.upsert(key=CHANNEL_KEY, name="#ops", is_hashtag=True)
        await ChannelRepository.update_last_read_at(CHANNEL_KEY, 5000)

        # Heard long before the last read mark, recovered just now.
        await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: from the past",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=1000,
            received_at=1000,
            recovered_at=6000,
        )

        result = await MessageRepository.get_unread_counts(None)

        assert result["counts"][f"channel-{CHANNEL_KEY}"] == 1

    @pytest.mark.asyncio
    async def test_recovered_dm_counts_as_unread(self, test_db):
        await ContactRepository.upsert({"public_key": CONTACT_KEY, "name": "Alice", "type": 1})
        await ContactRepository.update_last_read_at(CONTACT_KEY, 5000)

        await MessageRepository.create(
            msg_type="PRIV",
            text="missed you",
            conversation_key=CONTACT_KEY,
            sender_timestamp=1000,
            received_at=1000,
            recovered_at=6000,
        )

        result = await MessageRepository.get_unread_counts(None)

        assert result["counts"][f"contact-{CONTACT_KEY}"] == 1

    @pytest.mark.asyncio
    async def test_reading_the_conversation_clears_it(self, test_db):
        """The recovered message is unread until seen, then it stays read."""
        await ChannelRepository.upsert(key=CHANNEL_KEY, name="#ops", is_hashtag=True)
        await ChannelRepository.update_last_read_at(CHANNEL_KEY, 5000)
        await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: from the past",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=1000,
            received_at=1000,
            recovered_at=6000,
        )

        await ChannelRepository.update_last_read_at(CHANNEL_KEY, 7000)
        result = await MessageRepository.get_unread_counts(None)

        assert f"channel-{CHANNEL_KEY}" not in result["counts"]

    @pytest.mark.asyncio
    async def test_old_messages_without_recovery_stay_read(self, test_db):
        """Ordinary history is not resurrected -- only rows a sweep just stored."""
        await ChannelRepository.upsert(key=CHANNEL_KEY, name="#ops", is_hashtag=True)
        await ChannelRepository.update_last_read_at(CHANNEL_KEY, 5000)
        await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: ancient",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=1000,
            received_at=1000,
        )

        result = await MessageRepository.get_unread_counts(None)

        assert f"channel-{CHANNEL_KEY}" not in result["counts"]

    @pytest.mark.asyncio
    async def test_unread_divider_anchors_at_the_chronological_position(self, test_db):
        """The boundary is the oldest unread message by when it was *heard*.

        A message recovered from an old packet belongs in its own place in the
        conversation, not at the bottom, so reading from the divider replays
        history in order.
        """
        await ChannelRepository.upsert(key=CHANNEL_KEY, name="#ops", is_hashtag=True)
        await ChannelRepository.update_last_read_at(CHANNEL_KEY, 5000)

        recovered_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: recovered",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=1000,
            received_at=1000,
            recovered_at=6000,
        )
        await MessageRepository.create(
            msg_type="CHAN",
            text="Bob: live",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=5500,
            received_at=5500,
        )

        result = await MessageRepository.get_unread_counts(None)

        assert result["counts"][f"channel-{CHANNEL_KEY}"] == 2
        assert result["first_unread_ids"][f"channel-{CHANNEL_KEY}"] == recovered_id

    @pytest.mark.asyncio
    async def test_recovery_does_not_move_the_conversation_up_the_sidebar(self, test_db):
        """last_message_times stays the truth about when traffic was heard."""
        await ChannelRepository.upsert(key=CHANNEL_KEY, name="#ops", is_hashtag=True)
        await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: from the past",
            conversation_key=CHANNEL_KEY,
            sender_timestamp=1000,
            received_at=1000,
            recovered_at=int(time.time()),
        )

        result = await MessageRepository.get_unread_counts(None)

        assert result["last_message_times"][f"channel-{CHANNEL_KEY}"] == 1000


class TestContactSweepKeySource:
    """The DM sweep endpoint should not need the node's private key from a browser."""

    @pytest.mark.asyncio
    async def test_uses_the_stored_radio_key_when_none_is_sent(self, test_db, client):
        await RawPacketRepository.create(b"\x09\x00some-dm-bytes", int(time.time()))
        await ContactRepository.upsert({"public_key": CONTACT_KEY, "name": "Alice", "type": 1})
        set_private_key(PRIVATE_KEY)
        try:
            with patch("app.services.historical_decrypt._runner.submit") as mock_submit:
                mock_submit.return_value = type(
                    "S",
                    (),
                    {"started": True, "total_packets": 1, "message": "Started", "queued": 0},
                )()
                response = await client.post(
                    "/api/packets/decrypt/historical",
                    json={"key_type": "contact", "contact_public_key": CONTACT_KEY},
                )
        finally:
            clear_keys()

        assert response.status_code == 202
        job = mock_submit.call_args[0][0]
        assert job.contact_target is not None
        assert job.contact_target.private_key == PRIVATE_KEY
        assert job.contact_target.name == "Alice"

    @pytest.mark.asyncio
    async def test_says_so_when_no_key_is_available(self, test_db, client):
        clear_keys()
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={"key_type": "contact", "contact_public_key": CONTACT_KEY},
        )

        assert response.status_code == 400
        assert "private_key" in response.json()["detail"].lower()


class TestSweepStatusEndpoint:
    """A client that loads mid-sweep, or just after one, can still see it."""

    @pytest.mark.asyncio
    async def test_reports_the_finished_sweep(self, test_db, client):
        await RawPacketRepository.create(b"\x15\x00packet-bytes", int(time.time()))

        with patch("app.services.historical_decrypt.broadcast_event"):
            await submit_channel_sweep(
                [
                    ChannelTarget(
                        key_bytes=bytes.fromhex(CHANNEL_KEY), key_hex=CHANNEL_KEY, name="#ops"
                    )
                ],
                label="All rooms (1 key)",
            )
            await wait_until_idle(timeout=10)

        response = await client.get("/api/packets/decrypt/status")

        assert response.status_code == 200
        data = response.json()
        assert data["active"] is None
        assert data["queued"] == 0
        assert data["last"]["label"] == "All rooms (1 key)"
        assert data["last"]["status"] == "complete"
        assert data["last"]["processed"] == 1
        assert data["last"]["decrypted"] == 0
        assert get_status().active is None
