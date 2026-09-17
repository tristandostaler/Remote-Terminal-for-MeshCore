"""Tests for the packets router.

Covers the historical channel decryption endpoint, background task,
undecrypted count endpoint, and the maintenance endpoint.
"""

import time
from hashlib import sha256
from unittest.mock import AsyncMock, patch

import pytest

from app.repository import ChannelRepository, MessageRepository, RawPacketRepository
from app.services.historical_decrypt import (
    ChannelTarget,
    SweepSubmission,
    get_status,
    submit_channel_sweep,
    wait_until_idle,
)


async def _insert_raw_packets(count: int, decrypted: bool = False, age_days: int = 0) -> list[int]:
    """Insert raw packets and return their IDs."""
    ids = []
    base_ts = int(time.time()) - (age_days * 86400)
    for i in range(count):
        packet_id, _ = await RawPacketRepository.create(
            f"packet_data_{i}_{age_days}_{decrypted}".encode(), base_ts + i
        )
        if decrypted:
            # Create a message and link it
            msg_id = await MessageRepository.create(
                msg_type="CHAN",
                text=f"decrypted msg {i}",
                conversation_key="DEADBEEF" * 4,
                sender_timestamp=base_ts + i,
                received_at=base_ts + i,
            )
            if msg_id is not None:
                await RawPacketRepository.mark_decrypted(packet_id, msg_id)
        ids.append(packet_id)
    return ids


