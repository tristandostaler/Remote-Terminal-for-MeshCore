"""Where did a channel message travel? -- the Live Compare trace.

A "live only" verdict says this node missed a message; it does not say why.
The trace answers that by laying every known reception of the packet side by
side: each observer feeding the CoreScope instance recorded the relay hashes
the packet carried when it arrived (``path_json``), and this node's own
``messages.paths`` hold the same for what the radio heard. Resolving those
hashes to nodes -- first with this radio's contacts, then with the instance's
``/api/resolve-hops`` -- turns bare bytes into repeaters with coordinates, so
the operator can see whether the message ever got near their antenna or was
relayed only on the far side of the region.

Three remote calls at most feed one trace: the packet detail (mandatory --
without it there is nothing to show beyond our own paths), the observer list
(for observer coordinates, cached), and one hop resolution per distinct path
(capped). The first failing is reported as ``live_error``; the others degrade
to ``live_warning`` and the trace still renders with local knowledge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from app.models import Contact
from app.path_utils import split_path_hex
from app.repository.contacts import ContactRepository
from app.repository.live_feed import LiveFeedRepository
from app.repository.messages import MessageRepository
from app.repository.settings import AppSettingsRepository
from app.services.live_feed import LiveFeedClient, LiveFeedError, _coerce_float, _parse_iso

logger = logging.getLogger(__name__)

# Distinct hop sequences we ask the instance to resolve per trace. A busy
# packet can have dozens of observations, most sharing a handful of paths.
MAX_REMOTE_RESOLUTIONS = 8
# Alternatives listed for an ambiguous hop.
MAX_CANDIDATES = 8
OBSERVERS_CACHE_SECONDS = 600

_observers_cache: dict[str, tuple[float, dict[str, dict[str, Any]]]] = {}


def _node_dict(
    public_key: str | None,
    name: str | None,
    lat: float | None,
    lon: float | None,
    *,
    known_locally: bool = False,
    direct_neighbour: bool = False,
) -> dict[str, Any]:
    return {
        "public_key": public_key.lower() if public_key else None,
        "name": name or None,
        "lat": lat,
        "lon": lon,
        "known_locally": known_locally,
        "direct_neighbour": direct_neighbour,
    }


def _contact_node(contact: Contact) -> dict[str, Any]:
    return _node_dict(
        contact.public_key,
        contact.name,
        contact.lat,
        contact.lon,
        known_locally=True,
        direct_neighbour=contact.direct_path_len == 0,
    )


def _valid_location(lat: Any, lon: Any) -> bool:
    return (
        isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and not (lat == 0 and lon == 0)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def _json_list(value: Any) -> list[Any]:
    """``path_json``/``resolved_path`` arrive as JSON text or, on some builds, arrays."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if isinstance(value, dict):
        value = value.get("hops")
    return list(value) if isinstance(value, list) else []


def _hop_prefixes(value: Any) -> list[str]:
    return [str(hop).upper() for hop in _json_list(value) if isinstance(hop, (str, int))]


@dataclass
class _LocalIndex:
    """This radio's relays, matched by key prefix the way the firmware does."""

    relays: list[Contact]
    by_key: dict[str, Contact] = field(init=False)

    def __post_init__(self) -> None:
        self.by_key = {c.public_key.lower(): c for c in self.relays}

    def matches(self, prefix: str) -> list[Contact]:
        lowered = prefix.lower()
        return [c for c in self.relays if c.public_key.lower().startswith(lowered)]

    def get(self, public_key: str | None) -> Contact | None:
        return self.by_key.get(public_key.lower()) if public_key else None


@dataclass
class _RemoteHop:
    """One entry of a ``/api/resolve-hops`` answer, normalised."""

    public_key: str | None
    name: str | None
    ambiguous: bool
    candidates: list[dict[str, Any]]

    @classmethod
    def from_payload(cls, raw: dict[str, Any]) -> _RemoteHop:
        candidates: list[dict[str, Any]] = []
        seen: set[str] = set()
        for key in ("candidates", "conflicts"):
            for item in raw.get(key) or []:
                if not isinstance(item, dict):
                    continue
                pubkey = item.get("pubkey") or item.get("public_key")
                if not isinstance(pubkey, str) or pubkey.lower() in seen:
                    continue
                seen.add(pubkey.lower())
                lat, lon = _coerce_float(item.get("lat")), _coerce_float(item.get("lon"))
                if not _valid_location(lat, lon):
                    lat = lon = None
                candidates.append(_node_dict(pubkey, item.get("name"), lat, lon))
        pubkey = raw.get("pubkey") or raw.get("bestCandidate")
        return cls(
            public_key=pubkey.lower() if isinstance(pubkey, str) and pubkey else None,
            name=str(raw["name"]) if raw.get("name") else None,
            ambiguous=bool(raw.get("ambiguous")),
            candidates=candidates,
        )

    def location_of(self, public_key: str | None) -> tuple[float | None, float | None]:
        for candidate in self.candidates:
            if candidate["public_key"] == public_key:
                return candidate["lat"], candidate["lon"]
        return None, None


