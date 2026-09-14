"""Live Compare trace: where one channel message travelled, per observer and per this node."""

import json
import time
from datetime import UTC, datetime

import httpx
import pytest

from app.channel_constants import PUBLIC_CHANNEL_KEY
from app.repository import AppSettingsRepository, ContactRepository, MessageRepository
from app.repository.live_feed import LiveFeedRepository
from app.services import live_feed, live_feed_trace

NOW = int(time.time()) - 60
HASH = "a1b2c3d4e5f60718"

ALPHA = "aa" + "11" * 31  # a repeater this node knows, one hop away
BRAVO = "bb" + "22" * 31  # known, farther
CHARLIE_1 = "cc" + "01" * 31  # two known relays share the CC prefix
CHARLIE_2 = "cc" + "02" * 31
DELTA = "dd" + "77" * 31  # unknown here; the instance names it
OBS_1 = "0b" + "51" * 31
OBS_2 = "0b" + "52" * 31
SELF = {
    "public_key": "5e" + "1f" * 31,
    "name": "My radio",
    "lat": 45.50,
    "lon": -73.60,
    "known_locally": True,
    "direct_neighbour": False,
}


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat().replace("+00:00", "Z")


class FakeInstance:
    """CoreScope's packet-detail, observer and hop-resolution endpoints."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.detail_status = 200
        self.observers_status = 200
        self.resolve_status = 200
        self.observations: list[dict] = [
            {
                "id": 1,
                "observer_id": OBS_1,
                "observer_name": "Obs One",
                "observer_iata": "YUL",
                "snr": 8.5,
                "rssi": -101,
                "path_json": json.dumps(["AA", "DD"]),
                "resolved_path": json.dumps([None, DELTA]),
                "timestamp": _iso(NOW + 2),
            },
            {
                "id": 2,
                "observer_id": OBS_2,
                "observer_name": "Obs Two",
                "observer_iata": "YUL",
                "snr": -3.25,
                "rssi": -118,
                "path_json": json.dumps(["CC"]),
                "timestamp": _iso(NOW + 1),
            },
        ]
        self.observers = [
            {"id": OBS_1, "name": "Obs One", "iata": "YUL", "lat": 45.52, "lon": -73.58},
            {"id": OBS_2, "name": "Obs Two", "iata": "YUL", "lat": 45.70, "lon": -73.90},
        ]
        self.resolutions: dict[str, dict] = {
            "DD": {
                "name": "Delta",
                "pubkey": DELTA,
                "ambiguous": False,
                "candidates": [{"name": "Delta", "pubkey": DELTA, "lat": 45.9, "lon": -74.1}],
                "confidence": "unique_prefix",
            },
            "CC": {
                "name": "Charlie Two",
                "pubkey": CHARLIE_2,
                "ambiguous": True,
                "candidates": [
                    {"name": "Charlie Two", "pubkey": CHARLIE_2, "lat": 45.61, "lon": -73.71},
                    {"name": "Charlie One", "pubkey": CHARLIE_1, "lat": 46.80, "lon": -71.20},
                ],
                "confidence": "geo_proximity",
            },
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == f"/api/packets/{HASH}":
            if self.detail_status != 200:
                return httpx.Response(self.detail_status, json={"error": "Not found"})
            return httpx.Response(
                200,
                json={
                    "packet": {"hash": HASH, "payload_type": 5, "first_seen": _iso(NOW + 1)},
                    "path": ["AA", "DD"],
                    "observation_count": len(self.observations),
                    "observations": self.observations,
                },
            )
        if path == "/api/observers":
            if self.observers_status != 200:
                return httpx.Response(self.observers_status, text="nope")
            return httpx.Response(200, json={"observers": self.observers})
        if path == "/api/resolve-hops":
            if self.resolve_status != 200:
                return httpx.Response(self.resolve_status, text="nope")
            hops = request.url.params.get("hops", "").split(",")
            return httpx.Response(
                200,
                json={"resolved": {h: self.resolutions[h] for h in hops if h in self.resolutions}},
            )
        return httpx.Response(404, json={"error": "not found"})

    def calls(self, path_prefix: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.startswith(path_prefix)]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    live_feed._reset_for_tests()
    live_feed_trace._reset_for_tests()
    monkeypatch.setattr(live_feed, "RETRY_BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.setattr(live_feed_trace, "_self_node", lambda: dict(SELF))
    yield
    live_feed._reset_for_tests()
    live_feed_trace._reset_for_tests()


@pytest.fixture
async def instance(test_db):
    fake = FakeInstance()
    live_feed._transport = fake.transport
    await AppSettingsRepository.update(
        live_feed_enabled=True, live_feed_url="https://live.example.test"
    )
    return fake


async def _relay(public_key: str, name: str, lat: float, lon: float, *, direct: bool = False):
    await ContactRepository.upsert(
        {
            "public_key": public_key,
            "name": name,
            "type": 2,
            "lat": lat,
            "lon": lon,
            "direct_path": "" if direct else None,
            "direct_path_len": 0 if direct else None,
        }
    )


async def _known_relays() -> None:
    await _relay(ALPHA, "Alpha", 45.51, -73.61, direct=True)
    await _relay(BRAVO, "Bravo", 45.60, -73.70)
    await _relay(CHARLIE_1, "Charlie One", 46.80, -71.20)
    await _relay(CHARLIE_2, "Charlie Two", 45.61, -73.71)


async def _mirror(text: str = "Alice: hello mesh", **overrides) -> None:
    row = {
        "packet_hash": HASH,
        "channel_name": "Public",
        "channel_key": PUBLIC_CHANNEL_KEY,
        "sender": "Alice",
        "text": text,
        "sender_timestamp": NOW,
        "first_seen": NOW + 1,
        "last_seen": NOW + 2,
        "repeats": 2,
        "observers": ["Obs One", "Obs Two"],
        "hops": 2,
        "snr": 8.5,
        "scope_name": None,
    }
    row.update(overrides)
    await LiveFeedRepository.upsert_many([row])


async def _local(text: str = "Alice: hello mesh", *, path: str | None = "AA", **kw) -> int:
    message_id = await MessageRepository.create(
        msg_type="CHAN",
        text=text,
        received_at=NOW + 4,
        conversation_key=PUBLIC_CHANNEL_KEY,
        sender_timestamp=NOW,
        sender_name="Alice",
        path=path,
        path_len=len(path) // 2 if path else 0,
        rssi=-95,
        snr=6.0,
        **kw,
    )
    assert message_id is not None
    return message_id


def _hops(route: dict) -> list[tuple[str, str | None]]:
    return [(h["prefix"], (h["node"] or {}).get("name")) for h in route["hops"]]


class TestLiveOnlyTrace:
    @pytest.mark.asyncio
    async def test_every_observation_becomes_a_route_with_named_hops(self, instance):
        await _known_relays()
        await _mirror()

        trace = await live_feed_trace.get_trace(packet_hash=HASH)

        assert trace is not None
        assert trace["heard_by_node"] is False
        assert trace["message_id"] is None
        assert trace["live_url"] == f"https://live.example.test/#/packets/{HASH}"
        assert trace["live_repeats"] == 2
        assert trace["self_node"] == SELF
        assert trace["live_error"] is None and trace["live_warning"] is None

        routes = trace["routes"]
        assert [r["kind"] for r in routes] == ["observer", "observer"]
        # Ordered by when each observer heard it.
        assert [r["receiver"]["name"] for r in routes] == ["Obs Two", "Obs One"]
        two, one = routes

        assert one["region"] == "YUL"
        assert one["heard_at"] == NOW + 2
        assert one["snr"] == 8.5 and one["rssi"] == -101
        # Observer coordinates come from the instance's observer list.
        assert (one["receiver"]["lat"], one["receiver"]["lon"]) == (45.52, -73.58)
        assert one["receiver"]["public_key"] == OBS_1
        assert _hops(one) == [("AA", "Alpha"), ("DD", "Delta")]
        alpha, delta = one["hops"]
        # Our own contact wins for a hop only this node can name...
        assert alpha["identified_by"] == "node"
        assert alpha["node"]["known_locally"] is True
        assert alpha["node"]["direct_neighbour"] is True
        assert alpha["ambiguous"] is False and alpha["candidates"] == []
        # ...and the instance's per-packet resolution for one we cannot, with
        # the coordinates its hop lookup returned.
        assert delta["identified_by"] == "live"
        assert delta["node"]["public_key"] == DELTA
        assert delta["node"]["known_locally"] is False
        assert (delta["node"]["lat"], delta["node"]["lon"]) == (45.9, -74.1)

        (charlie,) = two["hops"]
        assert charlie["ambiguous"] is True
        assert charlie["node"]["name"] == "Charlie Two"
        assert charlie["node"]["known_locally"] is True
        assert {c["name"] for c in charlie["candidates"]} == {"Charlie One", "Charlie Two"}

    @pytest.mark.asyncio
    async def test_hop_lookups_carry_the_observer_and_skip_paths_we_can_name(self, instance):
        await _known_relays()
        await _mirror()
        # Give the first observation a path this node names on its own.
        instance.observations[0]["path_json"] = json.dumps(["AA", "BB"])
        del instance.observations[0]["resolved_path"]

        await live_feed_trace.get_trace(packet_hash=HASH)

        lookups = instance.calls("/api/resolve-hops")
        assert len(lookups) == 1
        assert lookups[0].url.params["hops"] == "CC"
        assert lookups[0].url.params["observer"] == OBS_2

    @pytest.mark.asyncio
    async def test_a_synthetic_hash_never_reaches_the_instance(self, instance):
        await _mirror(packet_hash="syn:abc")

        trace = await live_feed_trace.get_trace(packet_hash="syn:abc")

        assert trace is not None
        assert trace["routes"] == []
        assert trace["live_url"] is None
        assert instance.requests == []

    @pytest.mark.asyncio
    async def test_packet_gone_from_the_instance_is_reported_not_fatal(self, instance):
        await _known_relays()
        await _mirror()
        await _local()
        instance.detail_status = 404

        trace = await live_feed_trace.get_trace(packet_hash=HASH)

        assert trace is not None
        assert "HTTP 404" in (trace["live_error"] or "")
        assert [r["kind"] for r in trace["routes"]] == ["node"]
        assert trace["heard_by_node"] is True
        assert instance.calls("/api/resolve-hops") == []

    @pytest.mark.asyncio
    async def test_observer_list_failure_only_degrades(self, instance):
        await _known_relays()
        await _mirror()
        instance.observers_status = 500

        trace = await live_feed_trace.get_trace(packet_hash=HASH)

        assert trace is not None
        assert trace["live_error"] is None
        assert "observer list unavailable" in (trace["live_warning"] or "")
        assert len(trace["routes"]) == 2
        assert all(r["receiver"]["lat"] is None for r in trace["routes"])
        assert trace["routes"][1]["receiver"]["name"] == "Obs One"

    @pytest.mark.asyncio
    async def test_hop_lookup_failure_leaves_unknown_hops_honest(self, instance):
        await _known_relays()
        await _mirror()
        instance.resolve_status = 503

        trace = await live_feed_trace.get_trace(packet_hash=HASH)

        assert trace is not None
        assert "hop lookup failed" in (trace["live_warning"] or "")
        two, one = trace["routes"]
        # The instance's resolved_path still names DD; CC stays a local tie.
        assert _hops(one) == [("AA", "Alpha"), ("DD", None)]
        assert one["hops"][1]["node"]["public_key"] == DELTA
        (charlie,) = two["hops"]
        assert charlie["node"] is None
        assert charlie["ambiguous"] is True
        assert len(charlie["candidates"]) == 2

    @pytest.mark.asyncio
    async def test_packet_without_observation_list_uses_its_own_observer_fields(self, instance):
        await _mirror()
        instance.observations = []

        # The detail then carries observer fields on the packet itself.
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"/api/packets/{HASH}":
                return httpx.Response(
                    200,
                    json={
                        "packet": {
                            "hash": HASH,
                            "observer_id": OBS_1,
                            "observer_name": "Obs One",
                            "observer_iata": "YUL",
                            "snr": 1.5,
                            "rssi": -110,
                            "path_json": "[]",
                            "timestamp": _iso(NOW + 2),
                        },
                        "path": [],
                        "observation_count": 1,
                        "observations": [],
                    },
                )
            return instance.handler(request)

        live_feed._transport = httpx.MockTransport(handler)

        trace = await live_feed_trace.get_trace(packet_hash=HASH)

        assert trace is not None
        assert len(trace["routes"]) == 1
        assert trace["routes"][0]["receiver"]["name"] == "Obs One"
        assert trace["routes"][0]["hops"] == []


class TestNodeSide:
    @pytest.mark.asyncio
    async def test_both_sides_share_one_trace_with_the_node_first(self, instance):
        await _known_relays()
        await _mirror()
        message_id = await _local(path="BBAA")  # two 1-byte hops
        await MessageRepository.add_path(
            message_id, "", received_at=NOW + 5, path_len=0, rssi=-80, snr=9.0
        )

        # The row's hash alone is enough: the local twin is found by identity.
        trace = await live_feed_trace.get_trace(packet_hash=HASH)

        assert trace is not None
        assert trace["heard_by_node"] is True
        assert trace["message_id"] == message_id
        assert trace["outgoing"] is False
        kinds = [r["kind"] for r in trace["routes"]]
        assert kinds == ["node", "node", "observer", "observer"]
        relayed, direct = trace["routes"][:2]
        assert relayed["receiver"] == SELF
        assert relayed["heard_at"] == NOW + 4
        assert relayed["snr"] == 6.0 and relayed["rssi"] == -95.0
        assert _hops(relayed) == [("BB", "Bravo"), ("AA", "Alpha")]
        assert direct["hops"] == []
        assert direct["rssi"] == -80.0

    @pytest.mark.asyncio
    async def test_node_only_message_never_contacts_the_instance(self, instance):
        await _known_relays()
        message_id = await _local(text="Alice: only here", path="DD")

        trace = await live_feed_trace.get_trace(message_id=message_id)

        assert trace is not None
        assert trace["packet_hash"] is None and trace["live_url"] is None
        assert trace["live_first_seen"] is None
        assert [r["kind"] for r in trace["routes"]] == ["node"]
        # Nobody here matches DD and there is no packet to ask the instance about.
        (hop,) = trace["routes"][0]["hops"]
        assert hop == {
            "prefix": "DD",
            "node": None,
            "ambiguous": False,
            "candidates": [],
            "identified_by": None,
        }
        assert instance.requests == []

    @pytest.mark.asyncio
    async def test_sender_is_the_contact_with_that_name(self, instance):
        await ContactRepository.upsert(
            {"public_key": "ee" * 32, "name": "Alice", "type": 1, "lat": 45.4, "lon": -73.5}
        )
        message_id = await _local(text="Alice: only here")

        trace = await live_feed_trace.get_trace(message_id=message_id)

        assert trace is not None
        assert trace["sender"]["public_key"] == "ee" * 32
        assert (trace["sender"]["lat"], trace["sender"]["lon"]) == (45.4, -73.5)

    @pytest.mark.asyncio
    async def test_unknown_row_is_none(self, instance):
        assert await live_feed_trace.get_trace(packet_hash="0000000000000000") is None
        assert await live_feed_trace.get_trace(message_id=987654) is None


class TestTraceEndpoint:
    @pytest.mark.asyncio
    async def test_requires_an_identifier(self, test_db, client):
        response = await client.get("/api/live-feed/trace")
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_row_is_404(self, instance, client):
        response = await client.get("/api/live-feed/trace", params={"message_id": 4242})
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_the_trace(self, instance, client):
        await _known_relays()
        await _mirror()
        message_id = await _local()

        listed = await client.get("/api/live-feed/messages", params={"window": "1d"})
        (row,) = [m for m in listed.json()["messages"] if m["packet_hash"] == HASH]
        assert row["message_id"] == message_id

        response = await client.get(
            "/api/live-feed/trace",
            params={"packet_hash": row["packet_hash"], "message_id": row["message_id"]},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["packet_hash"] == HASH
        assert payload["message_id"] == message_id
        assert payload["heard_by_node"] is True
        assert [r["kind"] for r in payload["routes"]] == ["node", "observer", "observer"]
        assert payload["routes"][0]["hops"][0]["node"]["name"] == "Alpha"
        assert payload["fetched_at"] >= NOW