class TestUndecryptedCount:
    """Test GET /api/packets/undecrypted/count."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_empty(self, test_db, client):
        response = await client.get("/api/packets/undecrypted/count")

        assert response.status_code == 200
        assert response.json()["count"] == 0

    @pytest.mark.asyncio
    async def test_counts_only_undecrypted(self, test_db, client):
        await _insert_raw_packets(3, decrypted=False)
        await _insert_raw_packets(2, decrypted=True)

        response = await client.get("/api/packets/undecrypted/count")

        assert response.status_code == 200
        assert response.json()["count"] == 3


class TestRegionBackfill:
    """Test POST /api/packets/region-backfill."""

    @pytest.mark.asyncio
    async def test_returns_zero_counts_on_empty_db(self, test_db, client):
        response = await client.post("/api/packets/region-backfill")

        assert response.status_code == 200
        assert response.json() == {"scanned": 0, "scoped": 0, "named": 0}


class TestGetRawPacket:
    """Test GET /api/packets/{id}."""

    @pytest.mark.asyncio
    async def test_returns_404_when_missing(self, test_db, client):
        response = await client.get("/api/packets/999999")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_returns_linked_packet_details(self, test_db, client):
        channel_key = "DEADBEEF" * 4
        await ChannelRepository.upsert(key=channel_key, name="#ops", is_hashtag=False)
        packet_id, _ = await RawPacketRepository.create(b"\x09\x00test-packet", 1700000000)
        msg_id = await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: hello",
            conversation_key=channel_key,
            sender_timestamp=1700000000,
            received_at=1700000000,
            sender_name="Alice",
        )
        assert msg_id is not None
        await RawPacketRepository.mark_decrypted(packet_id, msg_id)

        response = await client.get(f"/api/packets/{packet_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == packet_id
        assert data["timestamp"] == 1700000000
        assert data["data"] == "0900746573742d7061636b6574"
        assert data["decrypted"] is True
        assert data["decrypted_info"] == {
            "channel_name": "#ops",
            "sender": "Alice",
            "channel_key": channel_key,
            "contact_key": None,
            "sender_timestamp": 1700000000,
            "message": "Alice: hello",
        }


class TestDecryptHistoricalPackets:
    """Test POST /api/packets/decrypt/historical."""

    @pytest.mark.asyncio
    async def test_channel_decrypt_with_hex_key(self, test_db, client):
        """Channel decryption with a valid hex key starts background task."""
        await _insert_raw_packets(5)

        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "channel",
                "channel_key": "0123456789abcdef0123456789abcdef",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["started"] is True
        assert data["total_packets"] == 5
        assert "background" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_channel_decrypt_with_hashtag_name(self, test_db, client):
        """Channel decryption with a channel name derives key from hash."""
        await _insert_raw_packets(3)

        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "channel",
                "channel_name": "#general",
            },
        )

        assert response.status_code == 202
        data = response.json()
        assert data["started"] is True
        assert data["total_packets"] == 3

    @pytest.mark.asyncio
    async def test_channel_decrypt_invalid_hex(self, test_db, client):
        """Invalid hex string for channel key returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "channel",
                "channel_key": "not_valid_hex",
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "invalid" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_channel_decrypt_wrong_key_length(self, test_db, client):
        """Channel key with wrong length returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "channel",
                "channel_key": "aabbccdd",  # Only 4 bytes, need 16
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "16 bytes" in data["detail"]

    @pytest.mark.asyncio
    async def test_channel_decrypt_no_key_or_name(self, test_db, client):
        """Channel decryption without key or name returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={"key_type": "channel"},
        )

        assert response.status_code == 400
        data = response.json()
        assert "must provide" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_channel_decrypt_no_undecrypted_packets(self, test_db, client):
        """Channel decryption with no undecrypted packets returns not started."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "channel",
                "channel_key": "0123456789abcdef0123456789abcdef",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["started"] is False
        assert data["total_packets"] == 0

    @pytest.mark.asyncio
    async def test_channel_decrypt_resolves_channel_name(self, test_db, client):
        """Channel decryption finds display name from DB when channel exists."""
        key_hex = "0123456789ABCDEF0123456789ABCDEF"
        await ChannelRepository.upsert(key=key_hex, name="#test-channel", is_hashtag=True)
        await _insert_raw_packets(1)

        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "channel",
                "channel_key": key_hex.lower(),
            },
        )

        assert response.status_code == 202
        assert response.json()["started"] is True

    @pytest.mark.asyncio
    async def test_contact_decrypt_missing_private_key(self, test_db, client):
        """Contact decryption without private key returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "contact",
                "contact_public_key": "aa" * 32,
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "private_key" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_contact_decrypt_missing_contact_key(self, test_db, client):
        """Contact decryption without contact public key returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "contact",
                "private_key": "aa" * 64,
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "contact_public_key" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_contact_decrypt_wrong_private_key_length(self, test_db, client):
        """Private key with wrong length returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "contact",
                "private_key": "aa" * 32,  # 32 bytes, need 64
                "contact_public_key": "bb" * 32,
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "64 bytes" in data["detail"]

    @pytest.mark.asyncio
    async def test_contact_decrypt_wrong_public_key_length(self, test_db, client):
        """Contact public key with wrong length returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "contact",
                "private_key": "aa" * 64,
                "contact_public_key": "bb" * 16,  # 16 bytes, need 32
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "32 bytes" in data["detail"]

    @pytest.mark.asyncio
    async def test_contact_decrypt_invalid_hex(self, test_db, client):
        """Invalid hex for private key returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={
                "key_type": "contact",
                "private_key": "zz" * 64,
                "contact_public_key": "bb" * 32,
            },
        )

        assert response.status_code == 400
        data = response.json()
        assert "invalid" in data["detail"].lower()

    @pytest.mark.asyncio
    async def test_invalid_key_type(self, test_db, client):
        """Invalid key_type returns error."""
        response = await client.post(
            "/api/packets/decrypt/historical",
            json={"key_type": "invalid"},
        )

        assert response.status_code == 400
        data = response.json()
        assert "key_type" in data["detail"].lower()