def resolve_hop(
    prefix: str,
    *,
    local: _LocalIndex,
    server_key: str | None,
    remote: _RemoteHop | None,
) -> dict[str, Any]:
    """Name one hop.

    Precedence: the instance's per-packet ``resolved_path`` (it saw the
    packet's neighbours), then a unique match among this radio's relays, then
    the instance's best guess, then an honest "unknown" with whatever
    candidates either side offered.
    """
    local_matches = local.matches(prefix)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_candidate(node: dict[str, Any]) -> None:
        key = node["public_key"]
        if key and key not in seen:
            seen.add(key)
            candidates.append(node)

    for contact in local_matches:
        add_candidate(_contact_node(contact))
    if remote is not None:
        for candidate in remote.candidates:
            known = local.get(candidate["public_key"])
            add_candidate(_contact_node(known) if known else candidate)

    def with_remote_location(node: dict[str, Any]) -> dict[str, Any]:
        if remote is not None and not _valid_location(node["lat"], node["lon"]):
            lat, lon = remote.location_of(node["public_key"])
            if _valid_location(lat, lon):
                node = {**node, "lat": lat, "lon": lon}
        return node

    node: dict[str, Any] | None = None
    identified_by: str | None = None
    ambiguous = False

    if server_key:
        known = local.get(server_key)
        if known is not None:
            node = _contact_node(known)
        else:
            name = remote.name if remote and remote.public_key == server_key else None
            lat, lon = remote.location_of(server_key) if remote else (None, None)
            node = _node_dict(server_key, name, lat, lon)
        identified_by = "live"
    elif len(local_matches) == 1:
        node = _contact_node(local_matches[0])
        identified_by = "node"
    elif remote is not None and remote.public_key:
        known = local.get(remote.public_key)
        node = (
            _contact_node(known)
            if known
            else _node_dict(remote.public_key, remote.name, None, None)
        )
        identified_by = "live"
        ambiguous = remote.ambiguous or len(local_matches) > 1
    elif len(local_matches) > 1:
        ambiguous = True

    if node is not None:
        node = with_remote_location(node)
    if server_key or identified_by == "node":
        # A firm identity: the alternatives are noise.
        ambiguous, shown = False, []
    elif node is not None:
        add_candidate(node)
        ambiguous = ambiguous or len(candidates) > 1
        shown = candidates if ambiguous else []
    else:
        ambiguous, shown = len(candidates) > 1, candidates

    return {
        "prefix": prefix,
        "node": node,
        "ambiguous": ambiguous,
        "candidates": shown[:MAX_CANDIDATES],
        "identified_by": identified_by,
    }


async def _observer_index(client: LiveFeedClient) -> dict[str, dict[str, Any]]:
    cached = _observers_cache.get(client.base_url)
    if cached and time.monotonic() - cached[0] < OBSERVERS_CACHE_SECONDS:
        return cached[1]
    index: dict[str, dict[str, Any]] = {}
    for observer in await client.fetch_observers():
        observer_id = observer.get("id") or observer.get("observer_id")
        if not isinstance(observer_id, str) or not observer_id:
            continue
        lat, lon = _coerce_float(observer.get("lat")), _coerce_float(observer.get("lon"))
        if not _valid_location(lat, lon):
            lat = lon = None
        index[observer_id.lower()] = {
            "name": observer.get("name"),
            "iata": observer.get("iata"),
            "lat": lat,
            "lon": lon,
        }
    _observers_cache[client.base_url] = (time.monotonic(), index)
    return index


def _self_node() -> dict[str, Any] | None:
    """This radio, from the connected device's self info; ``None`` while offline."""
    try:
        from app.services.radio_runtime import radio_runtime

        mc = radio_runtime.meshcore
        info = mc.self_info if mc is not None else None
    except Exception:  # pragma: no cover - defensive around the radio seam
        return None
    if not info:
        return None
    lat, lon = _coerce_float(info.get("adv_lat")), _coerce_float(info.get("adv_lon"))
    if not _valid_location(lat, lon):
        lat = lon = None
    return _node_dict(
        info.get("public_key") or None, info.get("name"), lat, lon, known_locally=True
    )


