import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from app.compression import CompressionInfo, decode_and_describe
from app.imaging.aeic.ingest import note_inbound_chunk
from app.models import Message, MessagePath
from app.reactions import apply_reaction, extract_reaction_from_stored_text
from app.repository import ContactRepository, MessageRepository, RawPacketRepository

if TYPE_CHECKING:
    from app.decoder import DecryptedDirectMessage

logger = logging.getLogger(__name__)

BroadcastFn = Callable[..., Any]
LOG_MESSAGE_PREVIEW_LEN = 32


def truncate_for_log(text: str, max_chars: int = LOG_MESSAGE_PREVIEW_LEN) -> str:
    """Return a compact single-line message preview for log output."""
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    return f"{normalized[:max_chars].rstrip()}..."


def _format_channel_log_target(channel_name: str | None, channel_key: str) -> str:
    """Return a human-friendly channel label for logs."""
    return channel_name or channel_key


def format_contact_log_target(contact_name: str | None, public_key: str) -> str:
    """Return a human-friendly DM target label for logs."""
    return contact_name or public_key[:12]


def build_message_paths(
    path: str | None,
    received_at: int,
    path_len: int | None = None,
    rssi: int | None = None,
    snr: float | None = None,
) -> list[MessagePath] | None:
    """Build the single-path list used by message payloads."""
    return (
        [
            MessagePath(
                path=path or "", received_at=received_at, path_len=path_len, rssi=rssi, snr=snr
            )
        ]
        if path is not None
        else None
    )


def build_message_model(
    *,
    message_id: int,
    msg_type: str,
    conversation_key: str,
    text: str,
    sender_timestamp: int | None,
    received_at: int,
    paths: list[MessagePath] | None = None,
    txt_type: int = 0,
    signature: str | None = None,
    sender_key: str | None = None,
    outgoing: bool = False,
    acked: int = 0,
    sender_name: str | None = None,
    channel_name: str | None = None,
    packet_id: int | None = None,
    transport_code: int | None = None,
    region: str | None = None,
    compression: CompressionInfo | None = None,
    send_attempts: int | None = None,
    send_max_attempts: int | None = None,
    send_state: str | None = None,
    is_reaction: bool = False,
) -> Message:
    """Build a Message model with the canonical backend payload shape."""
    return Message(
        id=message_id,
        type=msg_type,
        conversation_key=conversation_key,
        text=text,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        paths=paths,
        txt_type=txt_type,
        signature=signature,
        sender_key=sender_key,
        outgoing=outgoing,
        acked=acked,
        sender_name=sender_name,
        channel_name=channel_name,
        packet_id=packet_id,
        transport_code=transport_code,
        region=region,
        compression=compression.codec if compression else None,
        plain_bytes=compression.plain_bytes if compression else None,
        wire_bytes=compression.wire_bytes if compression else None,
        payload_bytes=compression.payload_bytes if compression else None,
        send_attempts=send_attempts,
        send_max_attempts=send_max_attempts,
        send_state=send_state,
        is_reaction=is_reaction,
    )


def broadcast_message(
    *,
    message: Message,
    broadcast_fn: BroadcastFn,
    realtime: bool | None = None,
    packet_hash: str | None = None,
) -> None:
    """Broadcast a message payload, preserving the caller's broadcast signature."""
    payload = message.model_dump()
    if packet_hash is not None:
        payload["packet_hash"] = packet_hash
    if realtime is None:
        broadcast_fn("message", payload)
    else:
        broadcast_fn("message", payload, realtime=realtime)


async def build_stored_outgoing_channel_message(
    *,
    message_id: int,
    conversation_key: str,
    text: str,
    sender_timestamp: int,
    received_at: int,
    sender_name: str | None,
    sender_key: str | None,
    channel_name: str | None,
    compression: CompressionInfo | None = None,
    send_attempts: int | None = None,
    send_state: str | None = None,
    message_repository=MessageRepository,
) -> Message:
    """Build the current payload for a stored outgoing channel message."""
    acked_count, paths = await message_repository.get_ack_and_paths(message_id)
    return build_message_model(
        message_id=message_id,
        msg_type="CHAN",
        conversation_key=conversation_key,
        text=text,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        paths=paths,
        outgoing=True,
        acked=acked_count,
        sender_name=sender_name,
        sender_key=sender_key,
        channel_name=channel_name,
        compression=compression,
        send_attempts=send_attempts,
        send_state=send_state,
        is_reaction=extract_reaction_from_stored_text("CHAN", text) is not None,
    )