class TestUndecryptedTextPacketStreaming:
    @pytest.mark.asyncio
    async def test_count_undecrypted_text_messages_uses_keyset_pagination(self, test_db):
        """Counting undecrypted DM packets should use keyset pagination and filter by payload type."""

        # Simulate keyset pagination: each execute() call returns a cursor
        # whose fetchall() yields one batch.  The generator stops when a
        # batch is empty.
        batches = [
            [
                {"id": 1, "data": b"\x09\x00dm", "timestamp": 1000},
                {"id": 2, "data": b"\x15\x00chan", "timestamp": 1001},
            ],
            [{"id": 3, "data": b"\x09\x00dm2", "timestamp": 1002}],
            [],
        ]

        def fake_execute(*_args, **_kwargs):
            batch = batches.pop(0)

            class FakeCursor:
                async def fetchall(self):
                    return batch

                async def close(self):
                    pass

                async def __aenter__(self):
                    return self

                async def __aexit__(self, exc_type, exc, tb):
                    return None

            # aiosqlite's execute() returns a `contextmanager`-decorated
            # coroutine that is both awaitable and usable as an async-with.
            # Our repo code now uses `async with conn.execute(...) as cursor:`,
            # so the mock just needs to return something with __aenter__/__aexit__.
            return FakeCursor()

        with patch.object(test_db.conn, "execute", side_effect=fake_execute):
            count = await RawPacketRepository.count_undecrypted_text_messages(batch_size=2)

        # header byte 0x09 -> payload type 2 (TEXT_MESSAGE); 0x15 -> type 5 (not TEXT_MESSAGE)
        assert count == 2


def _group_text_bytes(key_bytes: bytes, suffix: bytes) -> bytes:
    """A GROUP_TEXT-shaped packet addressed to ``key_bytes``'s channel.

    Header 0x15 is FLOOD + GROUP_TEXT; the first payload byte is the channel
    hash the sweep indexes on. The ciphertext is junk -- tests inject the
    plaintext by patching ``decrypt_group_text`` -- but unique per call so the
    payload-hash dedup index does not collapse the rows.
    """
    channel_hash = sha256(key_bytes).digest()[0:1]
    return bytes([0x15, 0x00]) + channel_hash + b"\x00\x00" + suffix.ljust(16, b"\x00")


async def _insert_group_text_packets(key_bytes: bytes, count: int, age_days: int = 0) -> None:
    base_ts = int(time.time()) - (age_days * 86400)
    for i in range(count):
        await RawPacketRepository.create(_group_text_bytes(key_bytes, bytes([i + 1])), base_ts + i)


def _plaintext(sender: str, message: str, timestamp: int):
    return type(
        "DecryptResult", (), {"sender": sender, "message": message, "timestamp": timestamp}
    )()


