"""Live feed comparison: mirroring live.meshcore.ca (CoreScope) and comparing with our node."""

import asyncio
import json
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from app.channel_constants import PUBLIC_CHANNEL_KEY, hashtag_channel_key
from app.models import MIN_LIVE_FEED_POLL_INTERVAL
from app.repository import AppSettingsRepository, ChannelRepository, MessageRepository
from app.repository.live_feed import LiveFeedRepository
from app.services import live_feed

NOW = int(time.time())


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


def _live_message(sender: str, text: str, ts: int, *, seen: int | None = None, **extra) -> dict:
    """One message as CoreScope's ``/api/channels/{name}/messages`` returns it."""
    seen = seen if seen is not None else ts + 3
    payload = {
        "sender": sender,
        "text": text,
        "timestamp": _iso(seen),
        "sender_timestamp": ts,
        "packetId": ts,
        "packetHash": f"{ts:016x}",
        "repeats": 2,
        "observers": ["obs-a", "obs-b"],
        "hops": 1,
        "snr": 7.5,
        "scope_name": None,
    }
    payload.update(extra)
    return payload


PUBLIC_KEY_BYTES = bytes.fromhex(PUBLIC_CHANNEL_KEY)
SECRET_KEY_HEX = "CD" * 16
SECRET_KEY_BYTES = bytes.fromhex(SECRET_KEY_HEX)


def encrypt_group_text(channel_key: bytes, timestamp: int, sender: str, message: str) -> bytes:
    """A complete FLOOD/GRP_TXT packet, encrypted the way the firmware does it."""
    import hashlib
    import hmac

    from Crypto.Cipher import AES

    plaintext = (
        timestamp.to_bytes(4, "little") + b"\x00" + f"{sender}: {message}".encode() + b"\x00"
    )
    pad = (16 - len(plaintext) % 16) % 16 or 16
    plaintext += bytes(pad)
    ciphertext = AES.new(channel_key, AES.MODE_ECB).encrypt(plaintext)
    mac = hmac.new(channel_key + bytes(16), ciphertext, hashlib.sha256).digest()[:2]
    payload = hashlib.sha256(channel_key).digest()[0:1] + mac + ciphertext
    return bytes([0x15, 0x00]) + payload


def _live_packet(message: dict, channel_key: bytes) -> dict:
    """The ``/api/packets`` row CoreScope would serve for one of our fake messages."""
    raw = encrypt_group_text(
        channel_key, message["sender_timestamp"], message["sender"], message["text"]
    )
    return {
        "id": message["packetId"],
        "raw_hex": raw.hex(),
        "hash": message["packetHash"],
        "first_seen": message["timestamp"],
        "timestamp": message["timestamp"],
        "route_type": 1,
        "payload_type": 5,
        "observation_count": message["repeats"],
        "observer_name": message["observers"][0] if message["observers"] else None,
        "snr": message["snr"],
        "path_json": json.dumps(["ab"] * message["hops"]),
    }


