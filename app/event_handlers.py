import logging
import time
from typing import TYPE_CHECKING

from meshcore import EventType

from app.models import CONTACT_TYPE_ROOM, Contact, ContactUpsert
from app.packet_processor import process_raw_packet
from app.repository import (
    ContactRepository,
)
from app.services import dm_ack_tracker
from app.services.contact_reconciliation import (
    promote_prefix_contacts_for_contact,
    record_contact_name_and_reconcile,
)
from app.services.dm_ack_apply import apply_dm_ack_code
from app.services.dm_ingest import (
    ingest_fallback_direct_message,
    resolve_direct_message_sender_metadata,
    resolve_fallback_direct_message_context,
)
from app.websocket import broadcast_event

if TYPE_CHECKING:
    from meshcore.events import Event, Subscription

logger = logging.getLogger(__name__)

# Track active subscriptions so we can unsubscribe before re-registering
# This prevents handler duplication after reconnects
_active_subscriptions: list["Subscription"] = []


def track_pending_ack(expected_ack: str, message_id: int, timeout_ms: int) -> bool:
    """Compatibility wrapper for pending DM ACK tracking."""
    return dm_ack_tracker.track_pending_ack(expected_ack, message_id, timeout_ms)


def cleanup_expired_acks() -> None:
    """Compatibility wrapper for expiring stale DM ACK entries."""
    dm_ack_tracker.cleanup_expired_acks()


async def on_contact_message(event: "Event") -> None:
    """Handle incoming direct messages from MeshCore library.

    NOTE: DMs are primarily handled by the packet processor via RX_LOG_DATA,
    which decrypts using our exported private key. This handler exists as a
    fallback for cases where:
    1. The private key couldn't be exported (firmware without ENABLE_PRIVATE_KEY_EXPORT)
    2. The packet processor couldn't match the sender to a known contact

    The packet processor handles: decryption, storage, broadcast, bot trigger.
    This handler adapts CONTACT_MSG_RECV payloads into the shared DM ingest
    workflow, which reconciles duplicates against the packet pipeline when possible.
    """
    payload = event.payload

    # Skip CLI command responses (txt_type=1) - these are handled by the command endpoint
    txt_type = payload.get("txt_type", 0)
    if txt_type == 1:
        logger.debug("Skipping CLI response from %s (txt_type=1)", payload.get("pubkey_prefix"))
        return

    # Get full public key if available, otherwise use prefix
    sender_pubkey = payload.get("public_key") or payload.get("pubkey_prefix", "")
    received_at = int(time.time())

    context = await resolve_fallback_direct_message_context(
        sender_public_key=sender_pubkey,
        received_at=received_at,
        broadcast_fn=broadcast_event,
        contact_repository=ContactRepository,
        log=logger,
    )
    if context.skip_storage:
        logger.debug(
            "Skipping message from repeater %s (not stored in chat history)",
            context.conversation_key[:12],
        )
        return

    # Try to create or reconcile the message via the shared DM ingest service.
    ts = payload.get("sender_timestamp")
    sender_timestamp = ts if ts is not None else received_at
    path = payload.get("path")
    path_len = payload.get("path_len")
    sender_name = context.sender_name
    sender_key = context.sender_key
    signature = payload.get("signature")
    if (
        context.contact is not None
        and context.contact.type == CONTACT_TYPE_ROOM
        and txt_type == 2
        and isinstance(signature, str)
        and signature
    ):
        sender_name, sender_key = await resolve_direct_message_sender_metadata(
            sender_public_key=signature,
            received_at=received_at,
            broadcast_fn=broadcast_event,
            contact_repository=ContactRepository,
            log=logger,
        )
    message = await ingest_fallback_direct_message(
        conversation_key=context.conversation_key,
        text=payload.get("text", ""),
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        path=path,
        path_len=path_len,
        txt_type=txt_type,
        signature=signature,
        sender_name=sender_name,
        sender_key=sender_key,
        broadcast_fn=broadcast_event,
        update_last_contacted_key=context.contact.public_key.lower() if context.contact else None,
    )

    if message is None:
        # Already handled by packet processor (or exact duplicate) - nothing more to do
        logger.debug(
            "DM from %s already processed by packet processor", context.conversation_key[:12]
        )
        return

    # If we get here, the packet processor didn't handle this message
    # (likely because private key export is not available)
    logger.debug(
        "DM from %s handled by event handler (fallback path)", context.conversation_key[:12]
    )


async def on_rx_log_data(event: "Event") -> None:
    """Store raw RF packet data and process via centralized packet processor.

    This is the unified entry point for all RF packets. The packet processor
    handles channel messages (GROUP_TEXT) and advertisements (ADVERT).
    """
    payload = event.payload
    logger.debug("Received RX log data packet")

    if "payload" not in payload:
        logger.warning("RX_LOG_DATA event missing 'payload' field")
        return

    raw_hex = payload["payload"]
    raw_bytes = bytes.fromhex(raw_hex)

    await process_raw_packet(
        raw_bytes=raw_bytes,
        snr=payload.get("snr"),
        rssi=payload.get("rssi"),
    )


