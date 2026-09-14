"""Mirror channel messages from a CoreScope instance (live.meshcore.ca) and compare.

live.meshcore.ca runs CoreScope, whose HTTP API is public and unauthenticated.
``GET /api/channels/{name}/messages?limit=&offset=&region=`` returns the
server-decrypted messages of one channel, newest observation first, with the
same ``sender`` / ``text`` / ``sender_timestamp`` triple we store locally.
That is everything the comparison needs: the message is re-keyed to the local
``(conversation_key, text, sender_timestamp)`` dedup identity and the SQL in
``LiveFeedRepository`` does the rest.

Region: CoreScope tags every observation with the observer's IATA region code
(``meshcore/{IATA}/{PUBKEY}/packets``). ``region=YUL,YQB`` restricts the feed
to messages heard by observers in those regions. This is a geographic filter
on the *remote* side and has nothing to do with MeshCore flood-scope regions.

Polling: the remote list is ordered by latest observation, so an old message
that a late repeater re-observes floats back to the top. Each sync therefore
walks pages until the page's newest activity is older than ``LOOKBACK_SECONDS``
(or a page cap), upserting by packet hash. Cheap, idempotent, and it needs no
cursor state to survive restarts.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from app.channel_constants import PUBLIC_CHANNEL_KEY, hashtag_channel_key, is_public_channel_name
from app.compression.metadata import decode_and_describe
from app.models import AppSettings
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
HTTP_TIMEOUT_SECONDS = 20.0
REGIONS_CACHE_SECONDS = 600
# Wait this long after a failed sync before letting the loop retry, whatever
# the configured interval says -- a dead host should not be hammered.
MIN_RETRY_AFTER_ERROR_SECONDS = 120


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
    unresolved_channels: list[str] = field(default_factory=list)


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
    """Thin async client for the handful of CoreScope endpoints we read."""

    def __init__(self, base_url: str, region: str = "", *, transport: Any = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.region = normalize_region(region)
        self._transport = transport if transport is not None else _transport

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        import httpx

        url = f"{self.base_url}{path}"
        try:
            async with httpx.AsyncClient(
                timeout=HTTP_TIMEOUT_SECONDS,
                follow_redirects=True,
                transport=self._transport,
                headers={"Accept": "application/json"},
            ) as client:
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


async def resolve_channel_keys(names: list[str]) -> dict[str, str | None]:
    """Map live channel names to this node's channel keys.

    ``Public`` and hashtag channels derive their key from the name alone, so
    they compare even before the node has joined them. Anything else must
    match a local channel by name (case-insensitive) or stays unresolved.
    """
    resolved: dict[str, str | None] = {}
    local_by_name: dict[str, str] | None = None
    for name in names:
        if is_public_channel_name(name):
            resolved[name] = PUBLIC_CHANNEL_KEY
            continue
        if name.startswith("#"):
            resolved[name] = hashtag_channel_key(name)
            continue
        if local_by_name is None:
            local_by_name = {
                channel.name.casefold(): channel.key.upper()
                for channel in await ChannelRepository.get_all()
            }
        resolved[name] = local_by_name.get(name.casefold())
    return resolved


async def compare_channel_keys(settings: AppSettings | None = None) -> list[str]:
    settings = settings or await AppSettingsRepository.get()
    mapping = await resolve_channel_keys(normalize_channel_names(settings.live_feed_channels))
    return [key for key in mapping.values() if key]


# ─── Sync ──────────────────────────────────────────────────────────────────


async def _sync_channel(
    client: LiveFeedClient, channel_name: str, channel_key: str | None, now: int
) -> int:
    """Walk one channel's remote pages back to the lookback horizon."""
    horizon = now - LOOKBACK_SECONDS
    fetched = 0
    offset = 0
    for _page in range(MAX_PAGES_PER_CHANNEL):
        messages, total = await client.fetch_channel_messages(channel_name, offset=offset)
        if not messages:
            break
        rows = [
            row
            for row in (normalize_live_message(m, channel_name, channel_key) for m in messages)
            if row is not None
        ]
        await LiveFeedRepository.upsert_many(rows)
        fetched += len(rows)
        offset += len(messages)
        if len(messages) < PAGE_LIMIT or (total is not None and offset >= total):
            break
        # The list is newest-activity first; once a whole page is older than the
        # horizon nothing further down can be newer.
        if rows and max(row["last_seen"] for row in rows) < horizon:
            break
    return fetched


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
            names = normalize_channel_names(settings.live_feed_channels)
            mapping = await resolve_channel_keys(names)
            _state.unresolved_channels = [name for name, key in mapping.items() if not key]
            await LiveFeedRepository.assign_channel_keys(mapping)
            client = LiveFeedClient(settings.live_feed_url, settings.live_feed_region)
            fetched = 0
            for name in names:
                fetched += await _sync_channel(client, name, mapping[name], now)
            await LiveFeedRepository.prune_older_than(now - RETENTION_SECONDS)
            _state.last_fetched = fetched
            _state.last_success_at = int(time.time())
            _state.last_error = None
            logger.info(
                "Live feed sync: %d messages from %s (%s)",
                fetched,
                settings.live_feed_url,
                settings.live_feed_region or "all regions",
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
        wake.clear()
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


def notify_settings_changed() -> None:
    """Wake the loop so a settings change takes effect now, not next interval."""
    if _wake is not None:
        _wake.set()


def _wake_requested() -> bool:
    return _wake is not None and _wake.is_set()


# ─── Read side ─────────────────────────────────────────────────────────────


async def get_status(settings: AppSettings | None = None) -> dict[str, Any]:
    settings = settings or await AppSettingsRepository.get()
    return {
        "enabled": settings.live_feed_enabled,
        "url": settings.live_feed_url,
        "region": normalize_region(settings.live_feed_region),
        "channels": normalize_channel_names(settings.live_feed_channels),
        "poll_interval": settings.live_feed_poll_interval,
        "mirrored_messages": await LiveFeedRepository.count(),
        **asdict(_state),
    }


async def get_compare_stats(window: str, settings: AppSettings | None = None) -> dict | None:
    """The statistics-page section; ``None`` when there is nothing to compare yet."""
    settings = settings or await AppSettingsRepository.get()
    status = await get_status(settings)
    if not settings.live_feed_enabled and status["mirrored_messages"] == 0:
        return None
    now = int(time.time())
    keys = await compare_channel_keys(settings)
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
