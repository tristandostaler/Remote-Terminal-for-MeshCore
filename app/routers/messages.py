import logging
import time

from fastapi import APIRouter, HTTPException, Query

from app.compression import encode_outbound
from app.event_handlers import track_pending_ack
from app.models import (
    CONTACT_TYPE_ROOM,
    McmpEstimateRequest,
    McmpEstimateResponse,
    Message,
    MessageActionResponse,
    MessagesAroundResponse,
    ReactToMessageRequest,
    ResendChannelMessageResponse,
    SendChannelMessageRequest,
    SendDirectMessageRequest,
)
from app.reactions import (
    ReactionInfo,
    apply_reaction,
    emoji_to_index_hex,
    encode_reaction,
    reaction_hash_for_message,
)
from app.repository import AmbiguousPublicKeyPrefixError, AppSettingsRepository, MessageRepository
from app.services.message_send import (
    SCOPE_UNSET,
    cancel_message_send,
    resend_channel_message_record,
    retry_direct_message_record,
    send_channel_message_to_channel,
    send_direct_message_to_contact,
)
from app.services.radio_runtime import radio_runtime as radio_manager
from app.websocket import broadcast_error, broadcast_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/messages", tags=["messages"])


@router.post("/mcmp-estimate", response_model=McmpEstimateResponse)
async def estimate_mcmp(request: McmpEstimateRequest) -> McmpEstimateResponse:
    """Compressed wire size of a draft message, for the live compose counter.

    When a conversation has MCMP enabled the frontend shows this byte count
    against the packet budget instead of the raw length, so the effective
    character capacity grows as compressible text is typed. Pure computation --
    no radio involved.
    """
    # Timestamp only affects v3 (fixed 4 bytes), so a placeholder is fine for the
    # size estimate.
    encoded = encode_outbound(request.text, version=request.version, timestamp=0)
    return McmpEstimateResponse(
        wire_bytes=len(encoded.encode("utf-8")),
        compressed=encoded != request.text,
    )


@router.get("/around/{message_id}", response_model=MessagesAroundResponse)
async def get_messages_around(
    message_id: int,
    type: str | None = Query(default=None, description="Filter by type: PRIV or CHAN"),
    conversation_key: str | None = Query(default=None, description="Filter by conversation key"),
    context: int = Query(default=100, ge=1, le=500, description="Number of messages before/after"),
) -> MessagesAroundResponse:
    """Get messages around a specific message for jump-to-message navigation."""
    settings = await AppSettingsRepository.get()
    blocked_keys = settings.blocked_keys or None
    blocked_names = settings.blocked_names or None
    messages, has_older, has_newer = await MessageRepository.get_around(
        message_id=message_id,
        msg_type=type,
        conversation_key=conversation_key,
        context_size=context,
        blocked_keys=blocked_keys,
        blocked_names=blocked_names,
    )
    return MessagesAroundResponse(messages=messages, has_older=has_older, has_newer=has_newer)


@router.get("", response_model=list[Message])
async def list_messages(
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    type: str | None = Query(default=None, description="Filter by type: PRIV or CHAN"),
    conversation_key: str | None = Query(
        default=None, description="Filter by conversation key (channel key or contact pubkey)"
    ),
    before: int | None = Query(
        default=None, description="Cursor: received_at of last seen message"
    ),
    before_id: int | None = Query(default=None, description="Cursor: id of last seen message"),
    after: int | None = Query(
        default=None, description="Forward cursor: received_at of last seen message"
    ),
    after_id: int | None = Query(
        default=None, description="Forward cursor: id of last seen message"
    ),
    q: str | None = Query(default=None, description="Full-text search query"),
) -> list[Message]:
    """List messages from the database."""
    settings = await AppSettingsRepository.get()
    blocked_keys = settings.blocked_keys or None
    blocked_names = settings.blocked_names or None
    return await MessageRepository.get_all(
        limit=limit,
        offset=offset,
        msg_type=type,
        conversation_key=conversation_key,
        before=before,
        before_id=before_id,
        after=after,
        after_id=after_id,
        q=q,
        blocked_keys=blocked_keys,
        blocked_names=blocked_names,
    )


