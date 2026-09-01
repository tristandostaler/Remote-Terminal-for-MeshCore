"""TCP server that impersonates a MeshCore companion radio for other apps.

One physical radio, many apps. RemoteTerm owns the transport; this server
lets the MeshCore mobile app, meshcore-cli, meshcore.js or Home Assistant
connect to RemoteTerm over TCP (the companion "WiFi" protocol) and:

* **answers locally** whatever RemoteTerm already knows -- identity
  (``SELF_INFO``/``DEVICE_INFO``), the whole contact list (the server keeps
  far more contacts than the radio can), channels, clock, battery and the
  inbound message queue -- so the chattiest app traffic never touches the radio;
* **caches** read-only device queries for a short TTL so several apps polling
  the same thing cost one radio round trip;
* **forwards** the rest (sends, logins, telemetry requests, config writes)
  under the shared radio operation lock, so app traffic never interleaves
  with RemoteTerm's own command sequences;
* **relays** the radio's push frames (ACKs, telemetry, login results, RF
  log) to every connected app, and feeds each app its own copy of every
  message RemoteTerm ingests.

Sends from apps go through the same service layer as the web UI, so they are
stored, broadcast, ACK-tracked and retried exactly like a message typed in
the browser. See ``AGENTS_virtual_node.md`` for the full design.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from meshcore import EventType
from meshcore.packets import CommandType, PacketType

from app.config import settings
from app.models import ContactUpsert
from app.repository import ChannelRepository, ContactRepository
from app.services import dm_ack_tracker
from app.virtual_node import protocol
from app.virtual_node.protocol import ErrorCode

logger = logging.getLogger(__name__)

# How long an app waits for the radio to answer a forwarded command.
FORWARD_TIMEOUT_SECONDS = 12.0
# Upper bound on waiting for the shared radio lock plus the command itself.
# Post-connect setup can hold the lock for a while; an app should get an
# error rather than hang forever.
FORWARD_TOTAL_TIMEOUT_SECONDS = 45.0
# Read-only device queries are answered from this cache while fresh.
RESPONSE_CACHE_TTL_SECONDS = 30.0
# Battery is answered from the 60 s stats sampler while its sample is this fresh.
BATTERY_CACHE_MAX_AGE_SECONDS = 180.0
# Per-client inbound message backlog. A client that never drains loses the oldest.
MAX_QUEUED_MESSAGES = 500
# 1-byte channel index on the wire.
MAX_VIRTUAL_CHANNEL_SLOTS = 255
# Hard floor for the slot count we advertise so apps keep probing new slots.
MIN_ADVERTISED_CHANNEL_SLOTS = 40
# App-side DM retries reuse the same (dest, timestamp, text); remember what we
# already sent for them so a retry does not create a second message row.
RECENT_DM_SEND_TTL_SECONDS = 120.0

_CMD = CommandType
_RESP = PacketType

OK = _RESP.OK.value
ERR = _RESP.ERROR.value
MSG_SENT = _RESP.MSG_SENT.value

# Commands that put something on the air.
TRANSMIT_COMMANDS = {
    _CMD.SEND_TXT_MSG.value,
    _CMD.SEND_CHANNEL_TXT_MSG.value,
    _CMD.SEND_SELF_ADVERT.value,
    _CMD.SHARE_CONTACT.value,
    _CMD.SEND_RAW_DATA.value,
    _CMD.SEND_LOGIN.value,
    _CMD.SEND_STATUS_REQ.value,
    _CMD.LOGOUT.value,
    _CMD.SEND_TRACE_PATH.value,
    _CMD.SEND_TELEMETRY_REQ.value,
    _CMD.BINARY_REQ.value,
    _CMD.PATH_DISCOVERY.value,
    _CMD.SEND_CONTROL_DATA.value,
    _CMD.SEND_ANON_REQ.value,
}
# Commands that change the radio's configuration or identity.
ADMIN_COMMANDS = {
    _CMD.SET_ADVERT_NAME.value,
    _CMD.SET_RADIO_PARAMS.value,
    _CMD.SET_RADIO_TX_POWER.value,
    _CMD.SET_ADVERT_LATLON.value,
    _CMD.IMPORT_CONTACT.value,
    _CMD.SET_TUNING_PARAMS.value,
    _CMD.SIGN_START.value,
    _CMD.SIGN_DATA.value,
    _CMD.SIGN_FINISH.value,
    _CMD.SET_DEVICE_PIN.value,
    _CMD.SET_OTHER_PARAMS.value,
    _CMD.SET_CUSTOM_VAR.value,
    _CMD.SET_FLOOD_SCOPE.value,
    _CMD.SET_AUTOADD_CONFIG.value,
    _CMD.SET_PATH_HASH_MODE.value,
    _CMD.SET_DEFAULT_FLOOD_SCOPE.value,
}
# Config writes after which our cached identity frames are stale.
IDENTITY_CHANGING_COMMANDS = {
    _CMD.SET_ADVERT_NAME.value,
    _CMD.SET_RADIO_PARAMS.value,
    _CMD.SET_RADIO_TX_POWER.value,
    _CMD.SET_ADVERT_LATLON.value,
    _CMD.SET_OTHER_PARAMS.value,
    _CMD.SET_PATH_HASH_MODE.value,
}
# Commands that mutate RemoteTerm's own contact/channel store.
LOCAL_WRITE_COMMANDS = {
    _CMD.ADD_UPDATE_CONTACT.value,
    _CMD.RESET_PATH.value,
    _CMD.REMOVE_CONTACT.value,
    _CMD.SET_CHANNEL.value,
}
# Never proxied: they would take the radio away from RemoteTerm itself.
REFUSED_COMMANDS = {
    _CMD.REBOOT.value,
    _CMD.IMPORT_PRIVATE_KEY.value,
    _CMD.FACTORY_RESET.value,
}
# Read-only device queries worth caching across clients.
CACHED_QUERY_COMMANDS = {
    _CMD.GET_CUSTOM_VARS.value,
    _CMD.GET_ADVERT_PATH.value,
    _CMD.GET_TUNING_PARAMS.value,
    _CMD.GET_STATS.value,
    _CMD.GET_AUTOADD_CONFIG.value,
    _CMD.GET_ALLOWED_REPEAT_FREQ.value,
    _CMD.GET_DEFAULT_FLOOD_SCOPE.value,
    _CMD.GET_BATT_AND_STORAGE.value,
}
# Commands addressed to a contact by its full 32-byte key right after the code.
# RemoteTerm offloads contacts from the radio, so the contact is re-staged on
# the radio before such a command is forwarded.
CONTACT_ADDRESSED_COMMANDS = {
    _CMD.SHARE_CONTACT.value,
    _CMD.SEND_LOGIN.value,
    _CMD.SEND_STATUS_REQ.value,
    _CMD.HAS_CONNECTION.value,
    _CMD.LOGOUT.value,
    _CMD.BINARY_REQ.value,
    _CMD.PATH_DISCOVERY.value,
    _CMD.SEND_ANON_REQ.value,
    _CMD.EXPORT_CONTACT.value,
}

# Which response codes end a forwarded command. Anything else that arrives in
# the meantime is a push (>= 0x80) or belongs to RemoteTerm's auto-fetch.
_EXPECTED_RESPONSES: dict[int, frozenset[int]] = {
    _CMD.EXPORT_CONTACT.value: frozenset({_RESP.CONTACT_URI.value, ERR}),
    _CMD.EXPORT_PRIVATE_KEY.value: frozenset({_RESP.PRIVATE_KEY.value, _RESP.DISABLED.value, ERR}),
    _CMD.GET_BATT_AND_STORAGE.value: frozenset({_RESP.BATTERY.value, ERR}),
    _CMD.DEVICE_QEURY.value: frozenset({_RESP.DEVICE_INFO.value, ERR}),
    _CMD.SIGN_START.value: frozenset({_RESP.SIGN_START.value, ERR}),
    _CMD.SIGN_FINISH.value: frozenset({_RESP.SIGNATURE.value, ERR}),
    _CMD.GET_CUSTOM_VARS.value: frozenset({_RESP.CUSTOM_VARS.value, ERR}),
    _CMD.GET_ADVERT_PATH.value: frozenset({_RESP.ADVERT_PATH.value, ERR}),
    _CMD.GET_TUNING_PARAMS.value: frozenset({_RESP.TUNING_PARAMS.value, ERR}),
    _CMD.GET_STATS.value: frozenset({_RESP.STATS.value, ERR}),
    _CMD.GET_AUTOADD_CONFIG.value: frozenset({_RESP.AUTOADD_CONFIG.value, ERR}),
    _CMD.GET_ALLOWED_REPEAT_FREQ.value: frozenset({_RESP.ALLOWED_REPEAT_FREQ.value, ERR}),
    _CMD.GET_DEFAULT_FLOOD_SCOPE.value: frozenset({_RESP.DEFAULT_FLOOD_SCOPE.value, ERR}),
}
for _code in (
    _CMD.SEND_RAW_DATA.value,
    _CMD.SEND_LOGIN.value,
    _CMD.SEND_STATUS_REQ.value,
    _CMD.SEND_TRACE_PATH.value,
    _CMD.SEND_TELEMETRY_REQ.value,
    _CMD.BINARY_REQ.value,
    _CMD.PATH_DISCOVERY.value,
    _CMD.SEND_ANON_REQ.value,
):
    _EXPECTED_RESPONSES[_code] = frozenset({MSG_SENT, ERR})

# Frames that are never the answer to a forwarded command: the radio's message
# queue (drained by RemoteTerm's auto-fetch concurrently) and contact-list
# streaming, which only RemoteTerm's own sync produces.
_NEVER_A_FORWARD_RESPONSE = frozenset(
    {
        _RESP.CONTACT_START.value,
        _RESP.CONTACT.value,
        _RESP.CONTACT_END.value,
        _RESP.CONTACT_MSG_RECV.value,
        _RESP.CHANNEL_MSG_RECV.value,
        _RESP.NO_MORE_MSGS.value,
        _RESP.CONTACT_MSG_RECV_V3.value,
        _RESP.CHANNEL_MSG_RECV_V3.value,
        protocol.RESP_CODE_CHANNEL_DATA_RECV,
    }
)

# Push frames we do NOT relay verbatim: adverts and message-waiting are
# synthesized from RemoteTerm's own pipeline instead (see on_app_event), so
# clients do not get them twice.
_UNRELAYED_PUSH_CODES = frozenset(
    {
        protocol.PUSH_CODE_ADVERT,
        protocol.PUSH_CODE_NEW_ADVERT,
        protocol.PUSH_CODE_MSG_WAITING,
    }
)


class VirtualNodeError(Exception):
    """A command could not be served; carries the ``ERR_CODE`` to answer with."""

    def __init__(self, code: ErrorCode, detail: str = "") -> None:
        super().__init__(detail or code.name)
        self.code = code


@dataclass(slots=True, eq=False)
class ClientSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    peer: str
    parser: protocol.HostFrameParser = field(default_factory=protocol.HostFrameParser)
    inbox: deque[bytes] = field(default_factory=lambda: deque(maxlen=MAX_QUEUED_MESSAGES))
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    connected_at: float = field(default_factory=time.time)
    commands_served: int = 0
    app_started: bool = False

    async def send(self, payload: bytes) -> None:
        async with self.write_lock:
            self.writer.write(protocol.frame_to_host(payload))
            await asyncio.wait_for(self.writer.drain(), timeout=5.0)


@dataclass(slots=True)
class _PendingForward:
    expected: frozenset[int] | None
    future: asyncio.Future


class VirtualNodeServer:
    """The virtual companion node. One instance per process (``virtual_node``)."""

    def __init__(self, *, radio: Any = None) -> None:
        self._radio_override = radio
        self._server: asyncio.base_events.Server | None = None
        self._clients: set[ClientSession] = set()
        self._client_tasks: set[asyncio.Task] = set()
        self._background: set[asyncio.Task] = set()
        self._pending_forward: _PendingForward | None = None
        self._response_cache: dict[bytes, tuple[float, bytes]] = {}
        self._self_info_frame: bytes | None = None
        self._device_info_frame: bytes | None = None
        self._channel_slots: list[str | None] = []
        self._channel_slots_lock = asyncio.Lock()
        # (prefix, timestamp, text) -> (message_id, expires_at, is_flood)
        self._recent_dm_sends: dict[tuple[str, int, str], tuple[int, float, bool]] = {}
        self.host = settings.virtual_node_host
        self.port = settings.virtual_node_port
        self.read_only = settings.virtual_node_read_only
        self.forwarded_commands = 0
        self.local_commands = 0
        self.cached_commands = 0

    # ------------------------------------------------------------------ lifecycle

    @property
    def is_listening(self) -> bool:
        return self._server is not None and self._server.is_serving()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    def status(self) -> dict[str, Any]:
        return {
            "enabled": settings.virtual_node_enabled,
            "listening": self.is_listening,
            "host": self.host,
            "port": self.port,
            "read_only": self.read_only,
            "client_count": self.client_count,
            "clients": [
                {
                    "peer": c.peer,
                    "connected_at": int(c.connected_at),
                    "commands": c.commands_served,
                    "queued_messages": len(c.inbox),
                }
                for c in self._clients
            ],
            "local_commands": self.local_commands,
            "cached_commands": self.cached_commands,
            "forwarded_commands": self.forwarded_commands,
        }

    async def start(self, *, host: str | None = None, port: int | None = None) -> None:
        if self._server is not None:
            return
        if host is not None:
            self.host = host
        if port is not None:
            self.port = port
        self._server = await asyncio.start_server(self._handle_client, self.host, self.port)
        bound = self._server.sockets[0].getsockname() if self._server.sockets else None
        if bound:
            self.port = bound[1]
        logger.info(
            "Virtual MeshCore node listening on %s:%d%s",
            self.host,
            self.port,
            " (read-only)" if self.read_only else "",
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        for task in list(self._client_tasks):
            task.cancel()
        if self._client_tasks:
            await asyncio.gather(*self._client_tasks, return_exceptions=True)
        for task in list(self._background):
            task.cancel()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
        self._clients.clear()
        logger.info("Virtual MeshCore node stopped")

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    def _radio(self) -> Any:
        if self._radio_override is not None:
            return self._radio_override
        from app.services.radio_runtime import radio_runtime

        return radio_runtime

    # ------------------------------------------------------------------ client loop

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        session = ClientSession(reader=reader, writer=writer, peer=peer)
        self._clients.add(session)
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        logger.info("Virtual node client connected from %s (%d total)", peer, len(self._clients))
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                for command in session.parser.feed(data):
                    await self._serve(session, command)
        except (asyncio.CancelledError, ConnectionError, TimeoutError):
            pass
        except Exception:
            logger.exception("Virtual node client %s failed", peer)
        finally:
            self._clients.discard(session)
            if task is not None:
                self._client_tasks.discard(task)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info(
                "Virtual node client %s disconnected (%d remaining)", peer, len(self._clients)
            )

    async def _serve(self, session: ClientSession, command: bytes) -> None:
        if not command:
            return
        code = command[0]
        session.commands_served += 1
        try:
            responses = await self.handle_command(session, code, command[1:])
        except VirtualNodeError as exc:
            logger.info("Virtual node refused command %d from %s: %s", code, session.peer, exc)
            responses = [protocol.encode_error(exc.code)]
        except Exception:
            logger.exception("Virtual node failed to serve command %d from %s", code, session.peer)
            responses = [protocol.encode_error(ErrorCode.BAD_STATE)]
        for frame in responses:
            await session.send(frame)

    # ------------------------------------------------------------------ dispatch

    async def handle_command(
        self, session: ClientSession, code: int, payload: bytes
    ) -> list[bytes]:
        """Answer one host command; returns the frames to write back, in order."""
        if code in REFUSED_COMMANDS:
            raise VirtualNodeError(ErrorCode.UNSUPPORTED_CMD, "refused by virtual node")
        if self.read_only and (
            code in TRANSMIT_COMMANDS or code in ADMIN_COMMANDS or code in LOCAL_WRITE_COMMANDS
        ):
            raise VirtualNodeError(ErrorCode.UNSUPPORTED_CMD, "virtual node is read-only")

        if code == _CMD.APP_START.value:
            session.app_started = True
            self.local_commands += 1
            return [self._self_info()]
        if code == _CMD.DEVICE_QEURY.value:
            return [await self._device_info()]
        if code == _CMD.GET_CONTACTS.value:
            self.local_commands += 1
            return await self._contacts(protocol.parse_get_contacts_since(payload))
        if code == _CMD.GET_CONTACT_BY_KEY.value:
            self.local_commands += 1
            return [await self._contact_by_key(payload)]
        if code == _CMD.GET_DEVICE_TIME.value:
            self.local_commands += 1
            return [protocol.encode_current_time(int(time.time()))]
        if code == _CMD.SET_DEVICE_TIME.value:
            # RemoteTerm keeps the radio clock synced to the server; an app's
            # idea of the time is acknowledged but not applied.
            self.local_commands += 1
            return [protocol.encode_ok()]
        if code == _CMD.SYNC_NEXT_MESSAGE.value:
            self.local_commands += 1
            if session.inbox:
                return [session.inbox.popleft()]
            return [protocol.encode_no_more_messages()]
        if code == _CMD.GET_CHANNEL.value:
            self.local_commands += 1
            return [await self._channel_info(payload)]
        if code == _CMD.SET_CHANNEL.value:
            self.local_commands += 1
            return [await self._set_channel(payload)]
        if code == _CMD.ADD_UPDATE_CONTACT.value:
            self.local_commands += 1
            return [await self._add_update_contact(payload)]
        if code == _CMD.RESET_PATH.value:
            self.local_commands += 1
            return [await self._reset_path(payload)]
        if code == _CMD.REMOVE_CONTACT.value:
            self.local_commands += 1
            return [await self._remove_contact(payload)]
        if code == _CMD.SEND_TXT_MSG.value:
            return [await self._send_text_message(payload)]
        if code == _CMD.SEND_CHANNEL_TXT_MSG.value:
            return [await self._send_channel_message(payload)]
        if code == _CMD.GET_BATT_AND_STORAGE.value:
            cached = self._battery_from_stats()
            if cached is not None:
                self.local_commands += 1
                return [cached]
        if code == _CMD.EXPORT_PRIVATE_KEY.value and not settings.enable_local_private_key_export:
            self.local_commands += 1
            return [protocol.encode_disabled()]

        # Everything else reaches the radio.
        command = bytes([code]) + payload
        if code in CACHED_QUERY_COMMANDS:
            cached = self._cached_response(command)
            if cached is not None:
                self.cached_commands += 1
                return [cached]
        response = await self._forward(
            command,
            stage_contact_key=(
                protocol.parse_public_key_arg(payload)
                if code in CONTACT_ADDRESSED_COMMANDS
                else None
            ),
            refresh_identity=code in IDENTITY_CHANGING_COMMANDS,
            cacheable=code in CACHED_QUERY_COMMANDS,
        )
        if code in ADMIN_COMMANDS:
            self._response_cache.clear()
        return [response]

    # ------------------------------------------------------------------ identity

    def _self_info(self) -> bytes:
        if self._self_info_frame is not None:
            return self._self_info_frame
        mc = getattr(self._radio(), "meshcore", None)
        info = getattr(mc, "self_info", None) if mc is not None else None
        if not info:
            raise VirtualNodeError(ErrorCode.BAD_STATE, "radio identity not known yet")
        return protocol.encode_self_info(info)

    async def _device_info(self) -> bytes:
        if self._device_info_frame is None:
            response = await self._forward(bytes([_CMD.DEVICE_QEURY.value, 0x03]))
            if response[0] != _RESP.DEVICE_INFO.value:
                return response
            self._device_info_frame = response
        else:
            self.local_commands += 1
        slots = await self._ensure_channel_slots()
        return protocol.rewrite_device_info_max_channels(
            self._device_info_frame, self._advertised_channel_slots(len(slots))
        )

    def _advertised_channel_slots(self, used: int) -> int:
        radio_max = getattr(self._radio(), "max_channels", None)
        radio_max = radio_max if isinstance(radio_max, int) and radio_max > 0 else 0
        return min(MAX_VIRTUAL_CHANNEL_SLOTS, max(MIN_ADVERTISED_CHANNEL_SLOTS, radio_max, used))

    def _battery_from_stats(self) -> bytes | None:
        from app.services.radio_stats import get_latest_radio_stats

        stats = get_latest_radio_stats()
        sampled_at = stats.get("timestamp")
        battery_mv = stats.get("battery_mv")
        if not isinstance(sampled_at, int | float) or not isinstance(battery_mv, int):
            return None
        if time.time() - sampled_at > BATTERY_CACHE_MAX_AGE_SECONDS:
            return None
        return protocol.encode_battery(battery_mv)

    # ------------------------------------------------------------------ contacts

    async def _contacts(self, since: int) -> list[bytes]:
        contacts = [
            c
            for c in await ContactRepository.get_all(limit=100_000)
            if protocol.is_full_public_key(c.public_key)
        ]
        frames: list[bytes] = []
        newest = 0
        for contact in contacts:
            lastmod = protocol.contact_lastmod(contact)
            newest = max(newest, lastmod)
            if since and lastmod <= since:
                continue
            frames.append(protocol.encode_contact(contact, lastmod=lastmod))
        return [
            protocol.encode_contact_start(len(frames)),
            *frames,
            protocol.encode_contact_end(newest),
        ]

    async def _contact_by_key(self, payload: bytes) -> bytes:
        key = protocol.parse_public_key_arg(payload)
        contact = await ContactRepository.get_by_key(key) if key else None
        if contact is None or not protocol.is_full_public_key(contact.public_key):
            return protocol.encode_error(ErrorCode.NOT_FOUND)
        return protocol.encode_contact(contact)

    async def _add_update_contact(self, payload: bytes) -> bytes:
        update = protocol.parse_contact_update(payload)
        if update is None:
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        from app.websocket import broadcast_event

        existing = await ContactRepository.get_by_key(update.public_key)
        upsert = ContactUpsert.from_radio_dict(update.public_key, update.to_radio_dict())
        if existing is not None and not update.adv_name:
            # An app edit with a blank name must not erase what we know.
            upsert.name = existing.name
        await ContactRepository.upsert(upsert)
        favorite = bool(update.flags & 0x01)
        if existing is None or existing.favorite != favorite:
            try:
                await ContactRepository.set_favorite(update.public_key, favorite)
            except Exception:
                logger.debug("Could not mirror favorite flag for %s", update.public_key[:12])
        stored = await ContactRepository.get_by_key(update.public_key)
        if stored is not None:
            broadcast_event("contact", stored.model_dump())
        logger.info(
            "Virtual node %s contact %s (%s)",
            "updated" if existing else "added",
            update.public_key[:12],
            update.adv_name or "unnamed",
        )
        return protocol.encode_ok()

    async def _reset_path(self, payload: bytes) -> bytes:
        key = protocol.parse_public_key_arg(payload)
        contact = await ContactRepository.get_by_key(key) if key else None
        if contact is None:
            return protocol.encode_error(ErrorCode.NOT_FOUND)
        await ContactRepository.update_direct_path(
            contact.public_key, "", -1, -1, updated_at=int(time.time())
        )
        # Best effort on the radio too, so a DM RemoteTerm sends next does not
        # stage the route the app just cleared from a stale library cache.
        try:
            async with self._radio().radio_operation("virtual_node:reset_path") as mc:
                await mc.commands.reset_path(bytes.fromhex(contact.public_key))
        except Exception as exc:
            logger.debug("Radio reset_path skipped for %s: %s", contact.public_key[:12], exc)
        from app.websocket import broadcast_event

        stored = await ContactRepository.get_by_key(contact.public_key)
        if stored is not None:
            broadcast_event("contact", stored.model_dump())
        return protocol.encode_ok()

    async def _remove_contact(self, payload: bytes) -> bytes:
        key = protocol.parse_public_key_arg(payload)
        contact = await ContactRepository.get_by_key(key) if key else None
        if contact is None:
            return protocol.encode_error(ErrorCode.NOT_FOUND)
        try:
            async with self._radio().radio_operation("virtual_node:remove_contact") as mc:
                if mc.get_contact_by_key_prefix(contact.public_key[:12]):
                    await mc.commands.remove_contact(bytes.fromhex(contact.public_key))
        except Exception as exc:
            logger.debug("Radio remove_contact skipped for %s: %s", contact.public_key[:12], exc)
        await ContactRepository.delete(contact.public_key)
        from app.websocket import broadcast_event

        broadcast_event("contact_deleted", {"public_key": contact.public_key})
        logger.info("Virtual node deleted contact %s", contact.public_key[:12])
        return protocol.encode_ok()

    # ------------------------------------------------------------------ channels

    async def _ensure_channel_slots(self) -> list[str | None]:
        """Reconcile the virtual slot table with the channel store.

        Slots are assigned on first sight and kept for the life of the process
        so an app's channel index stays meaningful between its requests; a
        deleted channel leaves a blank slot, reused only once the table is full.
        """
        async with self._channel_slots_lock:
            channels = await ChannelRepository.get_all()
            known = {c.key.upper() for c in channels}
            for index, key in enumerate(self._channel_slots):
                if key is not None and key not in known:
                    self._channel_slots[index] = None
            present = {k for k in self._channel_slots if k is not None}
            for key in sorted(known - present):
                if len(self._channel_slots) < MAX_VIRTUAL_CHANNEL_SLOTS:
                    self._channel_slots.append(key)
                elif None in self._channel_slots:
                    self._channel_slots[self._channel_slots.index(None)] = key
                else:
                    logger.warning("Virtual node channel table full; %s not exposed", key)
            return list(self._channel_slots)

    async def _slot_for_channel(self, key: str) -> int | None:
        key = key.upper()
        if key not in self._channel_slots:
            await self._ensure_channel_slots()
        try:
            return self._channel_slots.index(key)
        except ValueError:
            return None

    async def _channel_info(self, payload: bytes) -> bytes:
        if not payload:
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        index = payload[0]
        slots = await self._ensure_channel_slots()
        if index >= self._advertised_channel_slots(len(slots)):
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        key = slots[index] if index < len(slots) else None
        channel = await ChannelRepository.get_by_key(key) if key else None
        return protocol.encode_channel_info(index, channel)

    async def _set_channel(self, payload: bytes) -> bytes:
        update = protocol.parse_channel_update(payload)
        if update is None:
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        slots = await self._ensure_channel_slots()
        if update.index >= self._advertised_channel_slots(len(slots)):
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        from app.websocket import broadcast_event

        async with self._channel_slots_lock:
            while len(self._channel_slots) <= update.index:
                self._channel_slots.append(None)
            if update.is_clear:
                # Clearing a slot in the app only unbinds it here. The channel
                # and its history stay on the server: deleting server data from
                # a proxy client is not something a blank frame should do.
                previous = self._channel_slots[update.index]
                self._channel_slots[update.index] = None
                if previous:
                    logger.info(
                        "Virtual node unbound channel %s from slot %d", previous, update.index
                    )
                return protocol.encode_ok()
            if update.key in self._channel_slots and self._channel_slots.index(update.key) != (
                update.index
            ):
                self._channel_slots[self._channel_slots.index(update.key)] = None
            self._channel_slots[update.index] = update.key

        existing = await ChannelRepository.get_by_key(update.key)
        name = update.name or (existing.name if existing else update.key[:8])
        await ChannelRepository.upsert(
            update.key,
            name,
            is_hashtag=name.startswith("#"),
            on_radio=existing.on_radio if existing else False,
        )
        stored = await ChannelRepository.get_by_key(update.key)
        if stored is not None:
            broadcast_event("channel", stored.model_dump())
        logger.info("Virtual node set channel slot %d to %s", update.index, name)
        return protocol.encode_ok()

    # ------------------------------------------------------------------ sends

    async def _send_text_message(self, payload: bytes) -> bytes:
        from fastapi import HTTPException

        from app.services.message_send import send_direct_message_to_contact
        from app.websocket import broadcast_event

        outgoing = protocol.parse_send_txt_msg(payload)
        if outgoing is None:
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)

        if outgoing.txt_type != 0:
            # CLI commands to repeaters (txt_type 1) and signed text (2) are not
            # chat: forward them as-is after staging the contact, and relay the
            # reply through the client's inbox (see on_radio_frame).
            contact = await self._contact_for_prefix(outgoing.pubkey_prefix)
            return await self._forward(
                bytes([_CMD.SEND_TXT_MSG.value]) + payload,
                stage_contact_key=contact.public_key if contact else None,
                expected=frozenset({MSG_SENT, ERR}),
            )

        self._expire_recent_dm_sends()
        retry_key = (outgoing.pubkey_prefix, outgoing.timestamp, outgoing.text)
        remembered = self._recent_dm_sends.get(retry_key)
        if outgoing.attempt > 0 and remembered is not None:
            # The service layer already runs its own retry schedule for this
            # message; answer the app's retry with the ACK code still pending.
            self.local_commands += 1
            return self._msg_sent_for_message(remembered[0], is_flood=remembered[2])

        contact = await self._contact_for_prefix(outgoing.pubkey_prefix)
        if contact is None:
            raise VirtualNodeError(ErrorCode.NOT_FOUND, "unknown destination prefix")

        self.forwarded_commands += 1
        try:
            message = await send_direct_message_to_contact(
                contact=contact,
                text=outgoing.text,
                radio_manager=self._radio(),
                broadcast_fn=broadcast_event,
                track_pending_ack_fn=dm_ack_tracker.track_pending_ack,
                now_fn=time.time,
            )
        except HTTPException as exc:
            logger.info("Virtual node DM to %s failed: %s", contact.public_key[:12], exc.detail)
            return protocol.encode_error(
                ErrorCode.BAD_STATE if exc.status_code in (408, 423) else ErrorCode.ILLEGAL_ARG
            )
        is_flood = contact.effective_route_source == "flood"
        self._recent_dm_sends[retry_key] = (
            message.id,
            time.time() + RECENT_DM_SEND_TTL_SECONDS,
            is_flood,
        )
        return self._msg_sent_for_message(message.id, is_flood=is_flood)

    def _msg_sent_for_message(self, message_id: int, *, is_flood: bool) -> bytes:
        code, timeout_ms = _pending_ack_for_message(message_id)
        return protocol.encode_msg_sent(
            is_flood=is_flood,
            expected_ack=bytes.fromhex(code) if code else b"",
            suggested_timeout_ms=timeout_ms,
        )

    def _expire_recent_dm_sends(self) -> None:
        now = time.time()
        for key in [k for k, (_mid, expires, _f) in self._recent_dm_sends.items() if expires < now]:
            del self._recent_dm_sends[key]

    async def _contact_for_prefix(self, prefix_hex: str):
        try:
            contact = await ContactRepository.get_by_key_prefix(prefix_hex)
        except Exception as exc:
            logger.info("Virtual node could not resolve prefix %s: %s", prefix_hex, exc)
            return None
        if contact is None or not protocol.is_full_public_key(contact.public_key):
            return None
        return contact

    async def _send_channel_message(self, payload: bytes) -> bytes:
        from fastapi import HTTPException

        from app.services.message_send import send_channel_message_to_channel
        from app.websocket import broadcast_error, broadcast_event

        outgoing = protocol.parse_send_channel_txt_msg(payload)
        if outgoing is None:
            return protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        slots = await self._ensure_channel_slots()
        key = slots[outgoing.channel_index] if outgoing.channel_index < len(slots) else None
        channel = await ChannelRepository.get_by_key(key) if key else None
        if channel is None:
            raise VirtualNodeError(ErrorCode.NOT_FOUND, "no channel in that slot")
        self.forwarded_commands += 1
        try:
            await send_channel_message_to_channel(
                channel=channel,
                channel_key_upper=channel.key.upper(),
                key_bytes=bytes.fromhex(channel.key),
                text=outgoing.text,
                radio_manager=self._radio(),
                broadcast_fn=broadcast_event,
                error_broadcast_fn=broadcast_error,
                now_fn=time.time,
                temp_radio_slot=0,
            )
        except HTTPException as exc:
            logger.info("Virtual node channel send to %s failed: %s", channel.name, exc.detail)
            return protocol.encode_error(
                ErrorCode.BAD_STATE if exc.status_code in (408, 423) else ErrorCode.ILLEGAL_ARG
            )
        # The firmware answers a channel send with a plain OK.
        return protocol.encode_ok()

    # ------------------------------------------------------------------ forwarding

    def _cached_response(self, command: bytes) -> bytes | None:
        entry = self._response_cache.get(command)
        if entry is None:
            return None
        expires, response = entry
        if expires < time.time():
            del self._response_cache[command]
            return None
        return response

    async def _forward(
        self,
        command: bytes,
        *,
        stage_contact_key: str | None = None,
        refresh_identity: bool = False,
        expected: frozenset[int] | None = None,
        timeout: float | None = None,
        cacheable: bool = False,
    ) -> bytes:
        """Send a raw command to the radio and return the radio's answer frame.

        Runs under the shared radio operation lock so it cannot interleave
        with RemoteTerm's own commands. The answer is matched by response code
        via the frame tap (:meth:`on_radio_frame`); a timeout is reported to
        the app as ``ERR_CODE_BAD_STATE``.

        ``cacheable`` queries are re-checked against the response cache once
        the lock is held and stored before it is released, so several apps
        asking the same thing at the same moment cost one radio round trip:
        the first fills the cache and the rest, queued on the lock, read it.
        """
        code = command[0]
        if expected is None:
            expected = _EXPECTED_RESPONSES.get(code)
        if timeout is None:
            timeout = FORWARD_TIMEOUT_SECONDS
        try:
            return await asyncio.wait_for(
                self._forward_under_lock(
                    command,
                    stage_contact_key=stage_contact_key,
                    refresh_identity=refresh_identity,
                    expected=expected,
                    timeout=timeout,
                    cacheable=cacheable,
                ),
                timeout=FORWARD_TOTAL_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("Virtual node: radio busy too long for command %d", code)
            return protocol.encode_error(ErrorCode.BAD_STATE)
        except VirtualNodeError:
            raise
        except Exception as exc:
            # RadioDisconnectedError and friends: the radio is not usable now.
            logger.info("Virtual node could not forward command %d: %s", code, exc)
            return protocol.encode_error(ErrorCode.BAD_STATE)

    async def _forward_under_lock(
        self,
        command: bytes,
        *,
        stage_contact_key: str | None,
        refresh_identity: bool,
        expected: frozenset[int] | None,
        timeout: float,
        cacheable: bool = False,
    ) -> bytes:
        async with self._radio().radio_operation(f"virtual_node:{command[0]}") as mc:
            if cacheable:
                cached = self._cached_response(command)
                if cached is not None:
                    self.cached_commands += 1
                    return cached
            self.forwarded_commands += 1
            if stage_contact_key:
                await self._stage_contact(mc, stage_contact_key)
            response = await self._exchange(mc, command, expected, timeout)
            if refresh_identity and response[0] != ERR:
                await self._refresh_identity(mc)
            if cacheable and response[0] != ERR:
                self._response_cache[command] = (
                    time.time() + RESPONSE_CACHE_TTL_SECONDS,
                    response,
                )
            return response

    async def _exchange(
        self, mc: Any, command: bytes, expected: frozenset[int] | None, timeout: float
    ) -> bytes:
        loop = asyncio.get_running_loop()
        pending = _PendingForward(expected=expected, future=loop.create_future())
        self._pending_forward = pending
        try:
            await mc.connection_manager.send(command)
            return await asyncio.wait_for(pending.future, timeout=timeout)
        except TimeoutError:
            logger.info("Virtual node: no radio reply to command %d", command[0])
            return protocol.encode_error(ErrorCode.BAD_STATE)
        finally:
            if self._pending_forward is pending:
                self._pending_forward = None

    async def _stage_contact(self, mc: Any, public_key: str) -> None:
        """Put a stored contact back on the radio before a command addresses it."""
        contact = await ContactRepository.get_by_key(public_key)
        if contact is None or not protocol.is_full_public_key(contact.public_key):
            return
        try:
            result = await mc.commands.add_contact(contact.to_radio_dict())
            if result is not None and result.type == EventType.ERROR:
                logger.debug("Staging %s on radio failed: %s", public_key[:12], result.payload)
        except Exception as exc:
            logger.debug("Staging %s on radio raised: %s", public_key[:12], exc)

    async def _refresh_identity(self, mc: Any) -> None:
        """Re-read SELF_INFO/DEVICE_INFO after an app changed radio settings.

        The frame tap caches the raw frames as they pass; the radio manager's
        own view of path hash mode and repeat mode is updated too so RemoteTerm
        does not keep sending with a stale hop width.
        """
        from app.services.device_query import query_device_info
        from app.services.repeat_mode import extract_repeat_flag

        try:
            await mc.commands.send_appstart()
        except Exception as exc:
            logger.debug("Virtual node: self-info refresh failed: %s", exc)
        try:
            query = await query_device_info(mc)
        except Exception as exc:
            logger.debug("Virtual node: device-info refresh failed: %s", exc)
            return
        radio = self._radio()
        path_hash_mode = query.payload.get("path_hash_mode")
        if isinstance(path_hash_mode, int):
            radio.path_hash_mode = path_hash_mode
        repeat_flag = extract_repeat_flag(query.payload, query.raw_frame)
        if repeat_flag is not None:
            radio.repeat_enabled = repeat_flag

    # ------------------------------------------------------------------ radio -> apps

    def on_radio_frame(self, frame: bytes) -> None:
        """Every frame the radio sends RemoteTerm passes through here (frame tap).

        Three jobs: cache identity frames, complete the pending forwarded
        command, and relay push frames to connected apps.
        """
        if not frame:
            return
        code = frame[0]
        if code == _RESP.SELF_INFO.value:
            self._self_info_frame = bytes(frame)
        elif code == _RESP.DEVICE_INFO.value:
            self._device_info_frame = bytes(frame)

        pending = self._pending_forward
        if pending is not None and not pending.future.done() and code < 0x80:
            matches = (
                code in pending.expected
                if pending.expected is not None
                else code not in _NEVER_A_FORWARD_RESPONSE
            )
            if matches:
                pending.future.set_result(bytes(frame))
                return

        if not self._clients:
            return
        if code >= 0x80:
            if code not in _UNRELAYED_PUSH_CODES:
                self._broadcast_frame(bytes(frame))
            return
        if protocol.pulled_message_txt_type(frame) == 1:
            # A repeater's CLI reply. RemoteTerm's auto-fetch pulled it and its
            # own handler drops it, so this is the only way an app sees it.
            self._enqueue_for_all(bytes(frame))

    def _broadcast_frame(self, frame: bytes) -> None:
        for session in list(self._clients):
            self._spawn(self._send_quietly(session, frame))

    async def _send_quietly(self, session: ClientSession, frame: bytes) -> None:
        try:
            await session.send(frame)
        except Exception as exc:
            logger.debug("Virtual node push to %s failed: %s", session.peer, exc)

    def _enqueue_for_all(self, frame: bytes) -> None:
        waiting = protocol.encode_push_msg_waiting()
        for session in list(self._clients):
            session.inbox.append(frame)
            self._spawn(self._send_quietly(session, waiting))

    # ------------------------------------------------------------------ app pipeline -> apps

    def on_app_event(self, event_type: str, data: dict) -> None:
        """Mirror RemoteTerm's own broadcasts into the companion protocol.

        Called from ``broadcast_event`` for every realtime event. Incoming
        messages become queued ``*_MSG_RECV_V3`` frames plus a
        ``MSG_WAITING`` push; contact upserts become ``PUSH_CODE_ADVERT`` so
        apps re-pull the (locally served) contact list; channel changes
        refresh the virtual slot table.
        """
        if not self._clients:
            return
        if event_type == "message":
            if data.get("outgoing") or data.get("is_reaction"):
                return
            self._spawn(self._enqueue_message(dict(data)))
        elif event_type == "contact":
            public_key = str(data.get("public_key") or "")
            if protocol.is_full_public_key(public_key):
                self._broadcast_frame(protocol.encode_push_advert(public_key))
        elif event_type in ("channel", "channel_deleted"):
            self._spawn(self._ensure_channel_slots())

    async def _enqueue_message(self, message: dict) -> None:
        msg_type = message.get("type")
        if msg_type == "PRIV":
            frame = protocol.encode_contact_message(message)
        elif msg_type == "CHAN":
            key = str(message.get("conversation_key") or "")
            slot = await self._slot_for_channel(key) if key else None
            if slot is None:
                return
            frame = protocol.encode_channel_message(message, slot)
        else:
            return
        if frame is None:
            return
        self._enqueue_for_all(frame)


def _pending_ack_for_message(message_id: int) -> tuple[str | None, int]:
    """Newest ACK code RemoteTerm is waiting on for a message, with its timeout."""
    newest: tuple[float, str, int] | None = None
    for code, (pending_id, created_at, timeout_ms) in dm_ack_tracker._pending_acks.items():
        if pending_id == message_id and (newest is None or created_at > newest[0]):
            newest = (created_at, code, timeout_ms)
    if newest is None:
        return None, 0
    return newest[1], newest[2]


def install_frame_tap(meshcore: Any) -> None:
    """Route every inbound radio frame through :meth:`VirtualNodeServer.on_radio_frame`.

    Same wrapping strategy as the reader adapters in ``event_handlers.py``,
    including the idempotency flag so reconnects do not stack taps. Installed
    unconditionally: with the server disabled it is a dictionary lookup per
    frame and nothing else.
    """
    reader = meshcore._reader
    if getattr(reader, "_remoteterm_virtual_node_tap", False):
        return
    original = reader.handle_rx

    async def handle_rx(data: bytearray) -> None:
        try:
            virtual_node.on_radio_frame(bytes(data))
        except Exception:
            logger.exception("Virtual node frame tap failed")
        await original(data)

    reader.handle_rx = handle_rx
    reader._remoteterm_virtual_node_tap = True


virtual_node = VirtualNodeServer()