def broadcast_message_status(
    *,
    message_id: int,
    send_state: str | None,
    send_attempts: int | None,
    send_max_attempts: int | None,
    broadcast_fn: BroadcastFn,
) -> None:
    """Broadcast an outgoing message's send progress.

    Separate from ``message_acked`` because progress and delivery are different
    facts: a message can reach its last attempt without ever being acked, and an
    ACK can land after the attempts are done.
    """
    broadcast_fn(
        "message_status",
        {
            "message_id": message_id,
            "send_state": send_state,
            "send_attempts": send_attempts,
            "send_max_attempts": send_max_attempts,
        },
    )


def broadcast_message_acked(
    *,
    message_id: int,
    ack_count: int,
    paths: list[MessagePath] | None,
    packet_id: int | None,
    broadcast_fn: BroadcastFn,
) -> None:
    """Broadcast a message_acked payload."""
    broadcast_fn(
        "message_acked",
        {
            "message_id": message_id,
            "ack_count": ack_count,
            "paths": [path.model_dump() for path in paths] if paths else [],
            "packet_id": packet_id,
        },
    )


async def increment_ack_and_broadcast(
    *,
    message_id: int,
    broadcast_fn: BroadcastFn,
) -> int:
    """Increment a message's ACK count and broadcast the update."""
    ack_count = await MessageRepository.increment_ack_count(message_id)
    broadcast_fn("message_acked", {"message_id": message_id, "ack_count": ack_count})
    return ack_count


async def reconcile_duplicate_message(
    *,
    existing_msg: Message,
    packet_id: int | None,
    path: str | None,
    received_at: int,
    path_len: int | None,
    rssi: int | None = None,
    snr: float | None = None,
    broadcast_fn: BroadcastFn,
) -> None:
    logger.debug(
        "Duplicate %s for %s (msg_id=%d, outgoing=%s) - adding path",
        existing_msg.type,
        existing_msg.conversation_key[:12],
        existing_msg.id,
        existing_msg.outgoing,
    )

    if path is not None:
        paths = await MessageRepository.add_path(
            existing_msg.id, path, received_at, path_len, rssi=rssi, snr=snr
        )
    else:
        paths = existing_msg.paths or []

    if existing_msg.outgoing and existing_msg.type == "CHAN":
        ack_count = await MessageRepository.increment_ack_count(existing_msg.id)
    else:
        ack_count = existing_msg.acked

    representative_packet_id = (
        existing_msg.packet_id if existing_msg.packet_id is not None else packet_id
    )

    if existing_msg.outgoing or path is not None:
        broadcast_message_acked(
            message_id=existing_msg.id,
            ack_count=ack_count,
            paths=paths,
            packet_id=representative_packet_id,
            broadcast_fn=broadcast_fn,
        )

    if packet_id is not None:
        await RawPacketRepository.mark_decrypted(packet_id, existing_msg.id)


async def handle_duplicate_message(
    *,
    packet_id: int | None,
    msg_type: str,
    conversation_key: str,
    text: str,
    sender_timestamp: int,
    outgoing: bool | None = None,
    path: str | None,
    received_at: int,
    path_len: int | None = None,
    rssi: int | None = None,
    snr: float | None = None,
    broadcast_fn: BroadcastFn,
) -> None:
    """Handle a duplicate message by updating paths/acks on the existing record."""
    existing_msg = await MessageRepository.get_by_content(
        msg_type=msg_type,
        conversation_key=conversation_key,
        text=text,
        sender_timestamp=sender_timestamp,
        outgoing=outgoing,
    )
    if not existing_msg:
        label = "message" if msg_type == "CHAN" else "DM"
        logger.warning(
            "Duplicate %s for %s but couldn't find existing",
            label,
            conversation_key[:12],
        )
        return

    await reconcile_duplicate_message(
        existing_msg=existing_msg,
        packet_id=packet_id,
        path=path,
        received_at=received_at,
        path_len=path_len,
        rssi=rssi,
        snr=snr,
        broadcast_fn=broadcast_fn,
    )


