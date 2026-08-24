"""Receiving AEIC images that arrive as binary GRP_DATA, the MCO Advanced way.

The text path (:mod:`app.imaging.aeic.ingest`) reassembles ``aei1:`` messages,
which only RemoteTerm sends. This module is the interoperable half: it takes
``RESP_CODE_CHANNEL_DATA_RECV`` (27) frames off the companion link, reassembles
upstream's chunk framing (:mod:`app.imaging.aeic.channel_data`) including its XOR
parity, and lands the result in the same ``aeic_image_sessions`` storage so the
UI renders it identically.

## Why the companion frame and not raw RF

RemoteTerm normally sniffs raw RF and decrypts channel traffic itself
(``decoder.decrypt_group_text``). We deliberately do NOT do that for GRP_DATA.
The firmware has already decrypted these frames and hands them over pre-split
into ``(data_type, payload)``, so taking frame 27 needs no crypto and no
assumption about the on-air plaintext layout -- a layout MCO Advanced never sees
either, and which is therefore not documented in any source we can check. Adding
a raw-RF GRP_DATA decoder would mean guessing where the blob starts inside the
plaintext, and a wrong guess in THIS codec does not throw: it reconstructs a
sharp, plausible, wrong picture.

The cost of that choice is honest and worth stating: this path only sees frames
the *local* radio decrypted, which means the channel must be loaded in one of
the radio's slots. That is the same constraint the firmware already puts on
channel text.

## There is more than one image codec on GRP_DATA

MCO Advanced ships AEIC (``0xAE1C``) *and* MCOimg (``0xFFF0``), and both ride
GRP_DATA. Only AEIC is a codec RemoteTerm has. An MCOimg frame is recognised and
reported as unsupported rather than handed to the AEIC decoder, which would
otherwise turn it confidently into garbage.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.imaging.aeic.channel_data import (
    DATA_TYPE_AEIC_IMAGE,
    DATA_TYPE_MCMP,
    DATA_TYPE_MCO_IMAGE,
    ParsedChannelData,
    PendingImage,
    assemble,
    parse_chunk_blob,
    recover_missing_body,
)

logger = logging.getLogger(__name__)

SESSION_TTL_SECONDS = 600
"""How long a partial image waits for its remaining chunks. Upstream expires
pending images too; ten minutes is far longer than any real chunk spacing and
short enough that an abandoned image cannot pin memory."""

MAX_PENDING_IMAGES = 32
"""Ceiling on partial images held in memory. Each is at most ~2.5 KB of bodies,
so this is a guard against a peer opening thousands of sessions, not a tuning
knob."""

MARKER_PREFIX = "aeib:"
"""Marker text stored as the message body for a binary-transport image.

