"""The authoring surface bot code programs against.

Bot source (stored in the DB) imports a single object::

    from remoteterm import bot

    @bot.on_keyword("wx", "weather")
    async def weather(ctx, msg):
        await ctx.reply("...")

Decorators register handlers into the *active collector* — set by
``app.bots.runtime`` while it executes a bot's source. Importing ``remoteterm``
outside a bot load is fine; calling a decorator outside one is an error.

Handlers may be ``async def`` (run on the event loop) or plain ``def`` (run in
the bot thread pool). Both receive ``ctx`` first; message-ish handlers also
receive ``msg``.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Sentinel: distinguishes "region not passed" from an explicit None/"" (unscoped).
_UNSET = object()

# Default per-message byte budget for ctx.split_text / ctx.reply_split — a safe
# MeshCore RF payload size once sender-name framing is added (the same default
# the mailbox bot has always used).
DEFAULT_SPLIT_BYTES = 155

# Worst-case numbering prefix reserved when splitting compressed replies. The
# real "(i/n) " prefix has <= this many digits, so its compressed form is never
# larger, keeping each numbered part within budget.
_NUM_RESERVE_PREFIX = "(99/99) "


def _mcmp_wire_len(text: str, version: int) -> int:
    """UTF-8 byte length ``text`` occupies on the wire once MCMP-compressed."""
    from app.compression import encode_outbound

    # Timestamp only affects v3 (fixed 4 bytes), so a placeholder is fine for
    # sizing.
    return len(encode_outbound(text, version=version, timestamp=0).encode("utf-8"))


def split_text_compressed(text: str, max_bytes: int, version: int) -> list[str]:
    """Split ``text`` so each numbered part fits ``max_bytes`` *after* MCMP.

    Like :meth:`BotContext.split_text`, but measures the compressed wire size
    instead of the raw byte length, so a conversation with MCMP enabled packs
    more text per message (fewer, larger parts). Prefers word/newline
    boundaries; parts are numbered ``(i/n)`` when there is more than one.
    """
    text = (text or "").strip()
    if not text:
        return []
    if _mcmp_wire_len(text, version) <= max_bytes:
        return [text]

    parts: list[str] = []
    remaining = text
    while remaining:
        # Whole remainder (with numbering headroom) fits -> last part.
        if _mcmp_wire_len(_NUM_RESERVE_PREFIX + remaining, version) <= max_bytes:
            parts.append(remaining)
            break
        # Largest char-prefix whose numbered, compressed size fits. Compressed
        # length is non-decreasing in character count, so binary search is valid.
        lo, hi, best = 1, len(remaining), 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if _mcmp_wire_len(_NUM_RESERVE_PREFIX + remaining[:mid], version) <= max_bytes:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        chunk = remaining[:best]
        if best < len(remaining):
            cut = max(chunk.rfind(" "), chunk.rfind("\n"))
            if cut > best // 2:
                chunk = chunk[: cut + 1]
        chunk = chunk.rstrip() or remaining[:best]
        parts.append(chunk)
        remaining = remaining[len(chunk) :].lstrip()

    total = len(parts)
    return [f"({i}/{total}) {p}" for i, p in enumerate(parts, 1)]


@dataclass(frozen=True)
class KeywordTrigger:
    keywords: tuple[str, ...]  # empty tuple = keywords come from the bot's UI trigger list
    handler: Callable[..., Any]


@dataclass(frozen=True)
class MessageTrigger:
    """Catch-all: runs for every in-scope message (greeter-style bots)."""

    handler: Callable[..., Any]


@dataclass(frozen=True)
class CronTrigger:
    expression: str  # empty string = expressions come from the bot's UI trigger list
    handler: Callable[..., Any]


@dataclass(frozen=True)
class EventTrigger:
    event: str  # e.g. "new_contact"
    handler: Callable[..., Any]


@dataclass(frozen=True)
class WebhookTrigger:
    slug: str
    handler: Callable[..., Any]


@dataclass
class HandlerCollector:
    """Accumulates the triggers a bot's source declares while it executes."""

    keywords: list[KeywordTrigger] = field(default_factory=list)
    messages: list[MessageTrigger] = field(default_factory=list)
    crons: list[CronTrigger] = field(default_factory=list)
    events: list[EventTrigger] = field(default_factory=list)
    webhooks: list[WebhookTrigger] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.keywords or self.messages or self.crons or self.events or self.webhooks)


_collector_lock = threading.Lock()
_active_collector: HandlerCollector | None = None