async def create_message_from_decrypted(
    *,
    packet_id: int,
    channel_key: str,
    sender: str | None,
    message_text: str,
    timestamp: int,
    received_at: int | None = None,
    path: str | None = None,
    path_len: int | None = None,
    rssi: int | None = None,
    snr: float | None = None,
    channel_name: str | None = None,
    realtime: bool = True,
    broadcast_fn: BroadcastFn,
    packet_hash: str | None = None,
    transport_code: int | None = None,
    region: str | None = None,
) -> int | None:
    """Store and broadcast a decrypted channel message."""
    received = received_at or int(time.time())
    # MCMP-compressed bodies ride as ordinary text behind an mcmp2:/mcmp3:
    # prefix; decode to plaintext before storage/dedup so the DB, search and bots
    # see the real message (non-MCMP text is returned unchanged). The compression
    # facts are measured against the body alone -- the firmware adds the
    # "<name>: " prefix outside the compressed payload, so counting it would
    # understate the ratio.
    message_text, compression = decode_and_describe(message_text)
    text = f"{sender}: {message_text}" if sender else message_text
    channel_key_normalized = channel_key.upper()

    # A reaction payload ("Name: r:HHHH:II") is stored like any message so
    # flood-echo dedup works, but hidden and applied to its target instead of
    # broadcast as a bubble.
    reaction = extract_reaction_from_stored_text("CHAN", text)

    resolved_sender_key: str | None = None
    if sender:
        candidates = await ContactRepository.get_by_name(sender)
        if len(candidates) == 1:
            resolved_sender_key = candidates[0].public_key

    msg_id = await MessageRepository.create(
        msg_type="CHAN",
        text=text,
        conversation_key=channel_key_normalized,
        sender_timestamp=timestamp,
        received_at=received,
        path=path,
        path_len=path_len,
        rssi=rssi,
        snr=snr,
        sender_name=sender,
        sender_key=resolved_sender_key,
        transport_code=transport_code,
        region=region,
        compression=compression,
        is_reaction=reaction is not None,
    )

    if msg_id is None:
        await handle_duplicate_message(
            packet_id=packet_id,
            msg_type="CHAN",
            conversation_key=channel_key_normalized,
            text=text,
            sender_timestamp=timestamp,
            outgoing=None,
            path=path,
            received_at=received,
            path_len=path_len,
            rssi=rssi,
            snr=snr,
            broadcast_fn=broadcast_fn,
        )
        return None

    if reaction is not None:
        await RawPacketRepository.mark_decrypted(packet_id, msg_id)
        await apply_reaction(
            msg_type="CHAN",
            conversation_key=channel_key_normalized,
            reaction=reaction,
            reactor_is_self=False,
            broadcast_fn=broadcast_fn,
        )
        return msg_id

    logger.info(
        'Stored channel message "%s" for %r (msg ID %d in chan ID %s)',
        truncate_for_log(text),
        _format_channel_log_target(channel_name, channel_key_normalized),
        msg_id,
        channel_key_normalized,
    )
    await RawPacketRepository.mark_decrypted(packet_id, msg_id)

    broadcast_message(
        message=build_message_model(
            message_id=msg_id,
            msg_type="CHAN",
            conversation_key=channel_key_normalized,
            text=text,
            sender_timestamp=timestamp,
            received_at=received,
            paths=build_message_paths(path, received, path_len, rssi=rssi, snr=snr),
            sender_name=sender,
            sender_key=resolved_sender_key,
            channel_name=channel_name,
            packet_id=packet_id,
            transport_code=transport_code,
            region=region,
            compression=compression,
        ),
        broadcast_fn=broadcast_fn,
        realtime=realtime,
        packet_hash=packet_hash,
    )

    # An aei1: body is one chunk of an AEIC image. The text stays in the message
    # (as the IE4 envelope does); this feeds the reassembler.
    await note_inbound_chunk(
        text=text,
        message_id=msg_id,
        conversation_type="CHAN",
        conversation_key=channel_key_normalized,
        peer_public_key=resolved_sender_key,
        sender_name=sender,
        broadcast_fn=broadcast_fn,
    )

    return msg_id


