"""Mirror channel messages from a CoreScope instance (live.meshcore.ca) and compare.

live.meshcore.ca runs CoreScope, whose HTTP API is public and unauthenticated.
Two of its endpoints matter here:

* ``GET /api/packets?type=5&since=&limit=&offset=&region=`` -- every GRP_TXT
  (channel text) transmission the observers heard, with the raw packet bytes
  (``raw_hex``) or at least the encrypted envelope (``decoded_json`` carrying
  ``channelHash`` / ``mac`` / ``encryptedData``). We decrypt those ourselves
  with the keys this node holds, exactly as ``packet_processor`` does for the
  radio, so **any** channel the node knows compares -- Public, hashtag and
  private ones alike -- whether or not the remote instance has the key.
* ``GET /api/channels/{name}/messages`` -- the remote instance's own decryption
  of one channel. Only channels it holds a key for (Public, community hashtag
  channels). Used as a fallback when the packet feed does not expose
  ciphertext, so Public still compares against a locked-down instance.

Either way the message is re-keyed to the local ``(conversation_key, text,
sender_timestamp)`` dedup identity and the SQL in ``LiveFeedRepository`` does
the rest.

Region: CoreScope tags every observation with the observer's IATA region code
(``meshcore/{IATA}/{PUBKEY}/packets``). ``region=YUL,YQB`` restricts the feed
to messages heard by observers in those regions. This is a geographic filter
on the *remote* side and has nothing to do with MeshCore flood-scope regions.

Polling: the first sync (and any sync after the URL, region or channel
selection changed) walks the packet feed back to ``LOOKBACK_SECONDS``. Every
later sync asks only for packets *observed* since the previous sync started,
minus ``CURSOR_OVERLAP_SECONDS`` -- CoreScope's ``since=`` is on observation
time, so a late repeat of an old message still comes through and refreshes its
counts. The cursor lives in memory: a restart costs one full walk, nothing
more. Rows whose observation count and last-seen time did not move are not
rewritten, so a quiet poll is a handful of reads and no writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.channel_constants import (
    PUBLIC_CHANNEL_KEY,
    PUBLIC_CHANNEL_NAME,
    hashtag_channel_key,
    is_public_channel_name,
)
from app.compression.metadata import decode_and_describe
from app.decoder import PayloadType, decrypt_group_text, extract_payload, get_packet_payload_type
from app.models import ALL_LIVE_FEED_CHANNELS, AppSettings
from app.repository.channels import ChannelRepository
from app.repository.live_feed import LiveFeedRepository
from app.repository.settings import AppSettingsRepository
from app.stats_windows import window_cutoff

logger = logging.getLogger(__name__)

# How far back each sync walks the remote list. Wide enough that a message the
# node decrypts late (historical decrypt after a channel is added) still finds
# its twin; narrow enough that a busy channel is a handful of pages.
LOOKBACK_SECONDS = 7 * 86400
# Mirrored rows older than this are dropped. Wider than the lookback so the
# "30d" statistics window keeps its comparison; nothing else references them.
RETENTION_SECONDS = 90 * 86400
PAGE_LIMIT = 200
MAX_PAGES_PER_CHANNEL = 40
# The packet feed covers every channel at once, so it gets a larger budget.
MAX_PACKET_PAGES = 100
GROUP_TEXT_PAYLOAD_TYPE = int(PayloadType.GROUP_TEXT)
HTTP_TIMEOUT_SECONDS = 20.0
REGIONS_CACHE_SECONDS = 600
# Wait this long after a failed sync before letting the loop retry, whatever
# the configured interval says -- a dead host should not be hammered.
MIN_RETRY_AFTER_ERROR_SECONDS = 120
# Incremental polls re-read this much before the previous sync started, so an
# observation that landed while that sync was running is not missed.
CURSOR_OVERLAP_SECONDS = 600
# How long POST /live-feed/sync waits for the sync it started before answering
# with "still syncing" -- short of common reverse-proxy timeouts.
SYNC_REQUEST_WAIT_SECONDS = 20.0


class LiveFeedError(Exception):
    """The remote instance could not be read (network, HTTP status, or shape)."""


@dataclass
class LiveFeedState:
    syncing: bool = False
    last_sync_started_at: int | None = None
    last_sync_completed_at: int | None = None
    last_success_at: int | None = None
    last_error: str | None = None
    last_fetched: int = 0
    last_changed: int = 0
    unresolved_channels: list[str] = field(default_factory=list)
    source: str = "packets"
    last_sync_full: bool = False
    # Observation-time watermark of the last successful sync and the
    # url|region|channels fingerprint it was taken under.
    cursor: int | None = None
    cursor_scope: str | None = None


_state = LiveFeedState()
_task: asyncio.Task | None = None
# Created by the loop on its own event loop: an ``asyncio.Event`` binds to the
# first loop that waits on it, and a module-level one would break as soon as a
# second loop (each test, a restarted server) touched it.
_wake: asyncio.Event | None = None
_sync_lock = asyncio.Lock()
_regions_cache: dict[str, tuple[float, list[dict[str, str]]]] = {}
# Test seam: an ``httpx`` transport swapped in for the real network.
_transport: Any = None


# ─── Remote client ─────────────────────────────────────────────────────────


def _parse_iso(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # Tolerate epoch seconds or milliseconds.
        return int(value / 1000) if value > 10_000_000_000 else int(value)
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())


def _coerce_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_channel_names(names: list[str]) -> list[str]:
    """Trim, drop blanks, dedupe case-insensitively, keep order."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in names:
        name = (raw or "").strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        cleaned.append(name)
    return cleaned