class TestChannelSweep:
    """The channel sweep itself (app.services.historical_decrypt)."""

    KEY_HEX = "AABBCCDDAABBCCDDAABBCCDDAABBCCDD"
    KEY = bytes.fromhex(KEY_HEX)

    def _target(self, name: str = "#test") -> ChannelTarget:
        return ChannelTarget(key_bytes=self.KEY, key_hex=self.KEY_HEX, name=name)

    async def _sweep(self, *targets: ChannelTarget) -> None:
        """Queue a sweep and wait it out, as a request plus its worker would."""
        await submit_channel_sweep(list(targets))
        await wait_until_idle(timeout=10)

    @pytest.mark.asyncio
    async def test_decrypts_matching_packets(self, test_db):
        """Packets that match the key become messages and the operator is told."""
        await _insert_group_text_packets(self.KEY, 3)

        # Each packet needs unique content or message dedup collapses them.
        call_count = 0

        def make_unique_result(*_args, **_kwargs):
            nonlocal call_count
            call_count += 1
            return _plaintext(f"User{call_count}", f"Hello {call_count}", 1700000000 + call_count)

        with (
            patch(
                "app.services.historical_decrypt.decrypt_group_text",
                side_effect=make_unique_result,
            ),
            patch("app.services.historical_decrypt.broadcast_success") as mock_success,
        ):
            await self._sweep(self._target())

        mock_success.assert_called_once()
        assert "3" in mock_success.call_args[0][1]

    @pytest.mark.asyncio
    async def test_recovered_messages_are_tagged(self, test_db):
        """A recovered message keeps the packet's time but is marked recovered."""
        await _insert_group_text_packets(self.KEY, 1, age_days=2)

        with (
            patch(
                "app.services.historical_decrypt.decrypt_group_text",
                return_value=_plaintext("User", "found me", 1700000000),
            ),
            patch("app.services.historical_decrypt.broadcast_success"),
        ):
            await self._sweep(self._target())

        messages = await MessageRepository.get_all(
            msg_type="CHAN", conversation_key=self.KEY_HEX, limit=10
        )
        assert len(messages) == 1
        assert messages[0].recovered_at is not None
        # received_at stays the packet's own timestamp, not the sweep's clock.
        assert messages[0].received_at < messages[0].recovered_at

    @pytest.mark.asyncio
    async def test_skips_non_matching_packets(self, test_db):
        """Nothing decrypted means no toast."""
        await _insert_group_text_packets(self.KEY, 2)

        with (
            patch("app.services.historical_decrypt.decrypt_group_text", return_value=None),
            patch("app.services.historical_decrypt.broadcast_success") as mock_success,
        ):
            await self._sweep(self._target())

        mock_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_tries_keys_whose_channel_hash_matches(self, test_db):
        """A packet is decrypted only with keys that could possibly own it.

        The on-air channel hash is one byte of sha256(key), so a sweep across
        every room must not spend an AES attempt per room per packet -- and a
        packet that is not GroupText at all is never tried.
        """
        other = ChannelTarget(key_bytes=b"\x11" * 16, key_hex="11" * 16, name="#other")
        assert sha256(self.KEY).digest()[0] != sha256(other.key_bytes).digest()[0]
        await _insert_group_text_packets(self.KEY, 2)
        await _insert_raw_packets(3)  # not GroupText

        tried: list[bytes] = []

        def record(_payload, key_bytes):
            tried.append(key_bytes)
            return None

        with patch("app.services.historical_decrypt.decrypt_group_text", side_effect=record):
            await self._sweep(self._target(), other)

        assert tried == [self.KEY, self.KEY]

    @pytest.mark.asyncio
    async def test_no_packets_does_not_start(self, test_db):
        """An empty backlog is reported, not queued."""
        submission = await submit_channel_sweep([self._target()])

        assert submission.started is False
        assert submission.total_packets == 0

    @pytest.mark.asyncio
    async def test_display_name_fallback(self, test_db):
        """A sweep for a channel we have no name for is credited to its key prefix."""
        await _insert_group_text_packets(self.KEY, 1)

        with (
            patch(
                "app.services.historical_decrypt.decrypt_group_text",
                return_value=_plaintext("User", "msg", 1700000000),
            ),
            patch("app.services.historical_decrypt.broadcast_success") as mock_success,
        ):
            await self._sweep(self._target(name=self.KEY_HEX[:12]))

        assert self.KEY_HEX[:12] in mock_success.call_args[0][0]

    @pytest.mark.asyncio
    async def test_one_pass_credits_each_room_its_own_finds(self, test_db):
        """Several keys ride one scan, and each find lands in the right room."""
        second = ChannelTarget(key_bytes=b"\x11" * 16, key_hex="11" * 16, name="#other")
        await _insert_group_text_packets(self.KEY, 1)
        await _insert_group_text_packets(second.key_bytes, 2)

        counter = 0

        def decrypt(_payload, _key):
            nonlocal counter
            counter += 1
            return _plaintext("U", f"m{counter}", 1700000000 + counter)

        with (
            patch("app.services.historical_decrypt.decrypt_group_text", side_effect=decrypt),
            patch("app.services.historical_decrypt.broadcast_success"),
        ):
            await self._sweep(self._target(), second)

        mine = await MessageRepository.get_all(msg_type="CHAN", conversation_key=self.KEY_HEX)
        theirs = await MessageRepository.get_all(msg_type="CHAN", conversation_key=second.key_hex)
        assert (len(mine), len(theirs)) == (1, 2)