@router.post("/direct", response_model=Message)
async def send_direct_message(request: SendDirectMessageRequest) -> Message:
    """Send a direct message to a contact."""
    radio_manager.require_connected()

    # First check our database for the contact
    from app.repository import ContactRepository

    try:
        db_contact = await ContactRepository.get_by_key_or_prefix(request.destination)
    except AmbiguousPublicKeyPrefixError as err:
        sample = ", ".join(key[:12] for key in err.matches[:2])
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ambiguous destination key prefix '{err.prefix}'. "
                f"Use a full 64-character public key. Matching contacts: {sample}"
            ),
        ) from err
    if not db_contact:
        raise HTTPException(
            status_code=404, detail=f"Contact not found in database: {request.destination}"
        )
    if len(db_contact.public_key) < 64:
        raise HTTPException(
            status_code=409,
            detail="Cannot send to an unresolved prefix-only contact until a full key is known",
        )

    return await send_direct_message_to_contact(
        contact=db_contact,
        text=request.text,
        radio_manager=radio_manager,
        broadcast_fn=broadcast_event,
        track_pending_ack_fn=track_pending_ack,
        now_fn=time.time,
        message_repository=MessageRepository,
        contact_repository=ContactRepository,
    )


# Preferred first radio slot used for sending channel messages.
# The send service may reuse/load other app-managed slots depending on transport
# and session cache state.
TEMP_RADIO_SLOT = 0


@router.post("/channel", response_model=Message)
async def send_channel_message(request: SendChannelMessageRequest) -> Message:
    """Send a message to a channel."""
    radio_manager.require_connected()

    # Get channel info from our database
    from app.repository import ChannelRepository

    db_channel = await ChannelRepository.get_by_key(request.channel_key)
    if not db_channel:
        raise HTTPException(
            status_code=404, detail=f"Channel {request.channel_key} not found in database"
        )

    # Convert channel key hex to bytes
    try:
        key_bytes = bytes.fromhex(request.channel_key)
    except ValueError:
        raise HTTPException(
            status_code=400, detail=f"Invalid channel key format: {request.channel_key}"
        ) from None

    # None field = no per-send override (fall back to the channel's persisted
    # override). An explicit string (including "" for unscoped) overrides it.
    flood_scope_override = (
        SCOPE_UNSET if request.flood_scope_override is None else request.flood_scope_override
    )

    return await send_channel_message_to_channel(
        channel=db_channel,
        channel_key_upper=request.channel_key.upper(),
        key_bytes=key_bytes,
        text=request.text,
        radio_manager=radio_manager,
        broadcast_fn=broadcast_event,
        error_broadcast_fn=broadcast_error,
        now_fn=time.time,
        temp_radio_slot=TEMP_RADIO_SLOT,
        flood_scope_override=flood_scope_override,
        message_repository=MessageRepository,
    )