def normalize_region(region: str) -> str:
    """``" yul, yqb "`` -> ``"YUL,YQB"``. Empty means every region."""
    codes = [part.strip().upper() for part in (region or "").split(",")]
    return ",".join(code for code in codes if code)


def normalize_live_text(sender: str | None, text: str) -> str:
    """Re-key a remote message to the exact text this node would have stored.

    Local channel messages are stored as ``"Sender: body"`` with the body run
    through MCMP decompression. CoreScope returns ``sender`` and ``text``
    separately -- sometimes with the ``"Sender: "`` prefix still on the text,
    sometimes not -- so both shapes normalize to the local one.
    """
    body = text or ""
    if sender:
        prefix = f"{sender}: "
        if body.startswith(prefix):
            body = body[len(prefix) :]
    body, _compression = decode_and_describe(body)
    return f"{sender}: {body}" if sender else body


def _synthetic_hash(channel: str, sender_timestamp: int | None, text: str) -> str:
    digest = hashlib.sha256(f"{channel}\x00{sender_timestamp}\x00{text}".encode()).hexdigest()
    return f"syn:{digest[:16]}"


def normalize_live_message(
    raw: dict[str, Any], channel_name: str, channel_key: str | None
) -> dict[str, Any] | None:
    """One remote message -> one ``live_feed_messages`` row, or ``None`` if unusable."""
    if not isinstance(raw, dict):
        return None
    sender = raw.get("sender")
    sender = str(sender).strip() if sender not in (None, "") else None
    if sender and sender.casefold() == "unknown":
        sender = None
    raw_text = raw.get("text")
    if raw_text is None:
        return None
    text = normalize_live_text(sender, str(raw_text))
    if not text.strip():
        return None
    sender_timestamp = _coerce_int(raw.get("sender_timestamp"))
    latest = _parse_iso(raw.get("timestamp"))
    first = _parse_iso(raw.get("first_seen")) or latest
    if first is None:
        first = sender_timestamp or int(time.time())
    latest = latest or first
    packet_hash = raw.get("packetHash") or raw.get("packet_hash") or raw.get("hash")
    packet_hash = (
        str(packet_hash) if packet_hash else _synthetic_hash(channel_name, sender_timestamp, text)
    )
    observers_raw = raw.get("observers")
    observers = (
        [str(o) for o in observers_raw if o is not None] if isinstance(observers_raw, list) else []
    )
    return {
        "packet_hash": packet_hash,
        "channel_name": channel_name,
        "channel_key": channel_key,
        "sender": sender,
        "text": text,
        "sender_timestamp": sender_timestamp,
        "first_seen": first,
        "last_seen": max(first, latest),
        "repeats": _coerce_int(raw.get("repeats")) or 1,
        "observers": observers,
        "hops": _coerce_int(raw.get("hops")),
        "snr": _coerce_float(raw.get("snr")),
        "scope_name": raw.get("scope_name") if isinstance(raw.get("scope_name"), str) else None,
    }


