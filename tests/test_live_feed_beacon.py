"""Live feed comparison against a MeshCore Beacon instance (``/api/v1``)."""

import hashlib
import time
from urllib.parse import unquote

import httpx
import pytest

from app.channel_constants import PUBLIC_CHANNEL_KEY, hashtag_channel_key
from app.repository import AppSettingsRepository, ChannelRepository, MessageRepository
from app.repository.live_feed import LiveFeedRepository
from app.services import live_feed, live_feed_trace
from tests.test_live_feed import encrypt_group_text

NOW = int(time.time())
SECRET_KEY_HEX = "CD" * 16
BOT_KEY_HEX = hashtag_channel_key("#bot")


def _hash_byte(key_hex: str) -> int:
    return hashlib.sha256(bytes.fromhex(key_hex)).digest()[0]


def _message(msg_id: int, sender: str, content: str, ts: int, *, count: int = 3) -> dict:
    return {
        "id": msg_id,
        "packetHash": f"beef{msg_id:012x}",
        "channelHash": "00",
        "senderName": sender,
        "content": content,
        "sentAt": ts * 1000,
        "observationCount": count,
        "scope": None,
        "scopeStatus": "unscoped",
    }


class FakeBeacon:
    """An httpx mock transport serving the Beacon endpoints the live feed reads.

    ``channels`` are Beacon channel records (``id``, ``name``, ``keyKnown``,
    ``channelHash``...), ``messages`` maps a channel id to its decrypted
    messages (newest first) and ``packets`` are encrypted GRP_TXT packets
    (``packetHash``, ``key``, ``sender``, ``text``, ``ts``, ``count``).
    """

    def __init__(self, channels, messages=None, packets=None, iatas=None, page_size=200):
        self.channels = channels
        self.messages = messages or {}
        self.packets = packets or []
        self.iatas = iatas or []
        self.page_size = page_size
        self.requests: list[httpx.Request] = []

    def paths(self, prefix: str) -> list[str]:
        return [r.url.path for r in self.requests if r.url.path.startswith(prefix)]

    def _detail(self, packet: dict) -> dict:
        raw = encrypt_group_text(
            bytes.fromhex(packet["key"]), packet["ts"], packet["sender"], packet["text"]
        )
        return {
            "packetHash": packet["packetHash"],
            "rawPayload": raw[2:].hex(),
            "decrypted": False,
            "firstHeardAt": (packet["ts"] + 1) * 1000,
            "lastHeardAt": (packet["ts"] + 5) * 1000,
            "observationCount": packet.get("count", 2),
            "observations": [
                {
                    "id": 1,
                    "observerId": "6f1c2d3e-0000-0000-0000-000000000001",
                    "observerName": "Observer A",
                    "iata": "YUL",
                    "heardAt": (packet["ts"] + 1) * 1000,
                    "pathLength": {"raw": "42", "hashSize": 2, "hopCount": 2},
                    "pathBytes": "aabbccdd",
                    "snr": 6.5,
                    "rssi": -98,
                    "resolvedPath": [
                        {
                            "confidence": "high",
                            "nodes": [
                                {
                                    "publicKey": "aabb" + "00" * 30,
                                    "name": "Relay One",
                                    "latitude": 45.5,
                                    "longitude": -73.6,
                                }
                            ],
                        },
                        {"confidence": "none", "nodes": []},
                    ],
                }
            ],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.rstrip("/")
        params = request.url.params
        if path == "/api/v1/channels":
            items = self.channels
            if params.get("hash"):
                items = [c for c in items if c["channelHash"] == params["hash"]]
            start = int(params["pageCursor"].split(":")[2]) if params.get("pageCursor") else 0
            page = items[start : start + self.page_size]
            more = start + self.page_size < len(items)
            summaries = [
                {k: v for k, v in c.items() if k not in ("hashtag", "keyFingerprint")} for c in page
            ]
            body = {"items": summaries, "hasMore": more}
            if more:
                body["nextPageCursor"] = f"v1:0:{start + self.page_size}"
            return httpx.Response(200, json=body)
        if path.startswith("/api/v1/channels/") and path.endswith("/messages"):
            channel_id = int(path.split("/")[4])
            since_ms = int(params.get("since", "0"))
            cursor = int(params.get("cursor", "0"))
            items = [
                m
                for m in self.messages.get(channel_id, [])
                if m["sentAt"] >= since_ms and (cursor == 0 or m["id"] < cursor)
            ]
            page = items[: self.page_size]
            more = len(items) > self.page_size
            body: dict = {"items": page, "hasMore": more}
            if more:
                body["nextCursor"] = page[-1]["id"]
            return httpx.Response(200, json=body)
        if path.startswith("/api/v1/channels/"):
            channel_id = int(path.split("/")[4])
            for channel in self.channels:
                if channel["id"] == channel_id:
                    return httpx.Response(200, json={**channel, "messageCount": 0})
            return httpx.Response(404, json={"error": "channel not found"})
        if path == "/api/v1/packets":
            assert params.get("payloadType") == "5"
            since, until = int(params["since"]) // 1000, int(params["until"]) // 1000
            cursor = int(params.get("cursor", "0"))
            items = [
                {
                    "packetHash": p["packetHash"],
                    "payloadType": 5,
                    "firstHeardAt": (p["ts"] + 1) * 1000,
                    "lastHeardAt": (p["ts"] + 5) * 1000,
                    "observationCount": p.get("count", 2),
                }
                for p in sorted(self.packets, key=lambda p: p["ts"], reverse=True)
                if since <= p["ts"] + 1 <= until and (cursor == 0 or (p["ts"] + 5) * 1000 < cursor)
            ]
            page = items[: self.page_size]
            more = len(items) > self.page_size
            body = {"items": page, "hasMore": more}
            if more:
                body["nextCursor"] = page[-1]["lastHeardAt"]
            return httpx.Response(200, json=body)
        if path.startswith("/api/v1/packets/"):
            packet_hash = unquote(path.split("/")[4])
            for packet in self.packets:
                if packet["packetHash"] == packet_hash:
                    return httpx.Response(200, json=self._detail(packet))
            return httpx.Response(404, json={"error": "packet not found"})
        if path == "/api/v1/iatas":
            return httpx.Response(200, json=self.iatas)
        return httpx.Response(404, json={"error": "not found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


PUBLIC_CHANNEL = {
    "id": 249,
    "name": "Public",
    "channelHash": f"{_hash_byte(PUBLIC_CHANNEL_KEY):02x}",
    "lastSeen": NOW * 1000,
    "isHashtag": False,
    "keyKnown": True,
}


@pytest.fixture(autouse=True)
def _reset_live_feed(monkeypatch):
    live_feed._reset_for_tests()
    live_feed_trace._reset_for_tests()
    monkeypatch.setattr(live_feed, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    yield
    live_feed._reset_for_tests()
    live_feed_trace._reset_for_tests()


async def _enable(**overrides):
    fields = {
        "live_feed_enabled": True,
        "live_feed_url": "https://live.example.test",
        "live_feed_channels": ["Public"],
    }
    fields.update(overrides)
    return await AppSettingsRepository.update(**fields)


def test_instance_base_url_tolerates_a_pasted_api_url():
    assert live_feed.instance_base_url("https://live.meshcore.ca/api/v1/") == (
        "https://live.meshcore.ca"
    )
    assert live_feed.instance_base_url("https://x.test/api") == "https://x.test"
    assert live_feed.instance_base_url("https://x.test/") == "https://x.test"


class TestBeaconSync:
    @pytest.mark.asyncio
    async def test_public_is_read_from_beacons_own_decryption(self, test_db):
        fake = FakeBeacon(
            [PUBLIC_CHANNEL],
            messages={
                249: [
                    _message(2, "Alice", "seen by both", NOW - 100),
                    _message(1, "Bob", "only the mesh heard this", NOW - 200),
                ]
            },
        )
        live_feed._transport = fake.transport
        await MessageRepository.create(
            msg_type="CHAN",
            text="Alice: seen by both",
            received_at=NOW - 99,
            conversation_key=PUBLIC_CHANNEL_KEY,
            sender_timestamp=NOW - 100,
            sender_name="Alice",
        )

        state = await live_feed.sync_once(await _enable(live_feed_region=" yul "))

        assert state.last_error is None
        assert state.source == "beacon"
        assert state.last_fetched == 2
        assert state.last_warning is None
        row = await LiveFeedRepository.get_by_hash(f"beef{2:012x}")
        assert row is not None
        assert row["text"] == "Alice: seen by both"
        assert row["channel_key"] == PUBLIC_CHANNEL_KEY
        assert row["sender_timestamp"] == NOW - 100
        assert row["repeats"] == 3
        message_requests = [
            r for r in fake.requests if r.url.path == "/api/v1/channels/249/messages"
        ]
        assert message_requests and message_requests[0].url.params["iatas"] == "YUL"
        # Everything Public needs comes from Beacon's decryption: no packet walk.
        assert fake.paths("/api/v1/packets") == []
        assert fake.paths("/api/packets") == []

        stats = await live_feed.get_compare_stats("24h")
        assert stats is not None

    @pytest.mark.asyncio
    async def test_hash_collisions_are_paged_and_matched_by_name(self, test_db):
        decoy = {**PUBLIC_CHANNEL, "id": 7, "name": "Somebody's club"}
        unkeyed = {**PUBLIC_CHANNEL, "id": 8, "name": None, "keyKnown": False}
        fake = FakeBeacon(
            [decoy, unkeyed, PUBLIC_CHANNEL],
            messages={
                7: [_message(9, "Mallory", "wrong channel", NOW - 50)],
                249: [_message(3, "Alice", "right channel", NOW - 60)],
            },
            page_size=1,
        )
        live_feed._transport = fake.transport

        state = await live_feed.sync_once(await _enable())

        assert state.last_error is None
        assert await LiveFeedRepository.count() == 1
        assert await LiveFeedRepository.get_by_hash(f"beef{3:012x}") is not None
        assert any(r.url.params.get("pageCursor") for r in fake.requests)
        assert fake.paths("/api/v1/channels/7/messages") == []

    @pytest.mark.asyncio
    async def test_hashtag_channel_is_confirmed_by_fingerprint(self, test_db):
        fingerprint = hashlib.sha256(bytes.fromhex(BOT_KEY_HEX)).digest()[:8].hex()
        bot = {
            "id": 40,
            "name": None,
            "channelHash": f"{_hash_byte(BOT_KEY_HEX):02x}",
            "lastSeen": NOW * 1000,
            "isHashtag": True,
            "keyKnown": True,
            "hashtag": "bot",
            "keyFingerprint": fingerprint,
        }
        fake = FakeBeacon([bot], messages={40: [_message(4, "Dan", "beep", NOW - 30)]})
        live_feed._transport = fake.transport

        state = await live_feed.sync_once(await _enable(live_feed_channels=["#bot"]))

        assert state.last_error is None
        row = await LiveFeedRepository.get_by_hash(f"beef{4:012x}")
        assert row is not None and row["channel_key"] == BOT_KEY_HEX
        assert "/api/v1/channels/40" in fake.paths("/api/v1/channels/40")

    @pytest.mark.asyncio
    async def test_private_channel_is_decrypted_from_packet_details(self, test_db):
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        packets = [
            {
                "packetHash": "aa01",
                "key": SECRET_KEY_HEX,
                "sender": "Eve",
                "text": "private hello",
                "ts": NOW - 400,
                "count": 2,
            },
            {
                "packetHash": "aa02",
                "key": "EF" * 16,
                "sender": "Zed",
                "text": "a key we do not hold",
                "ts": NOW - 300,
            },
        ]
        fake = FakeBeacon([PUBLIC_CHANNEL], packets=packets)
        live_feed._transport = fake.transport
        settings = await _enable(live_feed_channels=["Secret"])

        state = await live_feed.sync_once(settings)

        assert state.last_error is None
        assert state.last_warning is None
        row = await LiveFeedRepository.get_by_hash("aa01")
        assert row is not None
        assert row["text"] == "Eve: private hello"
        assert row["channel_key"] == SECRET_KEY_HEX
        assert row["sender_timestamp"] == NOW - 400
        assert row["hops"] == 2
        assert row["observers"] == ["Observer A"]
        assert await LiveFeedRepository.get_by_hash("aa02") is None
        assert sorted(fake.paths("/api/v1/packets/")) == [
            "/api/v1/packets/aa01",
            "/api/v1/packets/aa02",
        ]

        # The next sync re-reads summaries only: no detail is fetched again,
        # and a grown observation count still lands.
        packets[0]["count"] = 9
        fake.requests.clear()
        state = await live_feed.sync_once(settings, force=True)
        assert state.last_error is None
        assert fake.paths("/api/v1/packets/") == []
        row = await LiveFeedRepository.get_by_hash("aa01")
        assert row is not None and row["repeats"] == 9

    @pytest.mark.asyncio
    async def test_detail_budget_carries_the_backlog_to_the_next_sync(self, test_db, monkeypatch):
        monkeypatch.setattr(live_feed, "MAX_DETAIL_FETCHES_PER_SYNC", 2)
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        packets = [
            {
                "packetHash": f"bb{i:02x}",
                "key": SECRET_KEY_HEX,
                "sender": "Eve",
                "text": f"note {i}",
                "ts": NOW - 1000 - i * 10,
            }
            for i in range(5)
        ]
        fake = FakeBeacon([], packets=packets)
        live_feed._transport = fake.transport
        settings = await _enable(live_feed_channels=["Secret"])

        state = await live_feed.sync_once(settings)
        assert state.last_warning is not None and "Still inspecting" in state.last_warning
        assert await LiveFeedRepository.count() == 2

        await live_feed.sync_once(settings, force=True)
        state = await live_feed.sync_once(settings, force=True)
        assert await LiveFeedRepository.count() == 5
        assert state.last_warning is None

    @pytest.mark.asyncio
    async def test_regions_come_from_the_iata_list(self, test_db):
        fake = FakeBeacon(
            [PUBLIC_CHANNEL],
            iatas=[
                {"iata": "YUL", "displayName": "Montreal", "lat": 45.5, "lon": -73.6},
                {"iata": "yqb", "displayName": None, "lat": None, "lon": None},
            ],
        )
        live_feed._transport = fake.transport
        await _enable(live_feed_url="https://live.example.test/api/v1")

        result = await live_feed.get_regions()

        assert result["url"] == "https://live.example.test"
        assert result["regions"] == [
            {"code": "YUL", "label": "Montreal (YUL)"},
            {"code": "YQB", "label": "YQB"},
        ]


class TestBeaconTrace:
    @pytest.mark.asyncio
    async def test_trace_uses_beacons_resolved_paths(self, test_db):
        await ChannelRepository.upsert(SECRET_KEY_HEX, "Secret")
        packet = {
            "packetHash": "cc01",
            "key": SECRET_KEY_HEX,
            "sender": "Eve",
            "text": "trace me",
            "ts": NOW - 500,
        }
        fake = FakeBeacon([], packets=[packet])
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable(live_feed_channels=["Secret"]))

        trace = await live_feed_trace.get_trace(packet_hash="cc01")

        assert trace is not None
        assert trace["live_error"] is None
        assert trace["heard_by_node"] is False
        assert trace["live_url"] is None
        [route] = trace["routes"]
        assert route["kind"] == "observer"
        assert route["receiver"]["name"] == "Observer A"
        assert route["region"] == "YUL"
        assert route["heard_at"] == NOW - 499
        assert [hop["prefix"] for hop in route["hops"]] == ["AABB", "CCDD"]
        first, second = route["hops"]
        assert first["node"]["name"] == "Relay One"
        assert first["node"]["lat"] == 45.5
        assert first["identified_by"] == "live"
        assert second["node"] is None
        # Beacon's detail already resolves hops: no CoreScope hop lookup.
        assert not [r for r in fake.requests if "resolve-hops" in r.url.path]