@router.post("/{message_id}/react", response_model=Message)
async def react_to_message(message_id: int, request: ReactToMessageRequest) -> Message:
    """Send a MeshCore Open Advanced compatible emoji reaction to a message.

    Transmits ``r:HHHH:II`` (the target hash and the emoji's table index) as an
    ordinary channel/DM message, then attaches the emoji to the target row and
    broadcasts a ``message_reaction`` event. In 1:1 conversations only received
    messages can be reacted to -- the peer's client matches incoming reactions
    against its own outgoing messages, so a reaction to our own bubble could
    never land anywhere (MCO Advanced has the same rule).
    """
    radio_manager.require_connected()

    msg = await MessageRepository.get_by_id(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    if msg.is_reaction:
        raise HTTPException(status_code=400, detail="Cannot react to a reaction")
    if msg.sender_timestamp is None:
        raise HTTPException(
            status_code=400, detail="Message has no sender timestamp; reactions cannot address it"
        )

    emoji_index = emoji_to_index_hex(request.emoji)
    if emoji_index is None:
        raise HTTPException(
            status_code=400,
            detail=(
                "Emoji is not in the MeshCore Open reaction table; "
                "only listed emojis can ride the wire"
            ),
        )

    from app.repository import ChannelRepository, ContactRepository

    is_room = False
    contact = None
    if msg.type == "PRIV":
        contact = await ContactRepository.get_by_key(msg.conversation_key.lower())
        if not contact:
            raise HTTPException(
                status_code=404, detail=f"Contact not found in database: {msg.conversation_key}"
            )
        if len(contact.public_key) < 64:
            raise HTTPException(
                status_code=409,
                detail="Cannot react in a prefix-only conversation until a full key is known",
            )
        is_room = contact.type == CONTACT_TYPE_ROOM
        if not is_room and msg.outgoing:
            raise HTTPException(
                status_code=400,
                detail="In direct chats you can only react to messages you received",
            )

    our_name: str | None = None
    mc = radio_manager.meshcore
    if mc and mc.self_info:
        our_name = mc.self_info.get("name") or None

    target_hash = reaction_hash_for_message(msg, is_room=is_room, our_name=our_name)
    if target_hash is None:
        raise HTTPException(status_code=400, detail="Message cannot be hashed for a reaction")
    reaction_text = encode_reaction(target_hash, emoji_index)

    if msg.type == "CHAN":
        db_channel = await ChannelRepository.get_by_key(msg.conversation_key)
        if not db_channel:
            raise HTTPException(
                status_code=404, detail=f"Channel {msg.conversation_key} not found in database"
            )
        try:
            key_bytes = bytes.fromhex(msg.conversation_key)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"Invalid channel key format: {msg.conversation_key}"
            ) from None
        await send_channel_message_to_channel(
            channel=db_channel,
            channel_key_upper=msg.conversation_key.upper(),
            key_bytes=key_bytes,
            text=reaction_text,
            radio_manager=radio_manager,
            broadcast_fn=broadcast_event,
            error_broadcast_fn=broadcast_error,
            now_fn=time.time,
            temp_radio_slot=TEMP_RADIO_SLOT,
            message_repository=MessageRepository,
        )
    else:
        await send_direct_message_to_contact(
            contact=contact,
            text=reaction_text,
            radio_manager=radio_manager,
            broadcast_fn=broadcast_event,
            track_pending_ack_fn=track_pending_ack,
            now_fn=time.time,
            message_repository=MessageRepository,
            contact_repository=ContactRepository,
        )

    updated = await apply_reaction(
        msg_type=msg.type,
        conversation_key=msg.conversation_key,
        reaction=ReactionInfo(target_hash=target_hash, emoji=request.emoji),
        reactor_is_self=True,
        broadcast_fn=broadcast_event,
        fallback_target=msg,
    )
    return updated or msg


RESEND_WINDOW_SECONDS = 30


async def _resend_channel_message(
    msg: Message, *, new_timestamp: bool
) -> ResendChannelMessageResponse:
    """Validate and perform a channel resend. Shared by the resend and retry routes."""
    from app.imaging.aeic.channel_data_ingest import is_local_marker
    from app.repository import ChannelRepository

    if not msg.outgoing:
        raise HTTPException(status_code=400, detail="Can only resend outgoing messages")

    if msg.type != "CHAN":
        raise HTTPException(status_code=400, detail="Can only resend channel messages")

    # A picture's row holds a local marker, not the picture and not any text that
    # was ever on the air. Resending it transmits this server's own bookkeeping
    # -- "aeib:grp:1c1e08f41fd4dd96" arriving as a message in somebody's app,
    # which is exactly what a resend of a photo bubble did. Send the picture
    # again from the composer instead; that goes out as image data.
    if is_local_marker(msg.text):
        raise HTTPException(
            status_code=400,
            detail="This message is a picture, not text; send the image again instead",
        )

    if msg.sender_timestamp is None:
        raise HTTPException(status_code=400, detail="Message has no timestamp")

    # Byte-perfect resend enforces the 30s window; new-timestamp resend does not
    if not new_timestamp:
        elapsed = int(time.time()) - msg.sender_timestamp
        if elapsed > RESEND_WINDOW_SECONDS:
            raise HTTPException(status_code=400, detail="Resend window has expired (30 seconds)")

    db_channel = await ChannelRepository.get_by_key(msg.conversation_key)
    if not db_channel:
        raise HTTPException(status_code=404, detail=f"Channel {msg.conversation_key} not found")

    return await resend_channel_message_record(
        message=msg,
        channel=db_channel,
        new_timestamp=new_timestamp,
        radio_manager=radio_manager,
        broadcast_fn=broadcast_event,
        error_broadcast_fn=broadcast_error,
        now_fn=time.time,
        temp_radio_slot=TEMP_RADIO_SLOT,
        message_repository=MessageRepository,
    )