class LiveFeedClient:
    """Thin async client for the handful of CoreScope endpoints we read.

    Use it as an async context manager so a whole sync -- up to a hundred
    pages -- rides one connection pool instead of a TCP+TLS handshake per page.
    Outside a context each call opens a short-lived client, which is fine for
    the one-off region lookup.
    """

    def __init__(self, base_url: str, region: str = "", *, transport: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.region = normalize_region(region)
        self._transport = transport if transport is not None else _transport
        self._client: Any = None

    def _new_client(self) -> Any:
        import httpx

        return httpx.AsyncClient(
            timeout=HTTP_TIMEOUT_SECONDS,
            follow_redirects=True,
            transport=self._transport,
            headers={"Accept": "application/json"},
        )

    async def __aenter__(self) -> LiveFeedClient:
        self._client = self._new_client()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        try:
            if self._client is not None:
                response = await self._client.get(url, params=params or None)
            else:
                async with self._new_client() as client:
                    response = await client.get(url, params=params or None)
        except Exception as exc:  # httpx raises many transport subclasses
            raise LiveFeedError(f"{url}: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise LiveFeedError(f"{url}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError as exc:
            raise LiveFeedError(f"{url}: response is not JSON") from exc

    async def fetch_channel_messages(
        self, channel_name: str, *, limit: int | None = None, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int | None]:
        params: dict[str, Any] = {"limit": limit or PAGE_LIMIT, "offset": offset}
        if self.region:
            params["region"] = self.region
        payload = await self._get_json(
            f"/api/channels/{quote(channel_name, safe='')}/messages", params
        )
        if isinstance(payload, list):
            return [m for m in payload if isinstance(m, dict)], None
        if not isinstance(payload, dict):
            raise LiveFeedError("channel messages: unexpected response shape")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise LiveFeedError("channel messages: missing 'messages' list")
        return [m for m in messages if isinstance(m, dict)], _coerce_int(payload.get("total"))

    async def fetch_packets(
        self,
        *,
        since: int,
        offset: int = 0,
        limit: int | None = None,
        region_code: str | None = None,
    ) -> tuple[list[dict[str, Any]], int | None]:
        """One page of GRP_TXT transmissions heard since ``since`` (unix seconds)."""
        params: dict[str, Any] = {
            "type": GROUP_TEXT_PAYLOAD_TYPE,
            "limit": limit or PAGE_LIMIT,
            "offset": offset,
            "order": "desc",
            "since": datetime.fromtimestamp(since, tz=UTC).isoformat().replace("+00:00", "Z"),
        }
        if region_code:
            params["region"] = region_code
        payload = await self._get_json("/api/packets", params)
        if isinstance(payload, list):
            return [p for p in payload if isinstance(p, dict)], None
        if not isinstance(payload, dict):
            raise LiveFeedError("packets: unexpected response shape")
        packets = payload.get("packets")
        if not isinstance(packets, list):
            raise LiveFeedError("packets: missing 'packets' list")
        return [p for p in packets if isinstance(p, dict)], _coerce_int(payload.get("total"))

    async def fetch_regions(self) -> list[dict[str, str]]:
        payload = await self._get_json("/api/config/regions")
        regions: list[dict[str, str]] = []
        if isinstance(payload, dict):
            nested = payload.get("regions")
            items: dict[Any, Any] = nested if isinstance(nested, dict) else payload
            for code, label in items.items():
                if isinstance(code, str) and code:
                    regions.append(
                        {"code": code, "label": str(label) if label is not None else code}
                    )
        elif isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict) and item.get("code"):
                    regions.append(
                        {"code": str(item["code"]), "label": str(item.get("label") or item["code"])}
                    )
        regions.sort(key=lambda r: (r["label"].casefold(), r["code"]))
        return regions


# ─── Channel resolution ────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComparedChannel:
    """One channel the comparison covers: its local key, display name and hash byte."""

    key: str
    name: str

    @property
    def key_bytes(self) -> bytes:
        return bytes.fromhex(self.key)

    @property
    def hash_byte(self) -> int:
        return hashlib.sha256(self.key_bytes).digest()[0]

    @property
    def remote_name(self) -> str | None:
        """The name the remote instance would list this channel under, if any.

        Public and hashtag channels are named the same everywhere; a private
        channel's local name means nothing to another instance.
        """
        if self.key == PUBLIC_CHANNEL_KEY:
            return PUBLIC_CHANNEL_NAME
        if self.name.startswith("#") and hashtag_channel_key(self.name) == self.key:
            return self.name
        return None


def _is_channel_key(value: str) -> bool:
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)