async def backfill_message_regions(known_regions: list[str]) -> dict[str, int]:
    """Re-resolve region scope for stored channel messages that still have a raw packet.

    Region is normally resolved at ingest, so messages stored before the feature
    existed (or before a region was added to the list) carry no region. This walks
    every CHAN message that still has a retained raw packet, recomputes its
    transport code, and persists the resolved region.

    Messages whose raw packet has been purged cannot be re-evaluated.

    Returns counts: ``scanned`` (messages examined), ``scoped`` (transport-routed),
    and ``named`` (matched a known region).
    """
    from app.path_utils import parse_packet_envelope
    from app.region_resolver import resolve_region

    scanned = scoped = named = 0
    async for message_id, raw_bytes in MessageRepository.stream_chan_messages_with_raw():
        scanned += 1
        env = parse_packet_envelope(raw_bytes)
        if env is None or env.transport_codes is None:
            continue
        scoped += 1
        transport_code = env.transport_codes[0]
        region = resolve_region(int(env.payload_type), env.payload, transport_code, known_regions)
        if region:
            named += 1
        await MessageRepository.set_transport_scope(message_id, transport_code, region)

    logger.info("Region backfill complete: scanned=%d scoped=%d named=%d", scanned, scoped, named)
    return {"scanned": scanned, "scoped": scoped, "named": named}


async def create_dm_message_from_decrypted(
    *,
    packet_id: int,
    decrypted: "DecryptedDirectMessage",
    their_public_key: str,
    our_public_key: str | None,
    received_at: int | None = None,
    path: str | None = None,
    path_len: int | None = None,
    rssi: int | None = None,
    snr: float | None = None,
    outgoing: bool = False,
    realtime: bool = True,
    broadcast_fn: BroadcastFn,
    packet_hash: str | None = None,
    transport_code: int | None = None,
    region: str | None = None,
) -> int | None:
    """Store and broadcast a decrypted direct message."""
    from app.services.dm_ingest import ingest_decrypted_direct_message

    message = await ingest_decrypted_direct_message(
        packet_id=packet_id,
        decrypted=decrypted,
        their_public_key=their_public_key,
        received_at=received_at,
        path=path,
        path_len=path_len,
        rssi=rssi,
        snr=snr,
        outgoing=outgoing,
        realtime=realtime,
        broadcast_fn=broadcast_fn,
        packet_hash=packet_hash,
        transport_code=transport_code,
        region=region,
    )
    return message.id if message is not None else None


async def create_fallback_channel_message(
    *,
    conversation_key: str,
    message_text: str,
    sender_timestamp: int,
    received_at: int,
    path: str | None,
    path_len: int | None,
    txt_type: int,
    sender_name: str | None,
    channel_name: str | None,
    broadcast_fn: BroadcastFn,
    message_repository=MessageRepository,
) -> Message | None:
    """Store and broadcast a CHANNEL_MSG_RECV fallback channel message."""
    conversation_key_normalized = conversation_key.upper()
    # Decode MCMP here too: this get_msg() drain route must decode at parity with
    # the raw-RF route (create_message_from_decrypted) or the same message
    # arriving via both paths would store two rows (dedup keys on text).
    message_text, compression = decode_and_describe(message_text)
    text = f"{sender_name}: {message_text}" if sender_name else message_text

    # Same reaction handling as the raw-RF route: store hidden, apply to target.
    reaction = extract_reaction_from_stored_text("CHAN", text)

    resolved_sender_key: str | None = None
    if sender_name:
        candidates = await ContactRepository.get_by_name(sender_name)
        if len(candidates) == 1:
            resolved_sender_key = candidates[0].public_key

    msg_id = await message_repository.create(
        msg_type="CHAN",
        text=text,
        conversation_key=conversation_key_normalized,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        path=path,
        path_len=path_len,
        txt_type=txt_type,
        sender_name=sender_name,
        sender_key=resolved_sender_key,
        compression=compression,
        is_reaction=reaction is not None,
    )
    if msg_id is None:
        await handle_duplicate_message(
            packet_id=None,
            msg_type="CHAN",
            conversation_key=conversation_key_normalized,
            text=text,
            sender_timestamp=sender_timestamp,
            outgoing=None,
            path=path,
            received_at=received_at,
            path_len=path_len,
            broadcast_fn=broadcast_fn,
        )
        return None

    if reaction is not None:
        await apply_reaction(
            msg_type="CHAN",
            conversation_key=conversation_key_normalized,
            reaction=reaction,
            reactor_is_self=False,
            broadcast_fn=broadcast_fn,
        )
        return None

    message = build_message_model(
        message_id=msg_id,
        msg_type="CHAN",
        conversation_key=conversation_key_normalized,
        text=text,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        paths=build_message_paths(path, received_at, path_len),
        txt_type=txt_type,
        sender_name=sender_name,
        sender_key=resolved_sender_key,
        channel_name=channel_name,
        compression=compression,
    )
    broadcast_message(message=message, broadcast_fn=broadcast_fn)

    # Third and last AEIC ingest route: the get_msg() drain. Must feed the
    # reassembler at parity with the raw-RF route above, or an image that
    # arrives via this path alone never renders.
    await note_inbound_chunk(
        text=text,
        message_id=msg_id,
        conversation_type="CHAN",
        conversation_key=conversation_key_normalized,
        peer_public_key=resolved_sender_key,
        sender_name=sender_name,
        broadcast_fn=broadcast_fn,
    )
    return message


