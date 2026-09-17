import logging
from hashlib import sha256
from sqlite3 import OperationalError

import aiosqlite
from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.database import db
from app.decoder import parse_packet
from app.keystore import get_private_key, has_private_key
from app.models import (
    DecryptSweepStatus,
    RawPacketDecryptedInfo,
    RawPacketDetail,
)
from app.region_resolver import resolve_region
from app.repository import (
    AppSettingsRepository,
    ChannelRepository,
    ContactRepository,
    MessageRepository,
    RawPacketRepository,
)
from app.services.historical_decrypt import (
    ChannelTarget,
    ContactTarget,
    SweepSubmission,
    get_status,
    submit_channel_sweep,
    submit_contact_sweep,
)
from app.services.messages import backfill_message_regions

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/packets", tags=["packets"])


class DecryptRequest(BaseModel):
    key_type: str = Field(description="Type of key: 'channel' or 'contact'")
    channel_key: str | None = Field(
        default=None, description="Channel key as hex (16 bytes = 32 chars)"
    )
    channel_name: str | None = Field(
        default=None, description="Channel name (for hashtag channels, key derived from name)"
    )
    # Fields for contact (DM) decryption
    private_key: str | None = Field(
        default=None,
        description="Our private key as hex (64 bytes = 128 chars, Ed25519 seed + pubkey)",
    )
    contact_public_key: str | None = Field(
        default=None, description="Contact's public key as hex (32 bytes = 64 chars)"
    )


class DecryptResult(BaseModel):
    started: bool
    total_packets: int
    message: str