async def _sender_node(name: str | None, sender_key: str | None) -> dict[str, Any] | None:
    contact: Contact | None = None
    if sender_key:
        contact = await ContactRepository.get_by_key(sender_key)
    if contact is None and name:
        matches = await ContactRepository.get_by_name(name)
        if len(matches) == 1:
            contact = matches[0]
    if contact is None:
        return None
    lat, lon = contact.lat, contact.lon
    if not _valid_location(lat, lon):
        lat = lon = None
    return _node_dict(contact.public_key, contact.name or name, lat, lon, known_locally=True)


def _observations(detail: dict[str, Any]) -> list[dict[str, Any]]:
    """Every observation in a packet detail; the packet's own fields when it lists none."""
    observations = detail.get("observations")
    if not isinstance(observations, list) or not observations:
        packet = detail.get("packet")
        packet = packet if isinstance(packet, dict) else detail
        nested = packet.get("observations")
        if isinstance(nested, list) and nested:
            observations = nested
        else:
            observations = (
                [packet] if packet.get("observer_id") or packet.get("observer_name") else []
            )
    return [o for o in observations if isinstance(o, dict)]


async def _remote_resolutions(
    client: LiveFeedClient, paths: list[tuple[tuple[str, ...], str | None]]
) -> tuple[dict[tuple[str, ...], dict[str, _RemoteHop]], str | None]:
    """Ask the instance about each distinct hop sequence, a few at a time.

    Sequences past ``MAX_REMOTE_RESOLUTIONS`` share one observer-less lookup
    of their union, so a chatty packet still costs a bounded number of calls.
    """
    distinct: dict[tuple[str, ...], str | None] = {}
    for hops, observer in paths:
        if hops and hops not in distinct:
            distinct[hops] = observer
    if not distinct:
        return {}, None
    ordered = list(distinct.items())
    head, tail = ordered[:MAX_REMOTE_RESOLUTIONS], ordered[MAX_REMOTE_RESOLUTIONS:]

    async def lookup(hops: tuple[str, ...], observer: str | None) -> dict[str, _RemoteHop]:
        raw = await client.fetch_resolve_hops(list(dict.fromkeys(hops)), observer=observer)
        return {hop.upper(): _RemoteHop.from_payload(entry) for hop, entry in raw.items()}

    tasks = [lookup(hops, observer) for hops, observer in head]
    if tail:
        union = tuple(dict.fromkeys(hop for hops, _ in tail for hop in hops))
        tasks.append(lookup(union, None))
    results = await asyncio.gather(*tasks, return_exceptions=True)

    resolutions: dict[tuple[str, ...], dict[str, _RemoteHop]] = {}
    warning: str | None = None
    shared: dict[str, _RemoteHop] = {}
    for (hops, _observer), result in zip(head, results[: len(head)], strict=True):
        if isinstance(result, BaseException):
            warning = f"hop lookup failed: {result}"
            continue
        resolutions[hops] = result
    if tail:
        last = results[-1]
        if isinstance(last, BaseException):
            warning = f"hop lookup failed: {last}"
        else:
            shared = last
        for hops, _observer in tail:
            resolutions[hops] = shared
    return resolutions, warning