async def resolve_compared_channels(
    entries: list[str],
) -> tuple[list[ComparedChannel], list[str]]:
    """Turn the ``live_feed_channels`` setting into channels with keys.

    Accepted entries: ``*`` (every channel this node knows), a 32-hex channel
    key, ``Public``, a ``#hashtag`` name (key derived from the name, so it
    compares before the node joins it), or the name of a local channel.
    Returns the channels plus the entries nothing could be made of.
    """
    local = await ChannelRepository.get_all()
    by_key = {channel.key.upper(): channel for channel in local}
    by_name = {channel.name.casefold(): channel for channel in local}

    chosen: dict[str, ComparedChannel] = {}
    unresolved: list[str] = []

    def add(key: str, fallback_name: str) -> None:
        key = key.upper()
        if key in chosen:
            return
        channel = by_key.get(key)
        if channel is not None:
            name = channel.name
        elif key == PUBLIC_CHANNEL_KEY:
            name = PUBLIC_CHANNEL_NAME
        else:
            name = fallback_name
        chosen[key] = ComparedChannel(key=key, name=name)

    for raw in entries:
        entry = (raw or "").strip()
        if not entry:
            continue
        if entry == ALL_LIVE_FEED_CHANNELS:
            for channel in local:
                add(channel.key, channel.name)
            add(PUBLIC_CHANNEL_KEY, PUBLIC_CHANNEL_NAME)
        elif _is_channel_key(entry):
            add(entry, entry[:8].upper())
        elif is_public_channel_name(entry):
            add(PUBLIC_CHANNEL_KEY, PUBLIC_CHANNEL_NAME)
        elif entry.startswith("#"):
            add(hashtag_channel_key(entry), entry)
        elif entry.casefold() in by_name:
            channel = by_name[entry.casefold()]
            add(channel.key, channel.name)
        else:
            unresolved.append(entry)
    return list(chosen.values()), unresolved


async def compare_channel_keys(settings: AppSettings | None = None) -> list[str]:
    settings = settings or await AppSettingsRepository.get()
    channels, _unresolved = await resolve_compared_channels(settings.live_feed_channels)
    return [channel.key for channel in channels]


# ─── Sync ──────────────────────────────────────────────────────────────────


def _packet_payload(packet: dict[str, Any]) -> bytes | None:
    """The GRP_TXT payload bytes of a remote packet, from whichever field carries them."""
    raw_hex = packet.get("raw_hex") or packet.get("raw")
    if isinstance(raw_hex, str) and raw_hex:
        try:
            raw = bytes.fromhex(raw_hex)
        except ValueError:
            raw = b""
        if raw and get_packet_payload_type(raw) == PayloadType.GROUP_TEXT:
            payload = extract_payload(raw)
            if payload:
                return payload
    decoded = packet.get("decoded_json") or packet.get("decoded")
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except ValueError:
            decoded = None
    if isinstance(decoded, dict):
        nested = decoded.get("payload")
        envelope: dict[str, Any] = nested if isinstance(nested, dict) else decoded
        channel_hash = _coerce_int(envelope.get("channelHash"))
        mac = envelope.get("mac")
        encrypted = envelope.get("encryptedData")
        if channel_hash is not None and isinstance(mac, str) and isinstance(encrypted, str):
            try:
                return bytes([channel_hash & 0xFF]) + bytes.fromhex(mac) + bytes.fromhex(encrypted)
            except ValueError:
                return None
    return None


def _corescope_hash(payload: bytes) -> str:
    """CoreScope's transmission hash: SHA256(payload_type_byte || payload)[:16]."""
    return hashlib.sha256(bytes([GROUP_TEXT_PAYLOAD_TYPE]) + payload).hexdigest()[:16]


