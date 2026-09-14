"""Live feed comparison: mirroring live.meshcore.ca (CoreScope) and comparing with our node."""

import asyncio
import json
import time
from datetime import UTC, datetime

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


class FakeCoreScope:
    """An httpx mock transport that serves CoreScope's channel endpoints."""

    def __init__(self, channels: dict[str, list[dict]], regions: dict[str, str] | None = None):
        self.channels = channels
        self.regions = regions if regions is not None else {"YUL": "Montréal, CA"}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, text="nope")
        path = request.url.path
        if path == "/api/config/regions":
            return httpx.Response(200, json=self.regions)
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
def _reset_live_feed():
    live_feed._reset_for_tests()
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


class TestResolveChannelKeys:
    @pytest.mark.asyncio
    async def test_public_and_hashtag_channels_resolve_without_local_rows(self, test_db):
        mapping = await live_feed.resolve_channel_keys(["Public", "#bot", "Secret"])
        assert mapping["Public"] == PUBLIC_CHANNEL_KEY
        assert mapping["#bot"] == hashtag_channel_key("#bot")
        assert mapping["Secret"] is None

    @pytest.mark.asyncio
    async def test_other_names_resolve_against_local_channels(self, test_db):
        await ChannelRepository.upsert("AB" * 16, "Secret")
        mapping = await live_feed.resolve_channel_keys(["secret"])
        assert mapping["secret"] == "AB" * 16


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
        assert state.unresolved_channels == []
        assert await LiveFeedRepository.count() == 2
        # The region filter reaches the remote request, upper-cased and trimmed.
        assert fake.requests[0].url.params["region"] == "YUL"
        assert fake.requests[0].url.path == "/api/channels/Public/messages"

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
        assert both["live_observers"] == ["obs-a", "obs-b"]
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
        assert message["live_observers"] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_pagination_stops_at_the_lookback_horizon(self, test_db, monkeypatch):
        monkeypatch.setattr(live_feed, "PAGE_LIMIT", 2)
        recent = [_live_message("A", f"m{i}", NOW - i) for i in range(4)]
        ancient = [
            _live_message("Z", f"z{i}", NOW - live_feed.LOOKBACK_SECONDS - 86400 * (i + 1))
            for i in range(4)
        ]
        fake = FakeCoreScope({"Public": recent + ancient})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable())

        # 2 pages of recent rows, then the first ancient page ends the walk.
        assert len(fake.requests) == 3
        assert [r.url.params["offset"] for r in fake.requests] == ["0", "2", "4"]
        assert await LiveFeedRepository.count() == 6

    @pytest.mark.asyncio
    async def test_unresolved_channels_are_reported_and_stay_live_only(self, test_db):
        fake = FakeCoreScope({"Secret": [_live_message("A", "psst", NOW - 5)]})
        live_feed._transport = fake.transport
        await live_feed.sync_once(await _enable(live_feed_channels=["Secret"]))

        status = await live_feed.get_status()
        assert status["unresolved_channels"] == ["Secret"]
        messages = (await live_feed.list_messages("1d"))["messages"]
        assert [m["source"] for m in messages] == ["live"]
        assert messages[0]["channel_name"] == "Secret"

        # Joining the channel locally makes the mirrored rows comparable.
        await ChannelRepository.upsert("CD" * 16, "Secret")
        await live_feed.sync_once(await AppSettingsRepository.get())
        stats = await live_feed.get_compare_stats("1d")
        assert stats is not None
        assert stats["channels"][0]["channel_key"] == "CD" * 16

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
        fake = FakeCoreScope({"Public": [_live_message("A", "x", NOW)]})
        live_feed._transport = fake.transport
        await AppSettingsRepository.update(live_feed_url="https://live.example.test")
        await live_feed.sync_once()
        assert fake.requests == []
        assert await live_feed.get_compare_stats("1d") is None

        await live_feed.sync_once(force=True)
        assert len(fake.requests) == 1
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
        assert payload["channels"] == ["Public"]
        assert payload["poll_interval"] == 300
        assert payload["mirrored_messages"] == 0

    @pytest.mark.asyncio
    async def test_settings_patch_normalizes_and_wakes_the_loop(self, test_db, client):
        fake = FakeCoreScope({})
        live_feed._transport = fake.transport
        await live_feed.start_live_feed()
        await asyncio.sleep(0)  # let the loop create its wake event
        response = await client.patch(
            "/api/settings",
            json={
                "live_feed_enabled": True,
                "live_feed_url": "https://live.example.test/",
                "live_feed_region": " yul ,yqb",
                "live_feed_channels": ["Public", " #bot", "public"],
                "live_feed_poll_interval": 5,
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["live_feed_enabled"] is True
        assert payload["live_feed_url"] == "https://live.example.test"
        assert payload["live_feed_region"] == "YUL,YQB"
        assert payload["live_feed_channels"] == ["Public", "#bot"]
        assert payload["live_feed_poll_interval"] == MIN_LIVE_FEED_POLL_INTERVAL
        assert live_feed._wake_requested()
        await live_feed.stop_live_feed()

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
        assert json.loads(row["live_feed_channels"]) == ["Public"]
