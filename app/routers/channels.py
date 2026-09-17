import asyncio
import logging

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.channel_constants import (
    PUBLIC_CHANNEL_KEY,
    PUBLIC_CHANNEL_NAME,
    hashtag_channel_key,
    is_public_channel_key,
    is_public_channel_name,
)
from app.models import Channel, ChannelDetail, ChannelMessageCounts, ChannelTopSender
from app.region_scope import UNSCOPED_OVERRIDE_MARKER, is_unscoped, normalize_region_scope
from app.repository import ChannelRepository, MessageRepository
from app.services.historical_decrypt import ChannelTarget, submit_channel_sweep
from app.services.radio_runtime import radio_runtime as radio_manager
from app.websocket import broadcast_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/channels", tags=["channels"])


def _broadcast_channel_update(channel: Channel) -> None:
    broadcast_event("channel", channel.model_dump())


class CreateChannelRequest(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    key: str | None = Field(
        default=None,
        description="Channel key as hex string (32 chars = 16 bytes). If omitted or name starts with #, key is derived from name hash.",
    )


class BulkCreateHashtagChannelsRequest(BaseModel):
    channel_names: list[str] = Field(
        min_length=1,
        description="List of hashtag room names. Leading # is optional per entry.",
    )
    try_historical: bool = Field(
        default=False,
        description="Attempt one background historical decrypt sweep for the newly added rooms.",
    )


class BulkCreateHashtagChannelsResponse(BaseModel):
    created_channels: list[Channel]
    existing_count: int
    invalid_names: list[str]
    decrypt_started: bool = False
    decrypt_total_packets: int = 0
    message: str


class ChannelFloodScopeOverrideRequest(BaseModel):
    flood_scope_override: str = Field(
        description=(
            "Tri-state channel override. Blank clears the override (inherit the global "
            "scope); '*' forces unscoped/plain flood even when a global region is set; "
            "any other value scopes the channel to that region. Note the deliberate "
            "asymmetry vs. the send layer: here blank means 'inherit', so an explicit "
            "unscoped request must use '*'."
        )
    )


class ChannelPathHashModeOverrideRequest(BaseModel):
    path_hash_mode_override: int | None = Field(
        default=None,
        ge=0,
        le=2,
        description="Path hash mode override (0=1-byte, 1=2-byte, 2=3-byte, null = use radio default)",
    )


def _derive_channel_identity(
    requested_name: str,
    request_key: str | None = None,
) -> tuple[str, str, bool]:
    is_hashtag = requested_name.startswith("#")

    if is_public_channel_name(requested_name):
        if request_key:
            try:
                key_bytes = bytes.fromhex(request_key)
                if len(key_bytes) != 16:
                    raise HTTPException(
                        status_code=400,
                        detail="Channel key must be exactly 16 bytes (32 hex chars)",
                    )
            except ValueError:
                raise HTTPException(status_code=400, detail="Invalid hex string for key") from None
            if key_bytes.hex().upper() != PUBLIC_CHANNEL_KEY:
                raise HTTPException(
                    status_code=400,
                    detail=f'"{PUBLIC_CHANNEL_NAME}" must use the canonical Public key',
                )
        return PUBLIC_CHANNEL_KEY, PUBLIC_CHANNEL_NAME, False

    if request_key and not is_hashtag:
        try:
            key_bytes = bytes.fromhex(request_key)
            if len(key_bytes) != 16:
                raise HTTPException(
                    status_code=400, detail="Channel key must be exactly 16 bytes (32 hex chars)"
                )
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid hex string for key") from None
        key_hex = key_bytes.hex().upper()
        if is_public_channel_key(key_hex):
            raise HTTPException(
                status_code=400,
                detail=f'The canonical Public key may only be used for "{PUBLIC_CHANNEL_NAME}"',
            )
        return key_hex, requested_name, False

    return hashtag_channel_key(requested_name), requested_name, is_hashtag


def _normalize_bulk_hashtag_name(name: str) -> str | None:
    trimmed = name.strip()
    if not trimmed:
        return None
    normalized = trimmed.lstrip("#").strip()
    if not normalized:
        return None
    # Hashtag channel names are hashed verbatim (matching meshcore_py / meshcore-cli /
    # meshcore.js), so any character is permitted — '&', capitals, accents, etc. all map
    # to a valid SHA256-derived key. Character normalization (lowercasing / charset
    # restriction) is a client-side display choice, applied by the caller before submit.
    # The on-radio name field holds 32 UTF-8 bytes including the leading '#', so cap there
    # to keep the stored label and the derived key in sync across clients.
    if len(f"#{normalized}".encode()) > 32:
        return None
    return f"#{normalized}"


@router.get("", response_model=list[Channel])
async def list_channels() -> list[Channel]:
    """List all channels from the database."""
    return await ChannelRepository.get_all()


@router.get("/{key}/detail", response_model=ChannelDetail)
async def get_channel_detail(key: str) -> ChannelDetail:
    """Get comprehensive channel profile data with message statistics."""
    channel = await ChannelRepository.get_by_key(key)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    stats = await MessageRepository.get_channel_stats(channel.key)

    return ChannelDetail(
        channel=channel,
        message_counts=ChannelMessageCounts(**stats["message_counts"]),
        first_message_at=stats["first_message_at"],
        unique_sender_count=stats["unique_sender_count"],
        top_senders_24h=[ChannelTopSender(**s) for s in stats["top_senders_24h"]],
        path_hash_width_24h=stats["path_hash_width_24h"],
    )


@router.post("", response_model=Channel)
async def create_channel(request: CreateChannelRequest) -> Channel:
    """Create a channel in the database.

    With resident channels on (the default) the new channel is also pinned into
    a free radio slot, best-effort, so the firmware can decrypt and queue its
    messages as a fallback. Otherwise -- or when no slot is free -- it is loaded
    on the radio at send time only (see messages.py send_channel_message).
    """
    requested_name = request.name
    key_hex, channel_name, is_hashtag = _derive_channel_identity(requested_name, request.key)

    logger.info("Creating channel %s: %s (hashtag=%s)", key_hex, channel_name, is_hashtag)

    # Store in database only - radio sync happens at send time
    await ChannelRepository.upsert(
        key=key_hex,
        name=channel_name,
        is_hashtag=is_hashtag,
        on_radio=False,
    )

    stored = await ChannelRepository.get_by_key(key_hex)
    if stored is None:
        raise HTTPException(status_code=500, detail="Channel was created but could not be reloaded")

    _broadcast_channel_update(stored)
    _schedule_resident_slot_update(add=stored)
    return stored


def _schedule_resident_slot_update(
    *,
    add: Channel | None = None,
    remove_key: str | None = None,
) -> None:
    """Pin or unpin a channel on the radio in the background.

    Never blocks the HTTP response on the radio. A removal drops the in-memory
    mapping *synchronously*, so from this moment the slot is treated as scratch
    and a queued message for it can no longer resolve to the deleted channel;
    the radio-side clear then waits its turn for the operation lock. A
    disconnected radio leaves the slot for the next full sync, which rebuilds
    the resident set from the DB anyway.
    """
    from app.config import settings as app_settings
    from app.radio import RadioOperationError

    if not app_settings.resident_channels_enabled:
        return

    removed_slot: int | None = None
    if remove_key is not None:
        removed_slot = radio_manager.remove_resident_channel(remove_key)
        if removed_slot is None:
            remove_key = None

    if not radio_manager.is_connected or (add is None and remove_key is None):
        return

    async def _run() -> None:
        from app.radio_sync import add_resident_channel_to_radio, remove_resident_channel_from_radio

        try:
            async with radio_manager.radio_operation(
                "resident_channel_update", blocking=True
            ) as mc:
                if add is not None:
                    await add_resident_channel_to_radio(mc, add)
                if remove_key is not None:
                    await remove_resident_channel_from_radio(mc, remove_key, slot=removed_slot)
        except RadioOperationError as exc:
            logger.debug("Resident slot update skipped: %s", exc)
        except Exception:
            logger.warning("Resident slot update failed", exc_info=True)

    asyncio.create_task(_run())


@router.post("/bulk-hashtag", response_model=BulkCreateHashtagChannelsResponse)
async def bulk_create_hashtag_channels(
    request: BulkCreateHashtagChannelsRequest,
    response: Response,
) -> BulkCreateHashtagChannelsResponse:
    created_channels: list[Channel] = []
    existing_count = 0
    invalid_names: list[str] = []
    decrypt_started = False
    decrypt_total_packets = 0
    decrypt_targets: list[ChannelTarget] = []

    for raw_name in request.channel_names:
        normalized_name = _normalize_bulk_hashtag_name(raw_name)
        if normalized_name is None:
            invalid_names.append(raw_name)
            continue

        key_hex, channel_name, is_hashtag = _derive_channel_identity(normalized_name)
        existing = await ChannelRepository.get_by_key(key_hex)
        if existing is not None:
            existing_count += 1
            continue

        await ChannelRepository.upsert(
            key=key_hex,
            name=channel_name,
            is_hashtag=is_hashtag,
            on_radio=False,
        )
        stored = await ChannelRepository.get_by_key(key_hex)
        if stored is None:
            raise HTTPException(
                status_code=500,
                detail="Channel was created but could not be reloaded",
            )

        created_channels.append(stored)
        decrypt_targets.append(
            ChannelTarget(key_bytes=bytes.fromhex(stored.key), key_hex=stored.key, name=stored.name)
        )
        _broadcast_channel_update(stored)

    if request.try_historical and decrypt_targets:
        label = f"{len(decrypt_targets)} new room{'s' if len(decrypt_targets) != 1 else ''}"
        submission = await submit_channel_sweep(decrypt_targets, label=label)
        decrypt_total_packets = submission.total_packets
        decrypt_started = submission.started
        if decrypt_started:
            response.status_code = status.HTTP_202_ACCEPTED

    message = (
        f"Created {len(created_channels)} room{'s' if len(created_channels) != 1 else ''}"
        if created_channels
        else "No new rooms were added"
    )
    if request.try_historical and decrypt_targets:
        if decrypt_started:
            message += (
                f" and started background decrypt of {decrypt_total_packets} packet"
                f"{'s' if decrypt_total_packets != 1 else ''}"
            )
        else:
            message += "; no undecrypted packets were available"

    return BulkCreateHashtagChannelsResponse(
        created_channels=created_channels,
        existing_count=existing_count,
        invalid_names=invalid_names,
        decrypt_started=decrypt_started,
        decrypt_total_packets=decrypt_total_packets,
        message=message,
    )


@router.post("/{key}/mark-read")
async def mark_channel_read(key: str) -> dict:
    """Mark a channel as read (update last_read_at timestamp)."""
    channel = await ChannelRepository.get_by_key(key)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    updated = await ChannelRepository.update_last_read_at(key)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update read state")

    return {"status": "ok", "key": channel.key}


@router.post("/{key}/flood-scope-override", response_model=Channel)
async def set_channel_flood_scope_override(
    key: str, request: ChannelFloodScopeOverrideRequest
) -> Channel:
    """Set or clear a per-channel flood-scope override."""
    channel = await ChannelRepository.get_by_key(key)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    # Tri-state persisted override:
    #   blank        -> None: clear the override, inherit the global scope
    #   "*" / "0"    -> canonical unscoped marker: force unscoped even over a global
    #   region name  -> "#Region": scope this channel
    # NOTE: at this (channel-override) layer blank means "clear/inherit", so we must
    # check for blank *before* is_unscoped() (which also treats "" as unscoped).
    raw_override = (request.flood_scope_override or "").strip()
    if raw_override == "":
        override: str | None = None
    elif is_unscoped(raw_override):
        override = UNSCOPED_OVERRIDE_MARKER
    else:
        override = normalize_region_scope(raw_override)
    updated = await ChannelRepository.update_flood_scope_override(channel.key, override)
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update flood-scope override")

    refreshed = await ChannelRepository.get_by_key(channel.key)
    if refreshed is None:
        raise HTTPException(status_code=500, detail="Channel disappeared after update")

    broadcast_event("channel", refreshed.model_dump())
    return refreshed


@router.post("/{key}/path-hash-mode-override", response_model=Channel)
async def set_channel_path_hash_mode_override(
    key: str, request: ChannelPathHashModeOverrideRequest
) -> Channel:
    """Set or clear a per-channel path hash mode override."""
    channel = await ChannelRepository.get_by_key(key)
    if not channel:
        raise HTTPException(status_code=404, detail="Channel not found")

    updated = await ChannelRepository.update_path_hash_mode_override(
        channel.key, request.path_hash_mode_override
    )
    if not updated:
        raise HTTPException(status_code=500, detail="Failed to update path-hash-mode override")

    refreshed = await ChannelRepository.get_by_key(channel.key)
    if refreshed is None:
        raise HTTPException(status_code=500, detail="Channel disappeared after update")

    broadcast_event("channel", refreshed.model_dump())
    return refreshed


@router.delete("/{key}")
async def delete_channel(key: str) -> dict:
    """Delete a channel from the database by key.

    A channel pinned in a resident radio slot is unpinned and its slot cleared
    in the background, best-effort; scratch (send-time) slots are left alone.
    """
    if is_public_channel_key(key):
        raise HTTPException(
            status_code=400, detail="The canonical Public channel cannot be deleted"
        )

    logger.info("Deleting channel %s from database", key)
    await ChannelRepository.delete(key)

    broadcast_event("channel_deleted", {"key": key})
    _schedule_resident_slot_update(remove_key=key)

    return {"status": "ok"}