def _packet_hops(packet: dict[str, Any]) -> int | None:
    path = packet.get("path_json")
    if isinstance(path, str):
        try:
            path = json.loads(path)
        except ValueError:
            return None
    if isinstance(path, dict):
        path = path.get("hops")
    return len(path) if isinstance(path, list) else None


def decrypt_live_packet(
    packet: dict[str, Any], channels_by_hash: dict[int, list[ComparedChannel]]
) -> dict[str, Any] | None:
    """Decrypt one remote GRP_TXT packet with the compared channels' keys.

    Returns a ``live_feed_messages`` row, or ``None`` when the packet carries
    no ciphertext or belongs to a channel we hold no key for.
    """
    payload = _packet_payload(packet)
    if payload is None or len(payload) < 3:
        return None
    for channel in channels_by_hash.get(payload[0], ()):
        decrypted = decrypt_group_text(payload, channel.key_bytes)
        if decrypted is None:
            continue
        text = normalize_live_text(decrypted.sender, decrypted.message)
        if not text.strip():
            return None
        latest = _parse_iso(packet.get("timestamp"))
        first = _parse_iso(packet.get("first_seen")) or latest or int(time.time())
        latest = latest or first
        observer = packet.get("observer_name") or packet.get("observer_id")
        return {
            "packet_hash": str(packet.get("hash") or _corescope_hash(payload)),
            "channel_name": channel.name,
            "channel_key": channel.key,
            "sender": decrypted.sender,
            "text": text,
            "sender_timestamp": decrypted.timestamp,
            "first_seen": first,
            "last_seen": max(first, latest),
            "repeats": _coerce_int(packet.get("observation_count")) or 1,
            "observers": [str(observer)] if observer else [],
            "hops": _packet_hops(packet),
            "snr": _coerce_float(packet.get("snr")),
            "scope_name": None,
        }
    return None


@dataclass
class _PacketSyncResult:
    fetched: int = 0
    changed: int = 0
    packets: int = 0
    with_ciphertext: int = 0


async def _sync_packets(
    client: LiveFeedClient, channels: list[ComparedChannel], since: int
) -> _PacketSyncResult:
    """Walk the remote GRP_TXT feed observed since ``since`` and decrypt what we can."""
    result = _PacketSyncResult()
    channels_by_hash: dict[int, list[ComparedChannel]] = defaultdict(list)
    for channel in channels:
        channels_by_hash[channel.hash_byte].append(channel)
    # The packet endpoint takes one region at a time; several codes mean one
    # walk each, de-duplicated by hash in the upsert.
    region_codes: list[str | None] = [c for c in client.region.split(",") if c] or [None]
    seen: set[str] = set()
    for region_code in region_codes:
        offset = 0
        for _page in range(MAX_PACKET_PAGES):
            packets, total = await client.fetch_packets(
                since=since, offset=offset, region_code=region_code
            )
            if not packets:
                break
            rows: list[dict[str, Any]] = []
            for packet in packets:
                payload_type = _coerce_int(packet.get("payload_type"))
                if payload_type is not None and payload_type != GROUP_TEXT_PAYLOAD_TYPE:
                    continue
                result.packets += 1
                if _packet_payload(packet) is not None:
                    result.with_ciphertext += 1
                row = decrypt_live_packet(packet, channels_by_hash)
                if row is not None and row["packet_hash"] not in seen:
                    seen.add(row["packet_hash"])
                    rows.append(row)
            inserted, updated = await LiveFeedRepository.upsert_many(rows)
            result.fetched += len(rows)
            result.changed += inserted + updated
            offset += len(packets)
            if len(packets) < PAGE_LIMIT or (total is not None and offset >= total):
                break
    return result


async def _sync_channel_messages(
    client: LiveFeedClient, channel: ComparedChannel, horizon: int
) -> tuple[int, int]:
    """Fallback: walk the remote instance's own decryption of one channel.

    Returns ``(fetched, changed)``. The list is newest-activity first, so it is
    walked until a whole page is older than ``horizon``.
    """
    remote_name = channel.remote_name
    if remote_name is None:
        return 0, 0
    fetched = 0
    changed = 0
    offset = 0
    for _page in range(MAX_PAGES_PER_CHANNEL):
        messages, total = await client.fetch_channel_messages(remote_name, offset=offset)
        if not messages:
            break
        rows = [
            row
            for row in (normalize_live_message(m, channel.name, channel.key) for m in messages)
            if row is not None
        ]
        inserted, updated = await LiveFeedRepository.upsert_many(rows)
        fetched += len(rows)
        changed += inserted + updated
        offset += len(messages)
        if len(messages) < PAGE_LIMIT or (total is not None and offset >= total):
            break
        if rows and max(row["last_seen"] for row in rows) < horizon:
            break
    return fetched, changed