async def on_path_update(event: "Event") -> None:
    """Handle path update events."""
    payload = event.payload
    public_key = str(payload.get("public_key", "")).lower()
    pubkey_prefix = str(payload.get("pubkey_prefix", "")).lower()

    contact: Contact | None = None
    if public_key:
        logger.debug("Path update for %s", public_key[:12])
        contact = await ContactRepository.get_by_key(public_key)
    elif pubkey_prefix:
        # Legacy compatibility: older payloads may only include a prefix.
        logger.debug("Path update for prefix %s", pubkey_prefix)
        contact = await ContactRepository.get_by_key_prefix(pubkey_prefix)
    else:
        logger.debug("PATH_UPDATE missing public_key/pubkey_prefix, skipping")
        return

    if not contact:
        return

    # PATH_UPDATE is a serial control push event from firmware (not an RF packet).
    # Current meshcore payloads only include public_key for this event.
    # RF route/path bytes are handled via RX_LOG_DATA -> process_raw_packet,
    # so if path fields are absent here we treat this as informational only.
    path = payload.get("path")
    path_len = payload.get("path_len")
    path_hash_mode = payload.get("path_hash_mode")
    if path is None or path_len is None:
        logger.debug(
            "PATH_UPDATE for %s has no path payload, skipping DB update", contact.public_key[:12]
        )
        return

    try:
        normalized_path_len = int(path_len)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid path_len in PATH_UPDATE for %s: %r", contact.public_key[:12], path_len
        )
        return

    normalized_path_hash_mode: int | None
    if path_hash_mode is None:
        # Legacy firmware/library payloads only support 1-byte hop hashes.
        normalized_path_hash_mode = -1 if normalized_path_len == -1 else 0
    else:
        try:
            normalized_path_hash_mode = int(path_hash_mode)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid path_hash_mode in PATH_UPDATE for %s: %r",
                contact.public_key[:12],
                path_hash_mode,
            )
            normalized_path_hash_mode = None

    await ContactRepository.update_direct_path(
        contact.public_key,
        str(path),
        normalized_path_len,
        normalized_path_hash_mode,
        updated_at=int(time.time()),
    )


async def on_new_contact(event: "Event") -> None:
    """Handle new contact from radio's internal contact database.

    This is different from RF advertisements - these are contacts synced
    from the radio's stored contact list.
    """
    payload = event.payload
    public_key = payload.get("public_key", "")

    if not public_key:
        logger.warning("Received new contact event with no public_key, skipping")
        return

    logger.debug("New contact: %s", public_key[:12])

    contact_upsert = ContactUpsert.from_radio_dict(public_key.lower(), payload, on_radio=False)

    # Block new contacts whose type is in discovery_blocked_types, matching
    # the same guard in _process_advertisement.  Existing contacts (already
    # in the DB) are always updated.
    existing = await ContactRepository.get_by_key(public_key.lower())
    contact_type = contact_upsert.type or 0
    if existing is None and contact_type > 0:
        from app.repository import AppSettingsRepository

        settings = await AppSettingsRepository.get()
        if contact_type in settings.discovery_blocked_types:
            logger.debug(
                "Skipping new contact %s: type %d is in discovery_blocked_types",
                public_key[:12],
                contact_type,
            )
            return

    # Intentionally do not set first_seen or last_seen here: NEW_CONTACT
    # fires from the radio's stored contact DB, not an RF observation.
    # Both first_seen and last_seen are RF-only timestamps — they track
    # the first and most recent time we actually heard this pubkey over
    # the air (adverts, messages, path updates). Contacts synced from the
    # radio's internal DB without any RF activity stay NULL until a real
    # RF observation fills them in.
    await ContactRepository.upsert(contact_upsert)
    promoted_keys = await promote_prefix_contacts_for_contact(
        public_key=public_key,
        log=logger,
    )

    adv_name = payload.get("adv_name")
    await record_contact_name_and_reconcile(
        public_key=public_key,
        contact_name=adv_name,
        timestamp=int(time.time()),
        log=logger,
    )

    # Read back from DB so the broadcast includes all fields (last_contacted,
    # last_read_at, etc.) matching the REST Contact shape exactly.
    db_contact = await ContactRepository.get_by_key(public_key)
    broadcast_event(
        "contact",
        (
            db_contact.model_dump()
            if db_contact
            else Contact(**contact_upsert.model_dump(exclude_none=True)).model_dump()
        ),
    )
    if db_contact:
        for old_key in promoted_keys:
            broadcast_event(
                "contact_resolved",
                {
                    "previous_public_key": old_key,
                    "contact": db_contact.model_dump(),
                },
            )