async def create_outgoing_direct_message(
    *,
    conversation_key: str,
    text: str,
    sender_timestamp: int,
    received_at: int,
    broadcast_fn: BroadcastFn,
    compression: CompressionInfo | None = None,
    send_attempts: int | None = None,
    send_max_attempts: int | None = None,
    send_state: str | None = None,
    message_repository=MessageRepository,
) -> Message | None:
    """Store and broadcast an outgoing direct message."""
    # An outgoing reaction row is stored hidden; the react endpoint applies it
    # to its target only after the radio accepts the send.
    is_reaction = extract_reaction_from_stored_text("PRIV", text) is not None
    msg_id = await message_repository.create(
        msg_type="PRIV",
        text=text,
        conversation_key=conversation_key,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        outgoing=True,
        compression=compression,
        send_attempts=send_attempts,
        send_max_attempts=send_max_attempts,
        send_state=send_state,
        is_reaction=is_reaction,
    )
    if msg_id is None:
        return None

    message = build_message_model(
        message_id=msg_id,
        msg_type="PRIV",
        conversation_key=conversation_key,
        text=text,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        outgoing=True,
        acked=0,
        compression=compression,
        send_attempts=send_attempts,
        send_max_attempts=send_max_attempts,
        send_state=send_state,
    )
    if not is_reaction:
        broadcast_message(message=message, broadcast_fn=broadcast_fn)
    return message


async def create_outgoing_channel_message(
    *,
    conversation_key: str,
    text: str,
    sender_timestamp: int,
    received_at: int,
    sender_name: str | None,
    sender_key: str | None,
    channel_name: str | None,
    broadcast_fn: BroadcastFn,
    broadcast: bool = True,
    compression: CompressionInfo | None = None,
    send_attempts: int | None = None,
    send_state: str | None = None,
    message_repository=MessageRepository,
) -> Message | None:
    """Store and broadcast an outgoing channel message."""
    # Outgoing reactions ("OurName: r:HHHH:II") are stored hidden; the react
    # endpoint applies them to their target once the radio accepts the send.
    is_reaction = extract_reaction_from_stored_text("CHAN", text) is not None
    msg_id = await message_repository.create(
        msg_type="CHAN",
        text=text,
        conversation_key=conversation_key,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        outgoing=True,
        sender_name=sender_name,
        sender_key=sender_key,
        compression=compression,
        send_attempts=send_attempts,
        send_state=send_state,
        is_reaction=is_reaction,
    )
    if msg_id is None:
        return None

    message = await build_stored_outgoing_channel_message(
        message_id=msg_id,
        conversation_key=conversation_key,
        text=text,
        sender_timestamp=sender_timestamp,
        received_at=received_at,
        sender_name=sender_name,
        sender_key=sender_key,
        channel_name=channel_name,
        compression=compression,
        send_attempts=send_attempts,
        send_state=send_state,
        message_repository=message_repository,
    )
    if broadcast and not is_reaction:
        broadcast_message(message=message, broadcast_fn=broadcast_fn)
    return message