def _sync_scope(settings: AppSettings, channels: list[ComparedChannel]) -> str:
    """What the cursor is valid for; any change here means a full walk."""
    keys = ",".join(sorted(channel.key for channel in channels))
    return (
        f"{settings.live_feed_url.rstrip('/')}|{normalize_region(settings.live_feed_region)}|{keys}"
    )


async def sync_once(settings: AppSettings | None = None, *, force: bool = False) -> LiveFeedState:
    """Run one full sync. ``force`` runs it even while the feature is disabled."""
    settings = settings or await AppSettingsRepository.get()
    if not settings.live_feed_enabled and not force:
        return _state
    if _sync_lock.locked():
        # A sync is already running (the loop woke on a settings change, say).
        # Wait for it rather than starting a second walk of the same pages.
        async with _sync_lock:
            return _state
    async with _sync_lock:
        now = int(time.time())
        _state.syncing = True
        _state.last_sync_started_at = now
        try:
            channels, unresolved = await resolve_compared_channels(settings.live_feed_channels)
            _state.unresolved_channels = unresolved
            scope = _sync_scope(settings, channels)
            full_walk = _state.cursor is None or _state.cursor_scope != scope
            since = (
                now - LOOKBACK_SECONDS
                if full_walk or _state.cursor is None
                else max(now - LOOKBACK_SECONDS, _state.cursor - CURSOR_OVERLAP_SECONDS)
            )
            fetched = 0
            changed = 0
            source = "packets"
            async with LiveFeedClient(settings.live_feed_url, settings.live_feed_region) as client:
                if not channels:
                    packet_result = _PacketSyncResult()
                    packet_feed_available = True
                else:
                    packet_feed_available = True
                    try:
                        packet_result = await _sync_packets(client, channels, since)
                    except LiveFeedError as exc:
                        # An instance without the packet feed (or one that hides
                        # it) still serves its own decryption of public channels.
                        if "HTTP 404" not in str(exc) and "HTTP 403" not in str(exc):
                            raise
                        logger.info("Live feed packet endpoint unavailable (%s); falling back", exc)
                        packet_feed_available = False
                        packet_result = _PacketSyncResult()
                    fetched = packet_result.fetched
                    changed = packet_result.changed
                    # Fall back to the remote instance's own decryption when the
                    # packet feed gave us nothing to decrypt: no feed at all, or
                    # packets stripped of their ciphertext.
                    if not packet_feed_available or (
                        packet_result.packets > 0 and packet_result.with_ciphertext == 0
                    ):
                        source = "channel_messages"
                        for channel in channels:
                            got, moved = await _sync_channel_messages(client, channel, since)
                            fetched += got
                            changed += moved
            _state.source = source
            _state.last_sync_full = full_walk
            _state.cursor = now
            _state.cursor_scope = scope
            await LiveFeedRepository.prune_older_than(now - RETENTION_SECONDS)
            _state.last_fetched = fetched
            _state.last_changed = changed
            _state.last_success_at = int(time.time())
            _state.last_error = None
            logger.info(
                "Live feed sync (%s): %d messages checked, %d changed, from %s (%s, %d channels, via %s)",
                "full" if full_walk else "incremental",
                fetched,
                changed,
                settings.live_feed_url,
                settings.live_feed_region or "all regions",
                len(channels),
                source,
            )
        except LiveFeedError as exc:
            _state.last_error = str(exc)
            logger.warning("Live feed sync failed: %s", exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _state.last_error = f"{exc.__class__.__name__}: {exc}"
            logger.exception("Live feed sync crashed")
        finally:
            _state.syncing = False
            _state.last_sync_completed_at = int(time.time())
    return _state


async def _loop() -> None:
    global _wake
    _wake = asyncio.Event()
    wake = _wake
    while True:
        # Clear *before* reading settings: a wake that lands while a sync is
        # running must trigger another pass with the new settings, not be
        # swallowed by a clear after the sync.
        wake.clear()
        settings = None
        try:
            settings = await AppSettingsRepository.get()
            if settings.live_feed_enabled:
                await sync_once(settings)
        except asyncio.CancelledError:
            raise
        except RuntimeError as exc:
            # "Database not connected": the server is still starting or already
            # shutting down. Nothing to do until the next tick.
            logger.debug("Live feed loop skipped a tick: %s", exc)
        except Exception:
            logger.exception("Live feed loop error")

        interval = settings.live_feed_poll_interval if settings else 300
        if _state.last_error:
            interval = max(interval, MIN_RETRY_AFTER_ERROR_SECONDS)
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=interval)