@router.post(
    "/channel/{message_id}/resend",
    response_model=ResendChannelMessageResponse,
    response_model_exclude_none=True,
)
async def resend_channel_message(
    message_id: int,
    new_timestamp: bool = Query(default=False),
) -> ResendChannelMessageResponse:
    """Resend a channel message.

    When new_timestamp=False (default): byte-perfect resend using the original timestamp.
    Only allowed within 30 seconds of the original send.

    When new_timestamp=True: resend with a fresh timestamp so repeaters treat it as a
    new packet. Creates a new message row in the database. No time window restriction.
    """
    radio_manager.require_connected()

    msg = await MessageRepository.get_by_id(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    return await _resend_channel_message(msg, new_timestamp=new_timestamp)


async def _load_message(message_id: int) -> Message:
    msg = await MessageRepository.get_by_id(message_id)
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    return msg


@router.post("/{message_id}/cancel", response_model=MessageActionResponse)
async def cancel_message(message_id: int) -> MessageActionResponse:
    """Stop retransmitting an outgoing message.

    Only the attempts not yet made can be stopped -- whatever is already on air
    is gone. Cancelling a send that had already finished is not an error: the
    message is marked cancelled either way, which is the state the caller asked
    for.
    """
    msg = await _load_message(message_id)
    if not msg.outgoing:
        raise HTTPException(status_code=400, detail="Can only cancel outgoing messages")

    stopped = await cancel_message_send(
        message=msg,
        broadcast_fn=broadcast_event,
        message_repository=MessageRepository,
    )
    return MessageActionResponse(
        status="ok",
        message_id=message_id,
        message=await MessageRepository.get_by_id(message_id),
        stopped_pending_sends=stopped,
    )


@router.post("/{message_id}/retry", response_model=MessageActionResponse)
async def retry_message(
    message_id: int,
    new_timestamp: bool = Query(
        default=False,
        description=(
            "Channel messages only: send under a fresh timestamp, which repeaters treat "
            "as a new packet and which creates a new message row. Ignored for direct "
            "messages, where reusing the timestamp is what makes the resend a retry "
            "rather than a duplicate."
        ),
    ),
) -> MessageActionResponse:
    """Retransmit an outgoing message.

    Direct messages go out byte-identical under their original timestamp and
    restart their retry run under the current attempt cap. Channel messages reuse
    the existing resend machinery, including its 30-second byte-perfect window.
    """
    radio_manager.require_connected()

    msg = await _load_message(message_id)
    if not msg.outgoing:
        raise HTTPException(status_code=400, detail="Can only retry outgoing messages")

    if msg.type == "CHAN":
        resend = await _resend_channel_message(msg, new_timestamp=new_timestamp)
        return MessageActionResponse(
            status=resend.status,
            message_id=resend.message_id,
            message=resend.message or await MessageRepository.get_by_id(message_id),
        )

    from app.repository import ContactRepository

    contact = await ContactRepository.get_by_key_or_prefix(msg.conversation_key)
    if not contact:
        raise HTTPException(
            status_code=404, detail=f"Contact not found in database: {msg.conversation_key[:12]}"
        )

    await retry_direct_message_record(
        message=msg,
        contact=contact,
        radio_manager=radio_manager,
        broadcast_fn=broadcast_event,
        track_pending_ack_fn=track_pending_ack,
        message_repository=MessageRepository,
    )
    return MessageActionResponse(
        status="ok",
        message_id=message_id,
        message=await MessageRepository.get_by_id(message_id),
    )


@router.delete("/{message_id}", response_model=MessageActionResponse)
async def delete_message(message_id: int) -> MessageActionResponse:
    """Remove a message from the conversation, cancelling any pending sends first.

    Local only: the mesh has no unsend, so this drops our copy and stops us
    transmitting it again. Anything already delivered stays delivered.
    """
    msg = await _load_message(message_id)

    stopped = False
    if msg.outgoing:
        stopped = await cancel_message_send(
            message=msg,
            broadcast_fn=broadcast_event,
            message_repository=MessageRepository,
        )

    await MessageRepository.delete_by_id(message_id)
    broadcast_event("message_deleted", {"message_id": message_id})
    logger.info("Deleted message %d from conversation history", message_id)
    return MessageActionResponse(
        status="ok",
        message_id=message_id,
        stopped_pending_sends=stopped,
    )