def _bad_request(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


@router.get("/undecrypted/count")
async def get_undecrypted_count() -> dict:
    """Get the count of undecrypted packets."""
    count = await RawPacketRepository.get_undecrypted_count()
    return {"count": count}


@router.post("/region-backfill")
async def backfill_regions() -> dict:
    """Re-resolve region scope for stored channel messages that still have a raw packet.

    Region tagging normally happens at ingest, so messages stored before the feature
    (or before a region name was added to ``known_regions``) have no region. This
    recomputes them. Messages whose raw packet was already purged cannot be
    re-evaluated. Clients should refetch the conversation to see updated badges.
    """
    known_regions = (await AppSettingsRepository.get()).known_regions
    return await backfill_message_regions(known_regions)


@router.get("/{packet_id}", response_model=RawPacketDetail)
async def get_raw_packet(packet_id: int) -> RawPacketDetail:
    """Fetch one stored raw packet by row ID for on-demand inspection."""
    packet_row = await RawPacketRepository.get_by_id(packet_id)
    if packet_row is None:
        raise HTTPException(status_code=404, detail="Raw packet not found")

    stored_packet_id, packet_data, packet_timestamp, message_id = packet_row
    packet_info = parse_packet(packet_data)
    payload_type_name = packet_info.payload_type.name if packet_info else "Unknown"

    # Resolve regional flood-scope for transport-routed packets against the
    # current known-region list (we have the raw payload here, so this stays
    # accurate even if the stored message predates a region-list change).
    transport_code: int | None = None
    region: str | None = None
    if packet_info is not None and packet_info.transport_codes is not None:
        transport_code = packet_info.transport_codes[0]
        settings = await AppSettingsRepository.get()
        region = resolve_region(
            int(packet_info.payload_type),
            packet_info.payload,
            transport_code,
            settings.known_regions,
        )

    decrypted_info: RawPacketDecryptedInfo | None = None
    if message_id is not None:
        message = await MessageRepository.get_by_id(message_id)
        if message is not None:
            if message.type == "CHAN":
                channel = await ChannelRepository.get_by_key(message.conversation_key)
                decrypted_info = RawPacketDecryptedInfo(
                    channel_name=channel.name if channel else None,
                    sender=message.sender_name,
                    channel_key=message.conversation_key,
                    contact_key=message.sender_key,
                    sender_timestamp=message.sender_timestamp,
                    message=message.text,
                )
            else:
                decrypted_info = RawPacketDecryptedInfo(
                    sender=message.sender_name,
                    contact_key=message.conversation_key,
                    sender_timestamp=message.sender_timestamp,
                    message=message.text,
                )

    return RawPacketDetail(
        id=stored_packet_id,
        timestamp=packet_timestamp,
        data=packet_data.hex(),
        payload_type=payload_type_name,
        decrypted=message_id is not None,
        decrypted_info=decrypted_info,
        transport_code=transport_code,
        region=region,
    )


async def _channel_target(channel_key: str | None, channel_name: str | None) -> ChannelTarget:
    """Resolve a request's key-or-name into the key bytes and a name to show."""
    if channel_key:
        try:
            key_bytes = bytes.fromhex(channel_key)
        except ValueError:
            raise _bad_request("Invalid hex string for channel key") from None
        if len(key_bytes) != 16:
            raise _bad_request("Channel key must be 16 bytes (32 hex chars)")
        key_hex = channel_key.upper()
    elif channel_name:
        key_bytes = sha256(channel_name.encode("utf-8")).digest()[:16]
        key_hex = key_bytes.hex().upper()
    else:
        raise _bad_request("Must provide channel_key or channel_name")

    stored = await ChannelRepository.get_by_key(key_hex)
    name = stored.name if stored else (channel_name or key_hex[:12])
    return ChannelTarget(key_bytes=key_bytes, key_hex=key_hex, name=name)


async def _contact_target(private_key: str | None, contact_public_key: str | None) -> ContactTarget:
    """Resolve a DM sweep request, falling back to the radio's exported key.

    The browser has no business holding the node's private key, so the button in
    the UI sends only the contact's public key and the server reaches for the
    keystore. An explicit ``private_key`` still wins, for recovering DMs with a
    key this node never exported.
    """
    if private_key:
        try:
            private_key_bytes = bytes.fromhex(private_key)
        except ValueError:
            raise _bad_request("Invalid hex string for private key") from None
        if len(private_key_bytes) != 64:
            raise _bad_request("Private key must be 64 bytes (128 hex chars)")
    elif has_private_key():
        stored_key = get_private_key()
        if stored_key is None:  # pragma: no cover - has_private_key() just said otherwise
            raise _bad_request("Private key is unavailable")
        private_key_bytes = stored_key
    else:
        raise _bad_request(
            "No private_key available. Pass one, or connect a radio whose firmware was "
            "built with ENABLE_PRIVATE_KEY_EXPORT=1."
        )

    if not contact_public_key:
        raise _bad_request("Must provide contact_public_key for contact decryption")
    try:
        public_key_bytes = bytes.fromhex(contact_public_key)
    except ValueError:
        raise _bad_request("Invalid hex string for contact public key") from None
    if len(public_key_bytes) != 32:
        raise _bad_request("Contact public key must be 32 bytes (64 hex chars)")

    public_key_hex = contact_public_key.lower()
    contact = await ContactRepository.get_by_key(public_key_hex)
    return ContactTarget(
        private_key=private_key_bytes,
        public_key_bytes=public_key_bytes,
        public_key_hex=public_key_hex,
        name=contact.name if contact else None,
    )


def _as_result(submission: SweepSubmission, response: Response) -> DecryptResult:
    if submission.started:
        response.status_code = status.HTTP_202_ACCEPTED
    return DecryptResult(
        started=submission.started,
        total_packets=submission.total_packets,
        message=submission.message,
    )


@router.get("/decrypt/status", response_model=DecryptSweepStatus)
async def get_decrypt_status() -> DecryptSweepStatus:
    """Progress of the running sweep, the last one to finish, and the queue depth.

    Declared before ``GET /{packet_id}`` on purpose: that route would otherwise
    claim this path and fail to parse "decrypt" as a row ID.
    """
    return get_status()


@router.post("/decrypt/historical", response_model=DecryptResult)
async def decrypt_historical_packets(request: DecryptRequest, response: Response) -> DecryptResult:
    """Re-try stored packets against one key.

    Queued behind any sweep already running -- they all scan the same table, so
    serializing them costs nothing and keeps the reported progress meaningful.
    """
    if request.key_type == "channel":
        target = await _channel_target(request.channel_key, request.channel_name)
        return _as_result(await submit_channel_sweep([target]), response)

    if request.key_type == "contact":
        target = await _contact_target(request.private_key, request.contact_public_key)
        return _as_result(await submit_contact_sweep(target), response)

    raise _bad_request("key_type must be 'channel' or 'contact'")


@router.post("/decrypt/historical/all-channels", response_model=DecryptResult)
async def decrypt_historical_all_channels(response: Response) -> DecryptResult:
    """Re-try every stored packet against every channel key we know.

    The keys are tried per packet in a single pass, so this costs one scan
    however many rooms are joined. It is the recovery path for a database that
    accumulated packets before their rooms were added -- joining a room only
    sweeps when the operator asks it to.
    """
    channels = await ChannelRepository.get_all()
    targets: list[ChannelTarget] = []
    for channel in channels:
        try:
            key_bytes = bytes.fromhex(channel.key)
        except ValueError:
            logger.warning("Skipping channel %s in sweep: key is not hex", channel.key[:12])
            continue
        if len(key_bytes) != 16:
            logger.warning("Skipping channel %s in sweep: key is not 16 bytes", channel.key[:12])
            continue
        targets.append(
            ChannelTarget(key_bytes=key_bytes, key_hex=channel.key.upper(), name=channel.name)
        )

    if not targets:
        return DecryptResult(started=False, total_packets=0, message="No channel keys to try")

    label = f"All rooms ({len(targets)} key{'s' if len(targets) != 1 else ''})"
    return _as_result(await submit_channel_sweep(targets, label=label), response)


class MaintenanceRequest(BaseModel):
    prune_undecrypted_days: int | None = Field(
        default=None, ge=1, description="Delete undecrypted packets older than this many days"
    )
    purge_linked_raw_packets: bool = Field(
        default=False,
        description="Delete raw packets already linked to a stored message",
    )


class MaintenanceResult(BaseModel):
    packets_deleted: int
    vacuumed: bool


@router.post("/maintenance", response_model=MaintenanceResult)
async def run_maintenance(request: MaintenanceRequest) -> MaintenanceResult:
    """
    Run packet maintenance tasks and reclaim disk space.

    - Optionally deletes undecrypted packets older than the specified number of days
    - Optionally deletes raw packets already linked to stored messages
    - Runs VACUUM to reclaim disk space
    """
    deleted = 0

    if request.prune_undecrypted_days is not None:
        logger.info(
            "Running maintenance: pruning undecrypted packets older than %d days",
            request.prune_undecrypted_days,
        )
        pruned_undecrypted = await RawPacketRepository.prune_old_undecrypted(
            request.prune_undecrypted_days
        )
        deleted += pruned_undecrypted
        logger.info("Deleted %d old undecrypted packets", pruned_undecrypted)

    if request.purge_linked_raw_packets:
        logger.info("Running maintenance: purging raw packets linked to stored messages")
        purged_linked = await RawPacketRepository.purge_linked_to_messages()
        deleted += purged_linked
        logger.info("Deleted %d linked raw packets", purged_linked)

    # Run VACUUM to reclaim space on a dedicated connection.
    # VACUUM requires exclusive access — if the main connection is actively
    # writing (background sync, message processing, etc.) it fails with
    # SQLITE_BUSY. This is expected; we just report vacuumed=False.
    vacuumed = False
    try:
        async with aiosqlite.connect(db.db_path) as vacuum_conn:
            await vacuum_conn.executescript("VACUUM;")
        vacuumed = True
        logger.info("Database vacuumed")
    except OperationalError as e:
        logger.warning("VACUUM skipped (database busy): %s", e)
    except Exception as e:
        logger.error("VACUUM failed unexpectedly: %s", e)

    return MaintenanceResult(packets_deleted=deleted, vacuumed=vacuumed)