async def on_ack(event: "Event") -> None:
    """Handle ACK events for direct messages."""
    payload = event.payload
    ack_code = payload.get("code", "")

    if not ack_code:
        logger.debug("Received ACK with no code")
        return

    logger.debug("Received ACK with code %s", ack_code)
    matched = await apply_dm_ack_code(ack_code, broadcast_fn=broadcast_event)
    if matched:
        logger.info("ACK received for code %s", ack_code)
    else:
        logger.debug("ACK code %s does not match any pending messages", ack_code)


async def on_raw_data(event: "Event") -> None:
    """Handle full PUSH_CODE_RAW_DATA payloads used by interoperable media."""
    from app.services.radio_runtime import radio_runtime
    from app.services.raw_media import MediaTransport, dispatch_raw_media_payload

    raw = event.payload.get("payload", b"")
    if isinstance(raw, str):
        try:
            raw = bytes.fromhex(raw)
        except ValueError:
            return
    if isinstance(raw, (bytes, bytearray)):
        # Stated rather than defaulted: the handlers reply on the transport a
        # request arrived on, so this is the fact that keeps SAR clients answered
        # in raw even when the contact's text switch is on.
        await dispatch_raw_media_payload(bytes(raw), radio_runtime, transport=MediaTransport.RAW)


def install_full_raw_data_adapter(meshcore) -> None:
    """Work around meshcore-py 2.3.7 truncating RAW_DATA pushes to four bytes."""
    from meshcore.events import Event
    from meshcore.packets import PacketType

    reader = meshcore._reader
    if getattr(reader, "_remoteterm_full_raw_data", False):
        return
    original = reader.handle_rx

    async def handle_rx(data: bytearray) -> None:
        if data and data[0] == PacketType.RAW_DATA.value and len(data) >= 3:
            payload = {
                "SNR": int.from_bytes(data[1:2], "little", signed=True) / 4,
                "RSSI": int.from_bytes(data[2:3], "little", signed=True),
                "payload": bytes(data[3:]),
            }
            await reader.dispatcher.dispatch(Event(EventType.RAW_DATA, payload))
            return
        await original(data)

    reader.handle_rx = handle_rx
    reader._remoteterm_full_raw_data = True


async def _resolve_channel_data_key(channel_index: int, radio=None) -> str | None:
    """Map an inbound GRP_DATA frame's radio slot to a channel key.

    Cheap caches first, then the radio. The radio query is the only path that
    works on a cold cache, and a cold cache is the NORMAL state for a receiver:
    the reuse maps are populated as a side effect of *sending*, and on TCP (or
    with ``force_channel_slot_reconfigure``) they are never populated at all --
    see :meth:`RadioManager.channel_key_for_slot`. Resolving from the caches
    alone meant a peer's image was dropped unless we happened to have sent on
    that channel first.

    Safe to await here: the connection spawns each ``handle_rx`` as its own task,
    so waiting on a command reply does not stall the frame that carries it.
    ``_resolve_channel_for_pending_message`` also remembers what it learns, so
    only the first chunk of an image pays for the round trip.

    ``radio`` names the radio manager to ask; it defaults to the process-wide
    one and is passed explicitly by the virtual companion node, which holds its
    own reference.
    """
    from app.radio_sync import _resolve_channel_for_pending_message

    if radio is None:
        from app.services.radio_runtime import radio_runtime

        radio = radio_runtime

    cached = radio.channel_key_for_slot(channel_index)
    if cached is not None:
        return cached

    try:
        async with radio.radio_operation("aeic_channel_data_slot") as mc:
            key, _name = await _resolve_channel_for_pending_message(mc, channel_index)
    except Exception:
        # A busy or disconnected radio is not worth an exception here; the next
        # chunk retries, and a fully unresolvable slot is logged by the caller.
        logger.debug("Could not ask the radio which channel is in slot %d", channel_index)
        return None
    return key


async def on_channel_data(frame: bytes) -> None:
    """Handle one ``RESP_CODE_CHANNEL_DATA_RECV`` (27) frame.

    This is how an AEIC image sent by MCO Advanced arrives: binary GRP_DATA, not
    text. The firmware has already decrypted it and split off the data type, so
    nothing here touches crypto.
    """
    from app.imaging.aeic.channel_data import parse_channel_data_frame
    from app.imaging.aeic.channel_data_ingest import handle_channel_data

    parsed = parse_channel_data_frame(frame)
    if parsed is None:
        return
    conversation_key = await _resolve_channel_data_key(parsed.channel_index)
    if conversation_key is None:
        # The frame names a radio slot and nothing -- not the slot caches, not the
        # radio itself -- could say which channel lives there. Nothing to attach
        # the image to, so say why rather than dropping it silently.
        logger.info(
            "Ignoring GRP_DATA on radio slot %d: could not resolve it to a channel",
            parsed.channel_index,
        )
        return
    await handle_channel_data(
        parsed, conversation_key=conversation_key, broadcast_fn=broadcast_event
    )