class _BotDecorators:
    """The ``bot`` object bot code imports. Pure registration — no behavior."""

    @staticmethod
    def _collector() -> HandlerCollector:
        if _active_collector is None:
            raise RuntimeError(
                "bot decorators may only be used inside bot code loaded by RemoteTerm"
            )
        return _active_collector

    def on_keyword(self, *keywords: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Trigger on messages whose first word matches one of ``keywords``.

        With no arguments, the bot receives the keywords configured on its
        Triggers tab instead of hardcoding them.
        """
        normalized = tuple(k.strip().lower() for k in keywords if k and k.strip())

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._collector().keywords.append(KeywordTrigger(normalized, fn))
            return fn

        return decorator

    def on_message(self) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Trigger on every in-scope message (no keyword matching)."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._collector().messages.append(MessageTrigger(fn))
            return fn

        return decorator

    def on_cron(self, expression: str = "") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Trigger on a cron schedule (5-field crontab or @preset; dow 0=Monday).

        With no expression, the handler fires for schedules added on the bot's
        Triggers tab.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._collector().crons.append(CronTrigger(expression.strip(), fn))
            return fn

        return decorator

    def on_event(self, event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Trigger on a mesh event. Currently: ``new_contact``."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._collector().events.append(EventTrigger(event.strip(), fn))
            return fn

        return decorator

    def on_webhook(self, slug: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Trigger on ``POST /api/hooks/{slug}`` (token-gated via bot settings)."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._collector().webhooks.append(WebhookTrigger(slug.strip().lstrip("/"), fn))
            return fn

        return decorator


bot = _BotDecorators()


def collect_handlers(execute: Callable[[], None]) -> HandlerCollector:
    """Run ``execute`` (which exec()s bot source) with a fresh active collector."""
    global _active_collector
    with _collector_lock:
        collector = HandlerCollector()
        _active_collector = collector
        try:
            execute()
        finally:
            _active_collector = None
    return collector


@dataclass
class BotMessage:
    """The message a keyword/message handler is reacting to."""

    text: str
    keyword: str | None = None
    args: list[str] = field(default_factory=list)
    sender_name: str | None = None
    sender_key: str | None = None
    is_dm: bool = False
    channel_key: str | None = None
    channel_name: str | None = None
    sender_timestamp: int | None = None
    path: str | None = None
    path_bytes_per_hop: int | None = None
    region: str | None = None
    scoped: bool = False
    is_outgoing: bool = False

    @property
    def arg_text(self) -> str:
        return " ".join(self.args)


class BotHttp:
    """Small async HTTP helper handed to bots as ``ctx.http`` (httpx-backed)."""

    def __init__(self, timeout_seconds: float = 6.0) -> None:
        self._timeout = timeout_seconds

    async def get_json(self, url: str, *, headers: dict[str, str] | None = None) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()

    async def get_text(self, url: str, *, headers: dict[str, str] | None = None) -> str:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.text

    async def post_json(
        self,
        url: str,
        payload: Any,
        *,
        headers: dict[str, str] | None = None,
    ) -> Any:
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            return resp.json()


class BotContext:
    """Per-run context: sending, settings, persistent state, HTTP, i18n, logging.

    Sends go through the engine's shared TX spacing lock, exactly like the
    legacy fanout bots. In test runs sends are captured instead of transmitted.
    """

    def __init__(
        self,
        *,
        bot_id: str,
        bot_name: str,
        settings: dict[str, Any],
        state: dict[str, Any],
        origin_is_dm: bool = False,
        origin_sender_key: str | None = None,
        origin_channel_key: str | None = None,
        locale: str = "en",
        is_test: bool = False,
        log_fn: Callable[[str, str], None] | None = None,
        # Returns the stored Message (or None). Image sends need it to anchor
        # their AEIC session; text sends ignore it.
        send_fn: Callable[..., Awaitable[Any]] | None = None,
        translator: Any = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self.bot_id = bot_id
        self.bot_name = bot_name
        self.settings: dict[str, Any] = settings
        self.state: dict[str, Any] = state
        self.locale = locale
        self.is_test = is_test
        self.http = BotHttp()
        self.replies_sent = 0
        self.captured_sends: list[dict[str, Any]] = []
        self._origin_is_dm = origin_is_dm
        self._origin_sender_key = origin_sender_key
        self._origin_channel_key = origin_channel_key
        self._log_fn = log_fn
        self._send_fn = send_fn
        self._translator = translator
        self._loop = loop or asyncio.get_event_loop()
        self._state_dirty = False

    # -- persistence -----------------------------------------------------
    def mark_state_dirty(self) -> None:
        self._state_dirty = True

    @property
    def state_dirty(self) -> bool:
        # Conservative: assume any run that touched state mutated it. Bots that
        # only read pay one cheap JSON write; correctness beats cleverness here.
        return self._state_dirty or bool(self.state)

    # -- i18n --------------------------------------------------------------
    def t(self, key: str, **kwargs: Any) -> str:
        """Translate ``key`` in the run's locale; falls back to the key itself."""
        if self._translator is None:
            return key
        return self._translator.translate(key, self.locale, **kwargs)

    # -- logging -----------------------------------------------------------
    def log(self, message: str, level: str = "INFO") -> None:
        if self._log_fn is not None:
            self._log_fn(level, message)

    # -- sending -----------------------------------------------------------
    async def _dispatch_send(
        self,
        *,
        is_dm: bool,
        destination: str | None,
        channel_key: str | None,
        text: str,
        flood_scope_override: str | None,
    ) -> Any:
        """Dispatch one send. Returns the stored ``Message``, or None.

        Returning it matters only for image sends, which need the message row to
        anchor their AEIC session to; ``reply``/``send`` ignore it. None in test
        mode, where nothing is actually stored.
        """
        if not text or not text.strip():
            return None
        if self.is_test or self._send_fn is None:
            self.captured_sends.append(
                {
                    "is_dm": is_dm,
                    "destination": destination,
                    "channel_key": channel_key,
                    "text": text,
                    "region": flood_scope_override,
                }
            )
            self.replies_sent += 1
            return None
        sent = await self._send_fn(
            is_dm=is_dm,
            destination=destination,
            channel_key=channel_key,
            text=text,
            flood_scope_override=flood_scope_override,
        )
        self.replies_sent += 1
        return sent

    async def reply(self, text: str, *, region: Any = _UNSET) -> None:
        """Reply where the triggering message came from (DM sender or channel).

        ``region`` (channel replies only): omit for the channel default, pass a
        region name to scope this send, or ``None``/``""`` to force unscoped.
        """
        scope: str | None
        if region is _UNSET:
            scope = None
        elif region is None or region == "":
            scope = ""
        else:
            scope = str(region)
        if self._origin_is_dm:
            await self._dispatch_send(
                is_dm=True,
                destination=self._origin_sender_key,
                channel_key=None,
                text=text,
                flood_scope_override=None,
            )
        else:
            await self._dispatch_send(
                is_dm=False,
                destination=None,
                channel_key=self._origin_channel_key,
                text=text,
                flood_scope_override=scope,
            )

    async def _resolve_channel_key(self, channel: str) -> str:
        """Channel name (``#chan`` / ``Public``) or 32-hex key -> upper-case key."""
        from app.repository import ChannelRepository

        candidate = channel.strip()
        if len(candidate) == 32 and all(c in "0123456789abcdefABCDEF" for c in candidate):
            return candidate.upper()
        channels = await ChannelRepository.get_all()
        wanted = candidate.lstrip("#").lower()
        for ch in channels:
            if ch.name.lstrip("#").lower() == wanted:
                return ch.key
        raise ValueError(f"unknown channel {channel!r}")

    async def send(self, channel: str, text: str, *, region: Any = _UNSET) -> None:
        """Send to any channel by name (``#chan`` / ``Public``) or 32-hex key."""
        key = await self._resolve_channel_key(channel)
        scope: str | None
        if region is _UNSET:
            scope = None
        elif region is None or region == "":
            scope = ""
        else:
            scope = str(region)
        await self._dispatch_send(
            is_dm=False,
            destination=None,
            channel_key=key,
            text=text,
            flood_scope_override=scope,
        )

    async def send_dm(self, public_key: str, text: str) -> None:
        """Send a direct message to a contact by full public key."""
        await self._dispatch_send(
            is_dm=True,
            destination=public_key,
            channel_key=None,
            text=text,
            flood_scope_override=None,
        )

    # -- long replies --------------------------------------------------------
    def split_text(self, text: str, max_bytes: int = DEFAULT_SPLIT_BYTES) -> list[str]:
        """Split ``text`` into RF-sized parts, numbered ``(i/n)`` when split.

        Returns ``[text]`` unchanged when it already fits in ``max_bytes``
        UTF-8 bytes (the default is a safe MeshCore payload budget). Splits
        prefer word/newline boundaries and never cut inside a multi-byte
        character.
        """
        text = (text or "").strip()
        if not text:
            return []
        if len(text.encode("utf-8")) <= max_bytes:
            return [text]
        budget = max(1, max_bytes - 8)  # room for the "(i/n) " prefix
        parts: list[str] = []
        remaining = text
        while remaining:
            chunk = remaining.encode("utf-8")[:budget].decode("utf-8", "ignore")
            if not chunk:  # budget smaller than one multi-byte character
                chunk = remaining[0]
            elif len(remaining) > len(chunk):
                cut = max(chunk.rfind(" "), chunk.rfind("\n"))
                if cut > budget // 2:
                    chunk = chunk[: cut + 1].rstrip()
            parts.append(chunk)
            remaining = remaining[len(chunk) :].lstrip()
        total = len(parts)
        return [f"({i}/{total}) {p}" for i, p in enumerate(parts, 1)]

    async def reply_split(
        self, text: str, *, max_bytes: int = DEFAULT_SPLIT_BYTES, region: Any = _UNSET
    ) -> None:
        """Reply like :meth:`reply`, splitting long text into ``(i/n)`` parts.

        Text that fits in ``max_bytes`` is sent as-is in one message; longer
        text goes out as numbered parts, in order, each within the budget.

        When the reply target has MCMP compression enabled, parts are sized by
        their *compressed* wire length, so more text fits per message (fewer,
        larger parts) — otherwise by the raw byte length.
        """
        version = await self._origin_mcmp_version()
        parts = (
            split_text_compressed(text, max_bytes, version)
            if version is not None
            else self.split_text(text, max_bytes)
        )
        for part in parts:
            await self.reply(part, region=region)

    async def _origin_mcmp_version(self) -> int | None:
        """MCMP version for the reply target if compression is enabled, else None."""
        try:
            if self._origin_is_dm:
                if not self._origin_sender_key:
                    return None
                from app.repository import ContactRepository

                contact = await ContactRepository.get_by_key(self._origin_sender_key)
                if contact and contact.mcmp_enabled:
                    return contact.mcmp_version
            elif self._origin_channel_key:
                from app.repository import ChannelRepository

                channel = await ChannelRepository.get_by_key(self._origin_channel_key)
                if channel and channel.mcmp_enabled:
                    return channel.mcmp_version
        except Exception:
            return None
        return None

    # -- images --------------------------------------------------------------
    async def _dispatch_image(
        self,
        data: bytes,
        *,
        is_dm: bool,
        destination: str | None,
        channel_key: str | None,
        flood_scope_override: str | None,
        source_width: int | None,
        source_height: int | None,
    ) -> int:
        """Encode one image and send it. Returns the number of messages used.

        Everything about *how* it goes on air lives in
        :mod:`app.imaging.aeic.transport`, reached through
        ``aeic_service.send_image``. This method only supplies a destination and
        an emitter, so when the binary 0xAE1C transport lands the bot API does
        not change at all.
        """
        from app.imaging.aeic.prepare import AeicImagePrepareError
        from app.imaging.aeic.service import AeicUnavailable, aeic_service
        from app.imaging.aeic.transport import AeicTarget, resolve_message_budget

        if not data:
            raise ValueError("no image data")

        conversation_type = "PRIV" if is_dm else "CHAN"
        conversation_key = (destination if is_dm else channel_key) or ""
        if not conversation_key:
            raise ValueError("image send needs a destination")

        radio_manager = None
        if not self.is_test:
            from app.services.radio_runtime import radio_runtime

            radio_manager = radio_runtime

        async def emit_text(chunk: str):
            # Straight through the bot's own send path, so image chunks obey the
            # same TX spacing and test-capture rules as any reply. The returned
            # Message is what anchors the local AEIC session -- without it the
            # operator's own conversation shows raw aei1: text while the
            # recipient sees the photo.
            return await self._dispatch_send(
                is_dm=is_dm,
                destination=destination,
                channel_key=channel_key,
                text=chunk,
                flood_scope_override=flood_scope_override,
            )

        target = AeicTarget(
            conversation_type=conversation_type,
            conversation_key=conversation_key,
            emit_text=emit_text,
            message_budget=await resolve_message_budget(
                conversation_type, radio_manager=radio_manager
            ),
            radio_manager=radio_manager,
        )

        try:
            result, _bitstream, _metadata = await aeic_service.send_image(
                data,
                target,
                source_width=source_width,
                source_height=source_height,
            )
        except (AeicUnavailable, AeicImagePrepareError) as exc:
            # Surface the reason verbatim: it names the missing piece (the
            # onnxruntime extra, the undownloaded model, or Pillow) and the bot
            # author is the one who has to act on it.
            raise RuntimeError(f"AEIC image send failed: {exc}") from exc
        self.log(
            f"sent a {result.payload_bytes} B image as {result.chunk_count} "
            f"message(s) via {result.transport}"
        )
        return result.chunk_count

    async def reply_image(
        self,
        data: bytes,
        *,
        region: Any = _UNSET,
        source_width: int | None = None,
        source_height: int | None = None,
    ) -> int:
        """Reply with a photo where the triggering message came from.

        ``data`` is either an encoded image (JPEG/PNG/WebP -- anything Pillow can
        open, e.g. straight from ``ctx.http``) or exactly 786,432 bytes of
        512x512 packed RGB. It is stretched into a 512x512 square, encoded to
        ~150 bytes by the AEIC neural codec, and sent as one or two ordinary
        text messages. Returns how many messages that took.

        The recipient needs the AEIC model installed to see it, and gets a
        perceptually similar reconstruction rather than the original pixels.
        Requires the ``aeic`` extra and the downloaded model on THIS server;
        raises with the specific missing piece otherwise.
        """
        scope: str | None
        if region is _UNSET:
            scope = None
        elif region is None or region == "":
            scope = ""
        else:
            scope = str(region)
        if self._origin_is_dm:
            return await self._dispatch_image(
                data,
                is_dm=True,
                destination=self._origin_sender_key,
                channel_key=None,
                flood_scope_override=None,
                source_width=source_width,
                source_height=source_height,
            )
        return await self._dispatch_image(
            data,
            is_dm=False,
            destination=None,
            channel_key=self._origin_channel_key,
            flood_scope_override=scope,
            source_width=source_width,
            source_height=source_height,
        )

    async def send_image(
        self,
        channel: str,
        data: bytes,
        *,
        region: Any = _UNSET,
        source_width: int | None = None,
        source_height: int | None = None,
    ) -> int:
        """Send a photo to any channel by name (``#chan`` / ``Public``) or key.

        See :meth:`reply_image` for what ``data`` accepts and what the recipient
        needs.
        """
        key = await self._resolve_channel_key(channel)
        scope: str | None
        if region is _UNSET:
            scope = None
        elif region is None or region == "":
            scope = ""
        else:
            scope = str(region)
        return await self._dispatch_image(
            data,
            is_dm=False,
            destination=None,
            channel_key=key,
            flood_scope_override=scope,
            source_width=source_width,
            source_height=source_height,
        )

    async def send_dm_image(
        self,
        public_key: str,
        data: bytes,
        *,
        source_width: int | None = None,
        source_height: int | None = None,
    ) -> int:
        """Send a photo to a contact by full public key.

        See :meth:`reply_image` for what ``data`` accepts and what the recipient
        needs.
        """
        return await self._dispatch_image(
            data,
            is_dm=True,
            destination=public_key,
            channel_key=None,
            flood_scope_override=None,
            source_width=source_width,
            source_height=source_height,
        )

    # -- geocoding -----------------------------------------------------------
    async def geocode(self, query: str) -> dict[str, Any] | None:
        """Resolve a place name / postal code via Nominatim. Cached in-process."""
        from app.bots.geocode import geocode_query

        return await geocode_query(query)

    # -- introspection ---------------------------------------------------------
    async def mesh_stats(self) -> dict[str, int]:
        """Small mesh summary: total_contacts, total_repeaters, contacts_24h,
        repeaters_24h, new_contacts_7d, messages_24h."""
        from app.bots.placeholders import gather_mesh_stats

        return await gather_mesh_stats()

    def get_enabled_bots(self) -> list[dict[str, Any]]:
        """Metadata for every enabled bot: name, category, description, keywords."""
        from app.bots.engine import bot_engine

        out: list[dict[str, Any]] = []
        for loaded in bot_engine.bots.values():
            record = loaded.record
            if not record.enabled or loaded.code is None:
                continue
            keywords: list[str] = []
            for kws, _handler in loaded.keyword_map:
                keywords.extend(kws)
            out.append(
                {
                    "name": record.name,
                    "category": record.category,
                    "description": record.description,
                    "keywords": keywords,
                }
            )
        out.sort(key=lambda b: (b["category"], b["name"]))
        return out

    # -- sync bridges (for plain ``def`` handlers running in the thread pool) --
    def reply_sync(self, text: str, *, region: Any = _UNSET) -> None:
        asyncio.run_coroutine_threadsafe(self.reply(text, region=region), self._loop).result(
            timeout=30
        )

    def send_sync(self, channel: str, text: str, *, region: Any = _UNSET) -> None:
        asyncio.run_coroutine_threadsafe(
            self.send(channel, text, region=region), self._loop
        ).result(timeout=30)

    def send_dm_sync(self, public_key: str, text: str) -> None:
        asyncio.run_coroutine_threadsafe(self.send_dm(public_key, text), self._loop).result(
            timeout=30
        )