async def get_trace(
    *, packet_hash: str | None = None, message_id: int | None = None
) -> dict[str, Any] | None:
    """Build the trace for one merged row; ``None`` when neither side knows it."""
    settings = await AppSettingsRepository.get()
    live_row = await LiveFeedRepository.get_by_hash(packet_hash) if packet_hash else None
    message = await MessageRepository.get_by_id(message_id) if message_id else None
    if live_row is None and message is None:
        return None
    if live_row is not None and message is None:
        # The list joins on the dedup identity; find the twin the same way.
        message = await MessageRepository.find_channel_twin(
            live_row["channel_key"], live_row["text"], live_row.get("sender_timestamp")
        )

    relays = await ContactRepository.get_relays()
    local = _LocalIndex(relays)
    sender_name = (live_row or {}).get("sender") or (message.sender_name if message else None)
    if not sender_name and message is not None and ": " in message.text:
        sender_name = message.text.split(": ", 1)[0]

    routes: list[dict[str, Any]] = []
    self_node = _self_node()
    node_paths: list[tuple[tuple[str, ...], str | None]] = []
    if message is not None:
        for path in message.paths or []:
            hops = tuple(h.upper() for h in split_path_hex(path.path or "", path.path_len or 0))
            node_paths.append((hops, None))
            routes.append(
                {
                    "kind": "node",
                    "receiver": self_node,
                    "region": None,
                    "heard_at": path.received_at,
                    "snr": path.snr,
                    "rssi": float(path.rssi) if path.rssi is not None else None,
                    "hops": hops,
                }
            )
        if not message.paths and not message.outgoing:
            # Heard, but before this build recorded paths: still a reception.
            routes.append(
                {
                    "kind": "node",
                    "receiver": self_node,
                    "region": None,
                    "heard_at": message.received_at,
                    "snr": None,
                    "rssi": None,
                    "hops": (),
                }
            )

    live_error: str | None = None
    live_warning: str | None = None
    observer_routes: list[dict[str, Any]] = []
    remote_hash = packet_hash or (live_row or {}).get("packet_hash")
    url = settings.live_feed_url.rstrip("/")
    is_remote_hash = bool(remote_hash) and not str(remote_hash).startswith("syn:")

    resolutions: dict[tuple[str, ...], dict[str, _RemoteHop]] = {}
    async with LiveFeedClient(url, settings.live_feed_region) as client:
        if is_remote_hash:
            try:
                detail = await client.fetch_packet_detail(str(remote_hash))
            except LiveFeedError as exc:
                live_error = str(exc)
                detail = None
            if detail is not None:
                observers_index: dict[str, dict[str, Any]] = {}
                try:
                    observers_index = await _observer_index(client)
                except LiveFeedError as exc:
                    live_warning = f"observer list unavailable: {exc}"
                for observation in _observations(detail):
                    observer_id = observation.get("observer_id")
                    observer_id = str(observer_id).lower() if observer_id else None
                    meta = observers_index.get(observer_id or "", {})
                    hops = tuple(_hop_prefixes(observation.get("path_json")))
                    server_keys = [
                        str(k).lower() if isinstance(k, str) and k else None
                        for k in _json_list(observation.get("resolved_path"))
                    ]
                    receiver = _node_dict(
                        observer_id,
                        observation.get("observer_name") or meta.get("name"),
                        meta.get("lat"),
                        meta.get("lon"),
                    )
                    known = local.get(observer_id)
                    if known is not None:
                        receiver["known_locally"] = True
                        receiver["direct_neighbour"] = known.direct_path_len == 0
                        if not _valid_location(receiver["lat"], receiver["lon"]):
                            receiver["lat"], receiver["lon"] = known.lat, known.lon
                    observer_routes.append(
                        {
                            "kind": "observer",
                            "receiver": receiver,
                            "region": observation.get("observer_iata") or None,
                            "heard_at": _parse_iso(observation.get("timestamp")),
                            "snr": _coerce_float(observation.get("snr")),
                            "rssi": _coerce_float(observation.get("rssi")),
                            "hops": hops,
                            "_server_keys": server_keys,
                        }
                    )
        wanted = node_paths + [
            (route["hops"], route["receiver"]["public_key"]) for route in observer_routes
        ]
        # Only bother the instance for hops this radio cannot name on its own.
        needs_remote = [
            (hops, observer)
            for hops, observer in wanted
            if any(len(local.matches(h)) != 1 for h in hops)
        ]
        if needs_remote and is_remote_hash and live_error is None:
            resolutions, resolve_warning = await _remote_resolutions(client, needs_remote)
            live_warning = live_warning or resolve_warning

    by_time = lambda r: (r["heard_at"] is None, r["heard_at"] or 0)  # noqa: E731
    all_routes = sorted(routes, key=by_time) + sorted(observer_routes, key=by_time)
    for route in all_routes:
        hops: tuple[str, ...] = route.pop("hops")
        server_keys = route.pop("_server_keys", [])
        remote_for_path = resolutions.get(hops, {})
        route["hops"] = [
            resolve_hop(
                prefix,
                local=local,
                server_key=server_keys[i] if i < len(server_keys) else None,
                remote=remote_for_path.get(prefix),
            )
            for i, prefix in enumerate(hops)
        ]

    return {
        "packet_hash": remote_hash,
        "live_url": f"{url}/#/packets/{remote_hash}" if is_remote_hash else None,
        "message_id": message.id if message else None,
        "heard_by_node": message is not None,
        "outgoing": bool(message.outgoing) if message else False,
        "sender": await _sender_node(sender_name, message.sender_key if message else None),
        "self_node": self_node,
        "live_first_seen": (live_row or {}).get("first_seen"),
        "live_last_seen": (live_row or {}).get("last_seen"),
        "live_repeats": (live_row or {}).get("repeats"),
        "routes": all_routes,
        "live_error": live_error,
        "live_warning": live_warning,
        "fetched_at": int(time.time()),
    }


def _reset_for_tests() -> None:
    _observers_cache.clear()