class FakeCoreScope:
    """An httpx mock transport that serves CoreScope's packet and channel endpoints.

    ``channels`` maps a remote channel name to server-decrypted messages; each
    is also served as an encrypted packet built with ``keys[name]`` (Public by
    default), so the same fixture exercises both the packet path and the
    channel-messages fallback.
    """

    def __init__(
        self,
        channels: dict[str, list[dict]],
        regions: dict[str, str] | None = None,
        keys: dict[str, bytes] | None = None,
    ):
        self.channels = channels
        self.keys = keys or {}
        self.regions = regions if regions is not None else {"YUL": "Montréal, CA"}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None
        # 404 the packet feed (an older instance) -> the fallback path.
        self.packets_enabled = True
        # Serve packets without raw bytes or ciphertext (a locked-down instance).
        self.strip_ciphertext = False
        # Serve the encrypted envelope in decoded_json instead of raw_hex.
        self.envelope_only = False
        # Drop the connection on this many packet requests before serving normally.
        self.drop_packet_requests = 0
        # Drop the connection for packet slices entirely older than this timestamp.
        self.drop_slices_older_than: int | None = None
        # Drop every request whose User-Agent lacks this substring (a UA-blocking proxy).
        self.drop_user_agents_without: str | None = None

    def all_packets(self) -> list[dict]:
        packets: list[dict] = []
        for name, messages in self.channels.items():
            key = self.keys.get(name, PUBLIC_KEY_BYTES)
            for message in messages:
                packet = _live_packet(message, key)
                if self.envelope_only:
                    raw = bytes.fromhex(packet.pop("raw_hex"))
                    payload = raw[2:]
                    packet["decoded_json"] = json.dumps(
                        {
                            "type": "GRP_TXT",
                            "channelHash": payload[0],
                            "mac": payload[1:3].hex(),
                            "encryptedData": payload[3:].hex(),
                        }
                    )
                if self.strip_ciphertext:
                    packet["raw_hex"] = None
                    packet["decoded_json"] = json.dumps({"type": "GRP_TXT"})
                packets.append(packet)
        packets.sort(key=lambda p: p["timestamp"], reverse=True)
        return packets

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text="nope")
        if (
            self.drop_user_agents_without is not None
            and self.drop_user_agents_without not in request.headers.get("user-agent", "")
        ):
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        path = request.url.path
        if path == "/api/config/regions":
            return httpx.Response(200, json=self.regions)
        if path == "/api/packets":
            if not self.packets_enabled:
                return httpx.Response(404, json={"error": "not found"})
            assert request.url.params.get("type") == "5"
            assert "RemoteTerm-LiveCompare/" in request.headers.get("user-agent", "")
            since = live_feed._parse_iso(request.url.params.get("since")) or 0
            until = live_feed._parse_iso(request.url.params.get("until")) or 2**40
            if self.drop_packet_requests > 0:
                self.drop_packet_requests -= 1
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            if self.drop_slices_older_than is not None and until <= self.drop_slices_older_than:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            packets = [
                p
                for p in self.all_packets()
                if since <= (live_feed._parse_iso(p["timestamp"]) or 0) <= until
            ]
            limit = int(request.url.params.get("limit", "50"))
            offset = int(request.url.params.get("offset", "0"))
            return httpx.Response(
                200, json={"packets": packets[offset : offset + limit], "total": len(packets)}
            )
        if path.startswith("/api/channels/") and path.endswith("/messages"):
            name = httpx.URL(str(request.url)).path.split("/")[3]
            from urllib.parse import unquote

            messages = self.channels.get(unquote(name), [])
            limit = int(request.url.params.get("limit", "100"))
            offset = int(request.url.params.get("offset", "0"))
            return httpx.Response(
                200,
                json={"messages": messages[offset : offset + limit], "total": len(messages)},
            )
        return httpx.Response(404, json={"error": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _reset_live_feed(monkeypatch):
    live_feed._reset_for_tests()
    # Real retry backoff would add seconds to every failure-path test.
    monkeypatch.setattr(live_feed, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    yield
    live_feed._reset_for_tests()


async def _enable(**overrides):
    fields = {
        "live_feed_enabled": True,
        "live_feed_url": "https://live.example.test",
        "live_feed_channels": ["Public"],
    }
    fields.update(overrides)
    return await AppSettingsRepository.update(**fields)


async def _local_channel_message(text: str, ts: int, *, received: int | None = None, **kw):
    return await MessageRepository.create(
        msg_type="CHAN",
        text=text,
        received_at=received if received is not None else ts + 1,
        conversation_key=PUBLIC_CHANNEL_KEY,
        sender_timestamp=ts,
        sender_name=text.split(":")[0],
        **kw,
    )


# ─── Normalization ─────────────────────────────────────────────────────────


class TestNormalizeLiveMessage:
    def test_rebuilds_the_local_sender_prefixed_text(self):
        row = live_feed.normalize_live_message(
            _live_message("Alice", "hello mesh", 1_700_000_000), "Public", PUBLIC_CHANNEL_KEY
        )
        assert row is not None
        assert row["text"] == "Alice: hello mesh"
        assert row["sender"] == "Alice"
        assert row["channel_key"] == PUBLIC_CHANNEL_KEY
        assert row["sender_timestamp"] == 1_700_000_000
        assert row["first_seen"] == 1_700_000_003
        assert row["observers"] == ["obs-a", "obs-b"]
        assert row["packet_hash"] == f"{1_700_000_000:016x}"

    def test_does_not_double_prefix_when_text_already_carries_the_sender(self):
        row = live_feed.normalize_live_message(
            _live_message("Alice", "Alice: hello mesh", 1), "Public", PUBLIC_CHANNEL_KEY
        )
        assert row is not None
        assert row["text"] == "Alice: hello mesh"

    def test_unknown_sender_is_dropped_and_text_kept_verbatim(self):
        row = live_feed.normalize_live_message(
            _live_message("Unknown", "Bob: hi", 1), "Public", PUBLIC_CHANNEL_KEY
        )
        assert row is not None
        assert row["sender"] is None
        assert row["text"] == "Bob: hi"

    def test_missing_hash_gets_a_deterministic_synthetic_one(self):
        raw = _live_message("Alice", "x", 5)
        del raw["packetHash"]
        a = live_feed.normalize_live_message(dict(raw), "Public", None)
        b = live_feed.normalize_live_message(dict(raw), "Public", None)
        assert a is not None and b is not None
        assert a["packet_hash"] == b["packet_hash"]
        assert a["packet_hash"].startswith("syn:")

    def test_unusable_rows_are_skipped(self):
        assert live_feed.normalize_live_message({"sender": "A"}, "Public", None) is None
        assert live_feed.normalize_live_message({"text": "   "}, "Public", None) is None

    def test_region_and_channel_normalization(self):
        assert live_feed.normalize_region(" yul, yqb ,") == "YUL,YQB"
        assert live_feed.normalize_region("") == ""
        assert live_feed.normalize_channel_names([" Public", "public", "#bot", "", "#bot "]) == [
            "Public",
            "#bot",
        ]


class TestResolveComparedChannels:
    @pytest.mark.asyncio
    async def test_public_and_hashtag_channels_resolve_without_local_rows(self, test_db):
        channels, unresolved = await live_feed.resolve_compared_channels(
            ["Public", "#bot", "Secret"]
        )
        assert [(c.key, c.name) for c in channels] == [
            (PUBLIC_CHANNEL_KEY, "Public"),
            (hashtag_channel_key("#bot"), "#bot"),
        ]
        assert unresolved == ["Secret"]
        assert channels[0].remote_name == "Public"
        assert channels[1].remote_name == "#bot"

    @pytest.mark.asyncio
    async def test_keys_and_local_names_resolve_and_private_channels_have_no_remote_name(
        self, test_db
    ):
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        channels, unresolved = await live_feed.resolve_compared_channels(
            ["secret", SECRET_KEY_HEX.lower(), PUBLIC_CHANNEL_KEY]
        )
        assert unresolved == []
        assert [(c.key, c.name) for c in channels] == [
            (SECRET_KEY_HEX, "Secret"),
            (PUBLIC_CHANNEL_KEY, "Public"),
        ]
        assert channels[0].remote_name is None

    @pytest.mark.asyncio
    async def test_star_means_every_local_channel_plus_public(self, test_db):
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        await ChannelRepository.upsert(hashtag_channel_key("#bot"), "#bot", is_hashtag=True)
        channels, unresolved = await live_feed.resolve_compared_channels(["*"])
        assert unresolved == []
        # Every local channel (the test DB seeds #remoteterm) plus Public.
        assert {c.name for c in channels} >= {"Secret", "#bot", "Public", "#remoteterm"}


# ─── Sync + comparison ─────────────────────────────────────────────────────


class TestSyncAndCompare:
    @pytest.mark.asyncio
    async def test_sync_mirrors_messages_and_classifies_them(self, test_db):
        fake = FakeCoreScope(
            {
                "Public": [
                    _live_message("Alice", "seen by both", NOW - 100),
                    _live_message("Bob", "only the mesh heard this", NOW - 200),
                ]
            }
        )
        live_feed._transport = fake.transport
        await _local_channel_message("Alice: seen by both", NOW - 100)
        await _local_channel_message("Carol: only we heard this", NOW - 300)

        state = await live_feed.sync_once(await _enable(live_feed_region=" yul "))

        assert state.last_error is None
        assert state.last_fetched == 2
        assert state.source == "packets"
        assert state.unresolved_channels == []
        assert await LiveFeedRepository.count() == 2
        # Packets are fetched once for every channel, with the region filter
        # upper-cased and trimmed, and never via the per-channel endpoint.
        assert fake.requests[0].url.path == "/api/packets"
        assert fake.requests[0].url.params["region"] == "YUL"
        assert not any(r.url.path.startswith("/api/channels/") for r in fake.requests)

        stats = await live_feed.get_compare_stats("1d")
        assert stats is not None
        assert (stats["both"], stats["node_only"], stats["live_only"]) == (1, 1, 1)
        assert stats["node_coverage_pct"] == 50.0
        assert stats["live_coverage_pct"] == 50.0
        assert stats["channels"] == [
            {
                "channel_key": PUBLIC_CHANNEL_KEY,
                "channel_name": "Public",
                "both": 1,
                "node_only": 1,
                "live_only": 1,
            }
        ]
        assert sum(b["both"] + b["node_only"] + b["live_only"] for b in stats["over_time"]) == 3
        assert stats["status"]["enabled"] is True
        assert stats["status"]["region"] == "YUL"
        assert stats["status"]["mirrored_messages"] == 2

    @pytest.mark.asyncio
    async def test_merged_listing_has_no_duplicates_and_marks_sources(self, test_db):
        fake = FakeCoreScope(
            {
                "Public": [
                    _live_message("Alice", "seen by both", NOW - 100),
                    _live_message("Bob", "only the mesh heard this", NOW - 200),
                ]
            }
        )
        live_feed._transport = fake.transport
        await _local_channel_message("Alice: seen by both", NOW - 100)
        await _local_channel_message("Carol: only we heard this", NOW - 300, outgoing=True)
        await live_feed.sync_once(await _enable())

        result = await live_feed.list_messages("1d")
        by_text = {m["text"]: m for m in result["messages"]}
        assert result["total"] == 3
        assert result["counts"] == {"both": 1, "node_only": 1, "live_only": 1}
        assert [m["text"] for m in result["messages"]] == [
            "Alice: seen by both",
            "Bob: only the mesh heard this",
            "Carol: only we heard this",
        ]
        both = by_text["Alice: seen by both"]
        assert both["source"] == "both"
        assert both["message_id"] is not None
        # The packet feed names the first observer and counts the observations.
        assert both["live_observers"] == ["obs-a"]
        assert both["live_repeats"] == 2
        # Earliest of the two sides: we received it at ts+1, the mesh at ts+3.
        assert both["seen_at"] == NOW - 99
        assert by_text["Bob: only the mesh heard this"]["source"] == "live"
        assert by_text["Bob: only the mesh heard this"]["message_id"] is None
        node_only = by_text["Carol: only we heard this"]
        assert node_only["source"] == "node"
        assert node_only["outgoing"] is True
        assert node_only["live_first_seen"] is None

        only_live = await live_feed.list_messages("1d", source="live")
        assert [m["source"] for m in only_live["messages"]] == ["live"]
        assert only_live["total"] == 1
        # Counts ignore the source filter so every chip can show its number.
        assert only_live["counts"] == {"both": 1, "node_only": 1, "live_only": 1}

        searched = await live_feed.list_messages("1d", q="mesh heard")
        assert [m["text"] for m in searched["messages"]] == ["Bob: only the mesh heard this"]

        narrow = await live_feed.list_messages("1h", channel_key=PUBLIC_CHANNEL_KEY.lower())
        assert narrow["total"] == 3

    @pytest.mark.asyncio
    async def test_window_excludes_old_messages_on_both_sides(self, test_db):
        old = NOW - 3 * 86400
        fake = FakeCoreScope({"Public": [_live_message("Old", "live long ago", old)]})
        live_feed._transport = fake.transport
        await _local_channel_message("Older: node long ago", old)
        await live_feed.sync_once(await _enable())

        assert (await live_feed.list_messages("1d"))["total"] == 0
        assert (await live_feed.list_messages("1w"))["total"] == 2
        stats_all = await live_feed.get_compare_stats("all")
        assert stats_all is not None
        assert stats_all["live_only"] == 1 and stats_all["node_only"] == 1

    @pytest.mark.asyncio
    async def test_a_late_local_decrypt_flips_live_only_to_both(self, test_db):
        fake = FakeCoreScope({"Public": [_live_message("Alice", "late", NOW - 50)]})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable())
        assert (await live_feed.list_messages("1d"))["messages"][0]["source"] == "live"

        # Historical decrypt stores the same plaintext later: the join is on
        # content, so no re-sync is needed for the verdict to change.
        await _local_channel_message("Alice: late", NOW - 50, received=NOW)
        message = (await live_feed.list_messages("1d"))["messages"][0]
        assert message["source"] == "both"
        assert message["seen_at"] == NOW - 47  # the mesh saw it first

    @pytest.mark.asyncio
    async def test_resync_is_idempotent_and_refreshes_observations(self, test_db):
        first = _live_message("Alice", "hi", NOW - 10, repeats=1, observers=["a"])
        fake = FakeCoreScope({"Public": [first]})
        live_feed._transport = fake.transport
        settings = await _enable()
        await live_feed.sync_once(settings)
        fake.channels["Public"] = [
            _live_message("Alice", "hi", NOW - 10, repeats=3, observers=["a", "b", "c"])
        ]
        await live_feed.sync_once(settings)

        assert await LiveFeedRepository.count() == 1
        message = (await live_feed.list_messages("1d"))["messages"][0]
        assert message["live_repeats"] == 3
        assert message["live_observers"] == ["a"]

    @pytest.mark.asyncio
    async def test_packet_feed_is_paged_and_bounded_by_the_lookback(self, test_db, monkeypatch):
        monkeypatch.setattr(live_feed, "PAGE_LIMIT", 2)
        monkeypatch.setattr(live_feed, "PACKET_SLICE_SECONDS", live_feed.LOOKBACK_SECONDS)
        # Observed a little before the sync starts: the walk is bounded by
        # ``until = sync start``, and anything newer is the next poll's business.
        recent = [_live_message("A", f"m{i}", NOW - 30 - i) for i in range(5)]
        ancient = [
            _live_message("Z", f"z{i}", NOW - live_feed.LOOKBACK_SECONDS - 86400 * (i + 1))
            for i in range(4)
        ]
        fake = FakeCoreScope({"Public": recent + ancient})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable())

        # since= keeps the ancient packets out; 5 recent rows take 3 pages.
        assert [r.url.params["offset"] for r in fake.requests] == ["0", "2", "4"]
        assert all(r.url.params["since"] for r in fake.requests)
        assert await LiveFeedRepository.count() == 5

    @pytest.mark.asyncio
    async def test_private_channels_compare_through_local_decryption(self, test_db):
        """The remote instance has no key for Secret; we do, so its packets decrypt here."""
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        fake = FakeCoreScope(
            {
                "Secret": [_live_message("Zed", "private and heard by both", NOW - 40)],
                "Public": [_live_message("Alice", "public and live only", NOW - 50)],
            },
            keys={"Secret": SECRET_KEY_BYTES},
        )
        live_feed._transport = fake.transport
        await MessageRepository.create(
            msg_type="CHAN",
            text="Zed: private and heard by both",
            received_at=NOW - 39,
            conversation_key=SECRET_KEY_HEX,
            sender_timestamp=NOW - 40,
            sender_name="Zed",
        )

        state = await live_feed.sync_once(await _enable(live_feed_channels=["*"]))

        assert state.last_error is None
        assert state.source == "packets"
        assert state.last_fetched == 2
        by_text = {m["text"]: m for m in (await live_feed.list_messages("1d"))["messages"]}
        secret = by_text["Zed: private and heard by both"]
        assert secret["source"] == "both"
        assert secret["channel_key"] == SECRET_KEY_HEX
        assert secret["channel_name"] == "Secret"
        assert by_text["Alice: public and live only"]["source"] == "live"
        stats = await live_feed.get_compare_stats("1d")
        assert stats is not None
        assert {c["channel_name"]: c["both"] for c in stats["channels"]} == {
            "Secret": 1,
            "Public": 0,
        }

    @pytest.mark.asyncio
    async def test_packets_for_channels_we_hold_no_key_for_are_skipped(self, test_db):
        fake = FakeCoreScope(
            {"Secret": [_live_message("Zed", "unreadable", NOW - 40)]},
            keys={"Secret": SECRET_KEY_BYTES},
        )
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable(live_feed_channels=["*"]))
        assert state.last_error is None
        assert state.last_fetched == 0
        assert await LiveFeedRepository.count() == 0

    @pytest.mark.asyncio
    async def test_envelope_only_packets_decrypt_too(self, test_db):
        """CoreScope may expose only decoded_json's channelHash/mac/encryptedData."""
        fake = FakeCoreScope({"Public": [_live_message("Alice", "via envelope", NOW - 20)]})
        fake.envelope_only = True
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable())
        assert state.last_error is None
        assert state.source == "packets"
        assert [m["text"] for m in (await live_feed.list_messages("1d"))["messages"]] == [
            "Alice: via envelope"
        ]

    @pytest.mark.asyncio
    async def test_falls_back_to_server_decryption_when_packets_carry_no_ciphertext(self, test_db):
        fake = FakeCoreScope({"Public": [_live_message("Alice", "server side", NOW - 20)]})
        fake.strip_ciphertext = True
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable())
        assert state.last_error is None
        assert state.source == "channel_messages"
        assert state.last_fetched == 1
        assert any(r.url.path == "/api/channels/Public/messages" for r in fake.requests)

    @pytest.mark.asyncio
    async def test_falls_back_when_the_packet_feed_is_missing(self, test_db):
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        fake = FakeCoreScope(
            {
                "Public": [_live_message("Alice", "server side", NOW - 20)],
                "Secret": [_live_message("Zed", "never reachable", NOW - 30)],
            },
            keys={"Secret": SECRET_KEY_BYTES},
        )
        fake.packets_enabled = False
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable(live_feed_channels=["*"]))
        assert state.last_error is None
        assert state.source == "channel_messages"
        assert state.last_warning is not None and "HTTP 404" in state.last_warning
        # Only channels the remote instance can name are asked for: Public and
        # hashtag channels, never the private one.
        asked = {r.url.path for r in fake.requests if r.url.path.startswith("/api/channels/")}
        assert "/api/channels/Public/messages" in asked
        assert not any("Secret" in path for path in asked)
        assert state.last_fetched == 1

    @pytest.mark.asyncio
    async def test_later_syncs_are_incremental_and_a_scope_change_walks_again(self, test_db):
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        live_feed._transport = fake.transport
        settings = await _enable()

        first = await live_feed.sync_once(settings)
        assert first.last_sync_full is True
        assert first.last_sync_started_at is not None
        packet_requests = [r for r in fake.requests if r.url.path == "/api/packets"]
        # The week is walked in slices, newest first, none wider than a slice.
        expected_slices = live_feed.LOOKBACK_SECONDS // live_feed.PACKET_SLICE_SECONDS
        assert len(packet_requests) == expected_slices
        bounds = [
            (
                live_feed._parse_iso(r.url.params["since"]),
                live_feed._parse_iso(r.url.params["until"]),
            )
            for r in packet_requests
        ]
        assert bounds[0][1] == first.last_sync_started_at
        assert bounds[-1][0] == first.last_sync_started_at - live_feed.LOOKBACK_SECONDS
        assert all(u - s == live_feed.PACKET_SLICE_SECONDS for s, u in bounds)

        fake.requests.clear()
        second = await live_feed.sync_once(settings)
        assert second.last_sync_full is False
        # Only what was observed since the previous sync started, minus the
        # overlap: a single narrow slice.
        assert len([r for r in fake.requests if r.url.path == "/api/packets"]) == 1
        second_since = live_feed._parse_iso(fake.requests[0].url.params["since"])
        assert second_since == first.last_sync_started_at - live_feed.CURSOR_OVERLAP_SECONDS
        # Nothing moved on the remote side, so nothing was rewritten.
        assert second.last_fetched == 1
        assert second.last_changed == 0

        # A different region is a different feed: back to a full walk once.
        fake.requests.clear()
        third = await live_feed.sync_once(await _enable(live_feed_region="YQB"))
        assert third.last_sync_full is True
        assert third.last_sync_started_at is not None
        oldest = min(
            live_feed._parse_iso(r.url.params["since"]) or 0
            for r in fake.requests
            if r.url.path == "/api/packets"
        )
        assert oldest == third.last_sync_started_at - live_feed.LOOKBACK_SECONDS

    @pytest.mark.asyncio
    async def test_upsert_rewrites_only_rows_whose_observations_moved(self, test_db):
        base = live_feed.normalize_live_message(
            _live_message("Alice", "hi", NOW - 20, repeats=1), "Public", PUBLIC_CHANNEL_KEY
        )
        assert base is not None
        assert await LiveFeedRepository.upsert_many([base]) == (1, 0)
        assert await LiveFeedRepository.upsert_many([dict(base)]) == (0, 0)
        moved = dict(base, repeats=3, observers=["a", "b", "c"])
        assert await LiveFeedRepository.upsert_many([moved]) == (0, 1)
        later = dict(base, last_seen=base["last_seen"] + 60)
        assert await LiveFeedRepository.upsert_many([later]) == (0, 1)
        message = (await live_feed.list_messages("1d"))["messages"][0]
        assert message["live_repeats"] == 3
        assert message["live_last_seen"] == base["last_seen"] + 60

    @pytest.mark.asyncio
    async def test_dropped_connections_are_retried_with_a_fresh_connection(
        self, test_db, monkeypatch
    ):
        monkeypatch.setattr(live_feed, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        fake.drop_packet_requests = 2
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable())
        assert state.last_error is None
        assert state.last_warning is None
        assert state.source == "packets"
        assert await LiveFeedRepository.count() == 1

    @pytest.mark.asyncio
    async def test_a_dead_packet_feed_falls_back_and_warns(self, test_db, monkeypatch):
        """Every request dropped: the sync still completes through the instance's
        own decryption of Public, and says so instead of failing outright."""
        monkeypatch.setattr(live_feed, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        monkeypatch.setattr(live_feed, "PACKET_SLICE_SECONDS", live_feed.LOOKBACK_SECONDS)
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        fake.drop_packet_requests = 10**6
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable())
        assert state.last_error is None
        assert state.source == "channel_messages"
        assert state.last_warning is not None
        assert "Server disconnected" in state.last_warning
        assert state.last_fetched == 1
        # Retries happened: more than one packet request before giving up.
        assert len([r for r in fake.requests if r.url.path == "/api/packets"]) == (
            live_feed.RETRY_ATTEMPTS
        )

    @pytest.mark.asyncio
    async def test_old_failing_slices_are_skipped_and_reported(self, test_db, monkeypatch):
        monkeypatch.setattr(live_feed, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
        fake = FakeCoreScope(
            {
                "Public": [
                    _live_message("Alice", "fresh", NOW - 60),
                    _live_message("Bob", "two days old", NOW - 2 * 86400),
                ]
            }
        )
        fake.drop_slices_older_than = NOW - 86400
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable())
        assert state.last_error is None
        assert state.source == "packets"
        assert state.last_warning is not None and "time slices" in state.last_warning
        # The recent slice landed; the dropped hours did not, and the sync still
        # advanced its cursor so the next poll is incremental.
        assert [m["text"] for m in (await live_feed.list_messages("1w"))["messages"]] == [
            "Alice: fresh"
        ]
        assert state.last_sync_full is True
        assert state.cursor == state.last_sync_started_at

    @pytest.mark.asyncio
    async def test_deselected_channels_drop_out_of_the_comparison(self, test_db):
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        fake = FakeCoreScope(
            {
                "Secret": [_live_message("Zed", "private", NOW - 40)],
                "Public": [_live_message("Alice", "public", NOW - 50)],
            },
            keys={"Secret": SECRET_KEY_BYTES},
        )
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable(live_feed_channels=["*"]))
        assert (await live_feed.list_messages("1d"))["total"] == 2

        # Compare Public only: Secret's mirrored rows stay but must not show.
        await AppSettingsRepository.update(live_feed_channels=["Public"])
        listed = await live_feed.list_messages("1d")
        assert [m["channel_name"] for m in listed["messages"]] == ["Public"]
        stats = await live_feed.get_compare_stats("1d")
        assert stats is not None
        assert [c["channel_name"] for c in stats["channels"]] == ["Public"]
        assert await LiveFeedRepository.count() == 2

    @pytest.mark.asyncio
    async def test_region_change_clears_the_mirror_but_channel_change_does_not(
        self, test_db, monkeypatch
    ):
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        live_feed._transport = fake.transport
        cleared: list[int] = []
        original_clear = LiveFeedRepository.clear

        async def counting_clear():
            cleared.append(1)
            await original_clear()

        monkeypatch.setattr(LiveFeedRepository, "clear", counting_clear)
        await live_feed.sync_once(await _enable(live_feed_region="YUL"))
        assert cleared == []
        await live_feed.sync_once(await _enable(live_feed_channels=["Public"]))
        assert cleared == []  # channel selection: full walk, rows kept
        state = await live_feed.sync_once(await _enable(live_feed_region="YQB"))
        assert cleared == [1]  # another region: the old rows are not its observations
        assert state.last_sync_full is True
        assert await LiveFeedRepository.count() == 1
        assert any("mirror cleared" in line for line in state.recent_log)

    @pytest.mark.asyncio
    async def test_status_carries_a_recent_activity_log(self, test_db):
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable())
        status = await live_feed.get_status()
        assert any("sync started (full" in line for line in status["recent_log"])
        assert any("sync done via packets" in line for line in status["recent_log"])

    @pytest.mark.asyncio
    async def test_several_regions_walk_the_packet_feed_once_each(self, test_db):
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable(live_feed_region="YUL,YQB"))
        regions = [
            r.url.params.get("region") for r in fake.requests if r.url.path == "/api/packets"
        ]
        assert list(dict.fromkeys(regions)) == ["YUL", "YQB"]
        assert await LiveFeedRepository.count() == 1

    @pytest.mark.asyncio
    async def test_unknown_entries_are_reported_until_the_channel_exists(self, test_db):
        fake = FakeCoreScope(
            {"Secret": [_live_message("A", "psst", NOW - 5)]}, keys={"Secret": SECRET_KEY_BYTES}
        )
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable(live_feed_channels=["Secret"]))

        status = await live_feed.get_status()
        assert status["unresolved_channels"] == ["Secret"]
        assert status["channels"] == []
        assert await LiveFeedRepository.count() == 0

        # Joining the channel locally gives the name a key, so its packets decrypt.
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        state = await live_feed.sync_once(await AppSettingsRepository.get())
        assert state.unresolved_channels == []
        stats = await live_feed.get_compare_stats("1d")
        assert stats is not None
        assert stats["channels"][0]["channel_key"] == SECRET_KEY_HEX
        assert stats["live_only"] == 1

    @pytest.mark.asyncio
    async def test_http_failure_is_recorded_not_raised(self, test_db):
        fake = FakeCoreScope({})
        fake.fail_with = 503
        live_feed._transport = fake.transport
        state = await live_feed.sync_once(await _enable())
        assert state.last_error is not None
        assert "HTTP 503" in state.last_error
        assert state.last_success_at is None
        assert state.syncing is False

    @pytest.mark.asyncio
    async def test_disabled_feature_does_not_sync_unless_forced(self, test_db):
        fake = FakeCoreScope({"Public": [_live_message("A", "x", NOW - 30)]})
        live_feed._transport = fake.transport
        await AppSettingsRepository.update(live_feed_url="https://live.example.test")
        await live_feed.sync_once()
        assert fake.requests == []
        assert await live_feed.get_compare_stats("1d") is None

        await live_feed.sync_once(force=True)
        assert len(fake.requests) >= 1
        assert await LiveFeedRepository.count() == 1
        assert await live_feed.get_compare_stats("1d") is not None