async def start_live_feed() -> None:
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())


async def stop_live_feed() -> None:
    global _task, _wake
    if _task is None:
        return
    if not _task.done():
        _task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _task
    _task = None
    _wake = None


async def request_sync(wait_seconds: float = SYNC_REQUEST_WAIT_SECONDS) -> None:
    """Start a sync now and wait for it up to ``wait_seconds``.

    A first sync of a busy region can take minutes; an HTTP request that waits
    for all of it dies at the first proxy timeout while the sync carries on.
    So the sync runs as its own task, the caller gets a bounded wait, and the
    UIs (which poll status anyway) pick the outcome up when it lands.
    """
    task = asyncio.create_task(sync_once(force=True))
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=wait_seconds)


def notify_settings_changed() -> None:
    """Wake the loop so a settings change takes effect now, not next interval."""
    if _wake is not None:
        _wake.set()


def _wake_requested() -> bool:
    return _wake is not None and _wake.is_set()


# ─── Read side ─────────────────────────────────────────────────────────────


async def get_status(
    settings: AppSettings | None = None, channels: list[ComparedChannel] | None = None
) -> dict[str, Any]:
    settings = settings or await AppSettingsRepository.get()
    if channels is None:
        channels, _unresolved = await resolve_compared_channels(settings.live_feed_channels)
    return {
        "enabled": settings.live_feed_enabled,
        "url": settings.live_feed_url,
        "region": normalize_region(settings.live_feed_region),
        "channels": [channel.name for channel in channels],
        "poll_interval": settings.live_feed_poll_interval,
        "mirrored_messages": await LiveFeedRepository.count(),
        **{k: v for k, v in asdict(_state).items() if k not in ("cursor", "cursor_scope")},
    }


async def get_compare_stats(window: str, settings: AppSettings | None = None) -> dict | None:
    """The statistics-page section; ``None`` when there is nothing to compare yet."""
    settings = settings or await AppSettingsRepository.get()
    channels, _unresolved = await resolve_compared_channels(settings.live_feed_channels)
    status = await get_status(settings, channels)
    if not settings.live_feed_enabled and status["mirrored_messages"] == 0:
        return None
    now = int(time.time())
    keys = [channel.key for channel in channels]
    stats = await LiveFeedRepository.get_compare_stats(keys, window_cutoff(window, now), now)
    return {"status": status, **stats}


async def list_messages(
    window: str,
    *,
    channel_key: str | None = None,
    source: str | None = None,
    q: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    settings = await AppSettingsRepository.get()
    keys = await compare_channel_keys(settings)
    messages, total, counts = await LiveFeedRepository.list_merged(
        keys,
        window_cutoff(window, int(time.time())),
        channel_key=channel_key,
        source=source,
        q=q,
        limit=limit,
        offset=offset,
    )
    return {"window": window, "messages": messages, "total": total, "counts": counts}


async def get_regions(settings: AppSettings | None = None) -> dict[str, Any]:
    settings = settings or await AppSettingsRepository.get()
    url = settings.live_feed_url.rstrip("/")
    cached = _regions_cache.get(url)
    if cached and time.monotonic() - cached[0] < REGIONS_CACHE_SECONDS:
        return {"url": url, "regions": cached[1]}
    regions = await LiveFeedClient(url).fetch_regions()
    _regions_cache[url] = (time.monotonic(), regions)
    return {"url": url, "regions": regions}


def _reset_for_tests() -> None:
    global _state, _transport
    _state = LiveFeedState()
    _transport = None
    _regions_cache.clear()
    if _wake is not None:
        _wake.clear()