A GRP_DATA image is NOT a text message -- nothing readable crossed the air -- so
unlike the ``aei1:`` path there is no natural body to keep. The message row
exists purely to give the picture a place in the conversation, and this marker
is what the frontend matches to render it. It is a LOCAL convention and is never
transmitted; do not confuse it with a wire format.
"""


def marker_text(session_key: str) -> str:
    return f"{MARKER_PREFIX}{session_key}"


class ChannelDataReassembler:
    """Collects GRP_DATA chunks until an image is whole (or recoverable)."""

    def __init__(self) -> None:
        self._pending: dict[tuple[str, int, int], PendingImage] = {}

    def _expire(self, now: float) -> None:
        stale = [key for key, e in self._pending.items() if now - e.last_seen > SESSION_TTL_SECONDS]
        for key in stale:
            entry = self._pending.pop(key, None)
            if entry is not None:
                logger.info(
                    "Dropping an incomplete GRP_DATA image %s: %d of %d chunks after %ds",
                    key,
                    len(entry.bodies),
                    entry.total,
                    SESSION_TTL_SECONDS,
                )

    def _enforce_cap(self, protect: tuple[str, int, int]) -> None:
        """Trim to the ceiling, never evicting the chunk we just took.

        Runs AFTER the insert rather than before it: trimming first leaves room
        for one, and the very next line adds it, so the map settles one over the
        cap forever.
        """
        while len(self._pending) > MAX_PENDING_IMAGES:
            oldest = min(
                (k for k in self._pending if k != protect),
                key=lambda k: self._pending[k].first_seen,
                default=None,
            )
            if oldest is None:  # pragma: no cover - cap is far above 1
                return
            self._pending.pop(oldest, None)
            logger.warning("Evicted the oldest pending GRP_DATA image %s (cache full)", oldest)

    def note_chunk(
        self, conversation_key: str, blob: bytes, *, now: float | None = None
    ) -> tuple[bytes, int, bool] | None:
        """Absorb one blob.

        Returns ``(bitstream, metadata_byte, recovered)`` when the image is
        complete, else None. ``recovered`` says parity had to rebuild a chunk,
        which is worth surfacing: it means the mesh dropped a packet and the
        extra parity blob paid for itself.
        """
        moment = time.time() if now is None else now
        chunk = parse_chunk_blob(blob)
        if chunk is None:
            logger.debug("Ignoring a malformed GRP_DATA blob (%d bytes)", len(blob))
            return None

        self._expire(moment)
        key = (conversation_key, chunk.sender_prefix, chunk.img_id)
        entry = self._pending.get(key)
        if entry is None:
            entry = PendingImage(total=chunk.total, first_seen=moment, last_seen=moment)
            self._pending[key] = entry
        elif entry.total != chunk.total:
            # Same sender and image id but a different chunk count: this is a
            # different image reusing the id, not a continuation of this one.
            logger.info("GRP_DATA image %s restarted with a new chunk count; resetting", key)
            entry = PendingImage(total=chunk.total, first_seen=moment, last_seen=moment)
            self._pending[key] = entry
        entry.last_seen = moment
        self._enforce_cap(protect=key)

        if chunk.is_parity:
            entry.parity_body = chunk.body
        else:
            entry.bodies.setdefault(chunk.index, chunk.body)

        recovered = False
        if not entry.is_complete:
            rebuilt = recover_missing_body(entry)
            if rebuilt is None:
                return None
            missing = next(i for i in range(entry.total) if i not in entry.bodies)
            entry.bodies[missing] = rebuilt
            recovered = True

        result = assemble(entry)
        if result is None:
            return None
        self._pending.pop(key, None)
        bitstream, metadata_byte = result
        return bitstream, metadata_byte, recovered


reassembler = ChannelDataReassembler()

_decode_tasks: set[asyncio.Task] = set()


def describe_data_type(data_type: int) -> str:
    if data_type == DATA_TYPE_AEIC_IMAGE:
        return "AEIC image"
    if data_type == DATA_TYPE_MCO_IMAGE:
        return "MCOimg image (codec not supported here)"
    if data_type == DATA_TYPE_MCMP:
        return "MCMP text over GRP_DATA"
    return f"unknown data type 0x{data_type:04X}"


async def handle_channel_data(
    parsed: ParsedChannelData,
    *,
    conversation_key: str,
    broadcast_fn=None,
) -> bool:
    """Absorb one inbound GRP_DATA frame. True if it was an AEIC image chunk.

    Never raises: this runs on the radio event path, and a malformed frame from
    a peer must not take the link down.
    """
    if parsed.data_type != DATA_TYPE_AEIC_IMAGE:
        logger.debug(
            "Ignoring a GRP_DATA frame on channel %d: %s",
            parsed.channel_index,
            describe_data_type(parsed.data_type),
        )
        return False

    try:
        outcome = reassembler.note_chunk(conversation_key, parsed.payload)
    except Exception:
        logger.exception("Failed to absorb a GRP_DATA image chunk")
        return True
    if outcome is None:
        return True

    bitstream, metadata_byte, recovered = outcome
    logger.info(
        "Reassembled a %d-byte AEIC bitstream from GRP_DATA on %s%s",
        len(bitstream),
        conversation_key[:12],
        " (one chunk rebuilt from parity)" if recovered else "",
    )
    try:
        await _store_and_decode(
            bitstream, metadata_byte, conversation_key=conversation_key, broadcast_fn=broadcast_fn
        )
    except Exception:
        logger.exception("Failed to store a reassembled GRP_DATA image")
    return True


async def _store_and_decode(
    bitstream: bytes,
    metadata_byte: int,
    *,
    conversation_key: str,
    broadcast_fn=None,
) -> None:
    """Persist the image as a session anchored to a synthetic message row."""
    from app.imaging.aeic.ingest import _schedule_decode
    from app.imaging.aeic.text_transport import AeicStreamMetadata
    from app.repository import AeicImageRepository, MessageRepository
    from app.repository.aeic_image import inbound_channel_data_session_key

    metadata = AeicStreamMetadata.decode(metadata_byte)
    if metadata is None:
        logger.warning(
            "Dropping a GRP_DATA image with an undecodable metadata byte 0x%02X: the sender "
            "is running a rate or resolution this build cannot decode",
            metadata_byte,
        )
        return

    key = inbound_channel_data_session_key(conversation_key, bitstream)
    message_id = await MessageRepository.create(
        msg_type="CHAN",
        text=marker_text(key),
        received_at=int(time.time()),
        conversation_key=conversation_key,
        sender_key=None,
    )
    await AeicImageRepository.enforce_cache_limit()
    await AeicImageRepository.create_session(
        key=key,
        message_id=message_id,
        direction="incoming",
        conversation_type="CHAN",
        conversation_key=conversation_key,
        peer_public_key=None,
        square_size=metadata.square_size,
        aspect_code=metadata.aspect_code,
        rate_code=metadata.rate_code,
        total_chunks=1,
        state="complete",
    )
    await AeicImageRepository.store_bitstream(key, bitstream)
    if broadcast_fn is not None:
        broadcast_fn(
            "aeic_image_session",
            {
                "session_key": key,
                "message_id": message_id,
                "received": 1,
                "total": 1,
                "state": "complete",
            },
        )
    await _schedule_decode(key, bitstream, broadcast_fn)