# ─── HTTP surface ──────────────────────────────────────────────────────────


class TestLiveFeedEndpoints:
    @pytest.mark.asyncio
    async def test_status_reflects_defaults(self, test_db, client):
        response = await client.get("/api/live-feed/status")
        assert response.status_code == 200
        payload = response.json()
        assert payload["enabled"] is False
        assert payload["url"] == "https://live.meshcore.ca"
        # '*' resolved against a fresh node: its seeded channels plus Public.
        assert "Public" in payload["channels"]
        assert payload["source"] == "packets"
        assert payload["poll_interval"] == 900
        assert payload["mirrored_messages"] == 0

    @pytest.mark.asyncio
    async def test_settings_patch_normalizes_and_wakes_the_loop(self, test_db, client, monkeypatch):
        woken = MagicMock()
        monkeypatch.setattr(live_feed, "notify_settings_changed", woken)
        response = await client.patch(
            "/api/settings",
            json={
                "live_feed_enabled": True,
                "live_feed_url": "https://live.example.test/",
                "live_feed_region": " yul ,yqb",
                "live_feed_channels": ["Public", " #bot", "public", SECRET_KEY_HEX.lower()],
                "live_feed_poll_interval": 5,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["live_feed_enabled"] is True
        assert payload["live_feed_url"] == "https://live.example.test"
        assert payload["live_feed_region"] == "YUL,YQB"
        assert payload["live_feed_channels"] == ["Public", "#bot", SECRET_KEY_HEX.lower()]
        assert payload["live_feed_poll_interval"] == MIN_LIVE_FEED_POLL_INTERVAL
        woken.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_wake_during_a_sync_is_not_lost(self, test_db):
        """The loop clears its wake flag before reading settings, so a settings
        change that lands mid-sync triggers another pass instead of waiting a
        full poll interval."""
        fake = FakeCoreScope({"Public": [_live_message("Alice", "hi", NOW - 20)]})
        live_feed._transport = fake.transport
        await _enable()
        reads: list[float] = []
        original_get = AppSettingsRepository.get

        async def counting_get():
            reads.append(time.monotonic())
            settings = await original_get()
            if len(reads) == 1:
                live_feed.notify_settings_changed()  # arrives while this pass runs
            return settings

        with patch.object(AppSettingsRepository, "get", counting_get):
            await live_feed.start_live_feed()
            for _ in range(50):
                await asyncio.sleep(0.01)
                if len(reads) >= 2:
                    break
            await live_feed.stop_live_feed()
        # A second pass ran right away rather than after the 900 s interval.
        assert len(reads) >= 2

    @pytest.mark.asyncio
    async def test_settings_patch_rejects_non_http_url(self, test_db, client):
        response = await client.patch("/api/settings", json={"live_feed_url": "ftp://x"})
        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_sync_messages_stats_and_regions(self, test_db, client):
        fake = FakeCoreScope(
            {"Public": [_live_message("Alice", "hello", NOW - 30)]},
            regions={"YUL": "Montréal, CA", "YQB": "Québec, CA"},
        )
        live_feed._transport = fake.transport
        await _enable()
        await _local_channel_message("Alice: hello", NOW - 30)

        synced = await client.post("/api/live-feed/sync")
        assert synced.status_code == 200
        assert synced.json()["last_fetched"] == 1
        assert synced.json()["last_error"] is None

        messages = await client.get("/api/live-feed/messages", params={"window": "1d"})
        assert messages.status_code == 200
        body = messages.json()
        assert body["total"] == 1
        assert body["messages"][0]["source"] == "both"
        assert body["counts"] == {"both": 1, "node_only": 0, "live_only": 0}

        stats = await client.get("/api/live-feed/stats", params={"window": "1d"})
        assert stats.status_code == 200
        assert stats.json()["both"] == 1
        assert stats.json()["node_coverage_pct"] == 100.0

        regions = await client.get("/api/live-feed/regions")
        assert regions.status_code == 200
        assert regions.json()["regions"] == [
            {"code": "YUL", "label": "Montréal, CA"},
            {"code": "YQB", "label": "Québec, CA"},
        ]

        bad = await client.get("/api/live-feed/messages", params={"window": "1decade"})
        assert bad.status_code == 422
        bad_source = await client.get("/api/live-feed/messages", params={"source": "mars"})
        assert bad_source.status_code == 422

    @pytest.mark.asyncio
    async def test_probe_reports_each_user_agent_separately(self, test_db, client):
        fake = FakeCoreScope({})
        fake.drop_user_agents_without = "RemoteTerm-LiveCompare/"
        live_feed._transport = fake.transport
        await _enable()

        response = await client.post("/api/live-feed/probe")

        assert response.status_code == 200
        payload = response.json()
        assert payload["url"] == "https://live.example.test"
        by_label = {a["label"]: a for a in payload["attempts"]}
        ours = by_label["RemoteTerm (what the sync uses)"]
        assert ours["ok"] is True and ours["status"] == 200
        assert "RemoteTerm-LiveCompare/" in ours["user_agent"]
        python = by_label["python-httpx default (what older builds sent)"]
        assert python["ok"] is False
        assert "Server disconnected" in python["error"]

    @pytest.mark.asyncio
    async def test_regions_failure_is_a_502(self, test_db, client):
        fake = FakeCoreScope({})
        fake.fail_with = 500
        live_feed._transport = fake.transport
        response = await client.get("/api/live-feed/regions")
        assert response.status_code == 502

    @pytest.mark.asyncio
    async def test_statistics_carries_the_comparison_once_enabled(self, test_db, client):
        before = await client.get("/api/statistics")
        assert before.status_code == 200
        assert before.json()["live_compare"] is None

        fake = FakeCoreScope({"Public": [_live_message("Alice", "hello", NOW - 30)]})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable())

        after = await client.get("/api/statistics", params={"window": "1d"})
        assert after.status_code == 200
        section = after.json()["live_compare"]
        assert section["live_only"] == 1
        assert section["status"]["enabled"] is True
        assert section["channels"][0]["channel_name"] == "Public"


class TestMigration088:
    @pytest.mark.asyncio
    async def test_fresh_schema_has_table_and_settings_columns(self, test_db):
        async with test_db.readonly() as conn:
            async with conn.execute("PRAGMA table_info(live_feed_messages)") as cursor:
                columns = {row["name"] for row in await cursor.fetchall()}
            async with conn.execute("PRAGMA table_info(app_settings)") as cursor:
                settings_columns = {row["name"] for row in await cursor.fetchall()}
            async with conn.execute("SELECT live_feed_channels FROM app_settings") as cursor:
                row = await cursor.fetchone()
        assert {"packet_hash", "channel_key", "text", "sender_timestamp", "first_seen"} <= columns
        assert {
            "live_feed_enabled",
            "live_feed_url",
            "live_feed_region",
            "live_feed_channels",
            "live_feed_poll_interval",
        } <= settings_columns
        assert json.loads(row["live_feed_channels"]) == ["*"]