def install_channel_data_adapter(meshcore) -> None:
    """Intercept companion frame 27, which meshcore-py does not know about.

    ``PacketType`` jumps straight from 26 to 28, so an inbound GRP_DATA frame
    reaches ``reader.handle_rx``'s final ``else`` and is logged away as
    "Unhandled packet type" -- which is exactly why an image sent from MCO
    Advanced produced nothing in RemoteTerm, not even garbled text.

    Same wrapping strategy as :func:`install_full_raw_data_adapter`, including
    the idempotency flag, so reconnects do not stack adapters.

    Consuming the frame has a second obligation. Frame 27 usually arrives as the
    firmware's answer to ``CMD_SYNC_NEXT_MESSAGE``, and meshcore-py's ``get_msg``
    does not know it: the caller that asked is left waiting for a reply that
    already came. The auto-fetch loop asks with NO timeout, so one queued image
    hung it forever -- and its MESSAGES_WAITING handler refuses to start a second
    task while the first is alive, so every later push-driven fetch was dead too.
    Channel *text* still flowed (it is sniffed off raw RF, not pulled from this
    queue), which is what made the failure read as "text always, images never".
    A placeholder CHANNEL_MSG_RECV is dispatched for each frame so the waiter
    resolves and keeps draining; the app's pulled-message consumers skip it (see
    ``is_grp_data_placeholder``).

    The placeholder goes out BEFORE the frame is processed, not after: handling
    ends in a channel-slot lookup that can wait on the radio lock, and the drain
    loops hold that lock for their whole run -- dispatching after would deadlock
    the resolution against the very waiter it is meant to release.
    """
    from meshcore.events import Event

    from app.imaging.aeic.channel_data import (
        RESP_CODE_CHANNEL_DATA_RECV,
        grp_data_placeholder_payload,
    )

    reader = meshcore._reader
    if getattr(reader, "_remoteterm_channel_data", False):
        return
    original = reader.handle_rx

    async def handle_rx(data: bytearray) -> None:
        if data and data[0] == RESP_CODE_CHANNEL_DATA_RECV:
            try:
                await meshcore.dispatcher.dispatch(
                    Event(EventType.CHANNEL_MSG_RECV, grp_data_placeholder_payload())
                )
            except Exception:
                # A dispatcher that is not running yet only costs a get_msg
                # timeout; the image itself is still handled below.
                logger.debug("Could not release the message waiter for a GRP_DATA frame")
            try:
                await on_channel_data(bytes(data))
            except Exception:
                logger.exception("Failed to handle an inbound GRP_DATA frame")
            return
        await original(data)

    reader.handle_rx = handle_rx
    reader._remoteterm_channel_data = True


def register_event_handlers(meshcore) -> None:
    """Register event handlers with the MeshCore instance.

    Note: CHANNEL_MSG_RECV and ADVERTISEMENT events are NOT subscribed.
    These are handled by the packet processor via RX_LOG_DATA to avoid
    duplicate processing and ensure consistent handling.

    This function is safe to call multiple times (e.g., after reconnect).
    Existing handlers are unsubscribed before new ones are registered.
    """
    global _active_subscriptions

    # Unsubscribe existing handlers to prevent duplication after reconnects.
    # Try/except handles the case where the old dispatcher is in a bad state
    # (e.g., after reconnect with a new MeshCore instance).
    for sub in _active_subscriptions:
        try:
            sub.unsubscribe()
        except Exception:
            pass  # Old dispatcher may be gone, that's fine
    _active_subscriptions.clear()

    # Register handlers and track subscriptions
    _active_subscriptions.append(meshcore.subscribe(EventType.CONTACT_MSG_RECV, on_contact_message))
    _active_subscriptions.append(meshcore.subscribe(EventType.RX_LOG_DATA, on_rx_log_data))
    _active_subscriptions.append(meshcore.subscribe(EventType.PATH_UPDATE, on_path_update))
    _active_subscriptions.append(meshcore.subscribe(EventType.NEW_CONTACT, on_new_contact))
    _active_subscriptions.append(meshcore.subscribe(EventType.ACK, on_ack))
    _active_subscriptions.append(meshcore.subscribe(EventType.RAW_DATA, on_raw_data))
    install_full_raw_data_adapter(meshcore)
    install_channel_data_adapter(meshcore)
    # The virtual companion node sees every inbound frame: it caches identity
    # frames, completes the commands it forwarded for apps, and relays pushes.
    from app.virtual_node.server import install_frame_tap

    install_frame_tap(meshcore)
    logger.info("Event handlers registered")