class TestSweepProgress:
    """Progress reporting and the queue behind it."""

    @pytest.mark.asyncio
    async def test_status_is_idle_before_any_sweep(self, test_db, client):
        response = await client.get("/api/packets/decrypt/status")

        assert response.status_code == 200
        data = response.json()
        assert data["active"] is None
        assert data["queued"] == 0

    @pytest.mark.asyncio
    async def test_progress_is_broadcast_and_throttled(self, test_db):
        """Ticks are rate-limited, but start and finish always go out."""
        await _insert_raw_packets(40)
        events: list[tuple[str, dict]] = []

        def capture(event_type, data, **_kwargs):
            events.append((event_type, data))

        with patch("app.services.historical_decrypt.broadcast_event", side_effect=capture):
            await submit_channel_sweep(
                [ChannelTarget(key_bytes=b"\xaa" * 16, key_hex="AA" * 16, name="#p")]
            )
            await wait_until_idle(timeout=10)

        progress = [data for kind, data in events if kind == "decrypt_progress"]
        # 40 packets is well under the 250-packet tick interval, so only the
        # bookend events fire.
        assert len(progress) == 2
        assert progress[0]["status"] == "running"
        assert progress[0]["processed"] == 0
        assert progress[-1]["status"] == "complete"
        assert progress[-1]["processed"] == 40
        assert progress[-1]["total"] == 40

    @pytest.mark.asyncio
    async def test_targets_report_per_conversation_counts(self, test_db):
        """The finished sweep says how many messages each room got back."""
        key = b"\xaa" * 16
        await _insert_group_text_packets(key, 2)

        counter = 0

        def always_matches(*_args, **_kwargs):
            nonlocal counter
            counter += 1
            return _plaintext("U", f"m{counter}", 1700000000 + counter)

        with (
            patch("app.services.historical_decrypt.decrypt_group_text", side_effect=always_matches),
            patch("app.services.historical_decrypt.broadcast_success"),
        ):
            await submit_channel_sweep(
                [ChannelTarget(key_bytes=key, key_hex="AA" * 16, name="#ops")]
            )
            await wait_until_idle(timeout=10)

        last = get_status().last
        assert last is not None
        assert last.status == "complete"
        assert last.decrypted == 2
        assert [(t.name, t.decrypted) for t in last.targets] == [("#ops", 2)]

    @pytest.mark.asyncio
    async def test_second_sweep_queues_behind_the_first(self, test_db):
        """Two sweeps of the same table run in sequence, not on top of each other."""
        await _insert_raw_packets(4)
        target = ChannelTarget(key_bytes=b"\xaa" * 16, key_hex="AA" * 16, name="#ops")

        first = await submit_channel_sweep([target])
        second = await submit_channel_sweep([target])
        assert first.queued == 0
        assert second.queued == 1  # waits for the first, rather than rescanning alongside it
        await wait_until_idle(timeout=10)

        assert get_status().queued == 0
        assert get_status().active is None


class TestDecryptAllChannels:
    """Test POST /api/packets/decrypt/historical/all-channels."""

    @pytest.mark.asyncio
    async def test_reports_when_no_channels_exist(self, test_db, client):
        with patch("app.routers.packets.ChannelRepository.get_all", new=AsyncMock(return_value=[])):
            response = await client.post("/api/packets/decrypt/historical/all-channels")

        assert response.status_code == 200
        data = response.json()
        assert data["started"] is False
        assert "no channel keys" in data["message"].lower()

    @pytest.mark.asyncio
    async def test_sweeps_every_known_channel_key(self, test_db, client):
        await ChannelRepository.upsert(key="AA" * 16, name="#ops", is_hashtag=True)
        await ChannelRepository.upsert(key="BB" * 16, name="#weather", is_hashtag=True)
        await _insert_raw_packets(5)

        with patch("app.services.historical_decrypt._runner.submit") as mock_submit:
            mock_submit.return_value = SweepSubmission(
                started=True, job_id="j", total_packets=5, queued=0, message="Started"
            )
            response = await client.post("/api/packets/decrypt/historical/all-channels")

        assert response.status_code == 202
        assert response.json()["started"] is True
        job = mock_submit.call_args[0][0]
        names = {target.name for target in job.channel_targets}
        assert {"#ops", "#weather"} <= names
        assert f"{len(names)} keys" in job.label

    @pytest.mark.asyncio
    async def test_skips_channels_with_unusable_keys(self, test_db, client):
        """A malformed stored key is logged past, not fatal to the sweep."""
        await ChannelRepository.upsert(key="AA" * 16, name="#ops", is_hashtag=True)
        await ChannelRepository.upsert(key="NOTHEX", name="#broken", is_hashtag=False)
        await _insert_raw_packets(1)

        with patch("app.services.historical_decrypt._runner.submit") as mock_submit:
            mock_submit.return_value = SweepSubmission(
                started=True, job_id="j", total_packets=1, queued=0, message="Started"
            )
            response = await client.post("/api/packets/decrypt/historical/all-channels")

        assert response.status_code == 202
        job = mock_submit.call_args[0][0]
        names = {target.name for target in job.channel_targets}
        assert "#ops" in names
        assert "#broken" not in names


class TestMaintenanceEndpoint:
    """Test POST /api/packets/maintenance."""

    @pytest.mark.asyncio
    async def test_prune_old_undecrypted(self, test_db, client):
        """Prune deletes undecrypted packets older than threshold."""
        await _insert_raw_packets(3, decrypted=False, age_days=30)
        await _insert_raw_packets(2, decrypted=False, age_days=0)

        response = await client.post(
            "/api/packets/maintenance",
            json={"prune_undecrypted_days": 7},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["packets_deleted"] == 3

        # Verify only recent packets remain
        remaining = await RawPacketRepository.get_undecrypted_count()
        assert remaining == 2

    @pytest.mark.asyncio
    async def test_purge_linked_raw_packets(self, test_db, client):
        """Purge deletes raw packets that are linked to stored messages."""
        await _insert_raw_packets(3, decrypted=True)
        await _insert_raw_packets(2, decrypted=False)

        response = await client.post(
            "/api/packets/maintenance",
            json={"purge_linked_raw_packets": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["packets_deleted"] == 3

        # Undecrypted packets should remain
        remaining = await RawPacketRepository.get_undecrypted_count()
        assert remaining == 2

    @pytest.mark.asyncio
    async def test_both_prune_and_purge(self, test_db, client):
        """Both prune and purge can run in a single request."""
        await _insert_raw_packets(2, decrypted=True)
        await _insert_raw_packets(3, decrypted=False, age_days=30)
        await _insert_raw_packets(1, decrypted=False, age_days=0)

        response = await client.post(
            "/api/packets/maintenance",
            json={
                "prune_undecrypted_days": 7,
                "purge_linked_raw_packets": True,
            },
        )

        assert response.status_code == 200
        data = response.json()
        # 2 linked + 3 old undecrypted = 5 deleted
        assert data["packets_deleted"] == 5

    @pytest.mark.asyncio
    async def test_no_options_deletes_nothing(self, test_db, client):
        """No options specified means no deletions (only vacuum)."""
        await _insert_raw_packets(5)

        response = await client.post(
            "/api/packets/maintenance",
            json={},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["packets_deleted"] == 0

    @pytest.mark.asyncio
    async def test_vacuum_reports_status(self, test_db, client):
        """Maintenance endpoint reports vacuum status."""
        response = await client.post(
            "/api/packets/maintenance",
            json={},
        )

        assert response.status_code == 200
        data = response.json()
        # vacuumed is a boolean (may be True or False depending on DB state)
        assert isinstance(data["vacuumed"], bool)

    @pytest.mark.asyncio
    async def test_prune_days_validation(self, test_db, client):
        """prune_undecrypted_days must be >= 1."""
        response = await client.post(
            "/api/packets/maintenance",
            json={"prune_undecrypted_days": 0},
        )

        assert response.status_code == 422
