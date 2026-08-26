"""Receiving AEIC images that arrive as binary GRP_DATA, the MCO Advanced way.

The text path (:mod:`app.imaging.aeic.ingest`) reassembles ``aei1:`` messages,
which only RemoteTerm sends. This module is the interoperable half: it takes
``RESP_CODE_CHANNEL_DATA_RECV`` (27) frames off the companion link, reassembles
upstream's chunk framing (:mod:`app.imaging.aeic.channel_data`) including its XOR
parity, and lands the result in the same ``aeic_image_sessions`` storage so the
UI renders it identically.

## Two delivery paths feed this module

Raw RF is the primary one now (``packet_processor._process_group_data`` /
``decoder.decrypt_group_data``), exactly as for channel text: it works on any
firmware whose radio can hear the packet and needs no radio slot. It was
originally rejected because the GRP_DATA plaintext layout was undocumented and a
wrong guess in THIS codec does not throw -- it reconstructs a sharp, plausible,
wrong picture. Both halves of that reasoning have since collapsed: the layout is
now read from the firmware source itself (``BaseChatMesh.cpp``: data_type LE,
one length byte, blob), and the 2-byte HMAC per packet means a wrong key or a
wrong layout yields None, never a wrong picture. What settled it was a real
radio that heard every packet and delivered none of them: its firmware never
queues GRP_DATA for companions, so under the frame-27-only design pictures sent
to a channel simply never appeared.

Frame 27 is kept as the fallback for setups where the RF log is unavailable.
Both paths call :func:`handle_channel_data` on the same reassembler; a chunk
delivered twice -- once per path, or again via a repeater's re-flood -- is
absorbed idempotently (``bodies.setdefault`` before completion,
``_already_finished`` after).

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
    DATA_TYPE_MCO_APP,
    DATA_TYPE_MCO_IMAGE,
    MCO_APP_SUBTYPE_MCMP,
    MCO_APP_SUBTYPE_MCO_IMAGE,
    ParsedChannelData,
    PendingImage,
    assemble,
    mco_app_subtype,
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
        # Completed images, as key -> (finished_at, total). Kept because an image
        # can finish while chunks of it are still arriving, and a late chunk of a
        # forgotten image is indistinguishable from the first chunk of a new one.
        # See :meth:`_already_finished`.
        self._completed: dict[tuple[str, int, int], tuple[float, int]] = {}

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
        for key in [k for k, (at, _t) in self._completed.items() if now - at > SESSION_TTL_SECONDS]:
            del self._completed[key]

    def _already_finished(self, key: tuple[str, int, int], total: int, now: float) -> bool:
        """Whether this chunk belongs to an image that is already done.

        A one-data-chunk image -- upstream's *typical* ft32 size, and two packets
        on the air -- used to complete TWICE. The data chunk completes it, the
        entry is dropped, and then the parity chunk arrives and starts a fresh
        entry in which the single missing body is recoverable from parity alone.
        So it completed again, and the caller minted a second message row and a
        second session: one picture, two identical bubbles. Multi-chunk images
        never showed it, because parity alone cannot rebuild two missing bodies.

        Matching on ``total`` as well as the key is what keeps this from
        swallowing a genuinely different image that reused the id inside the
        window -- a different chunk count means a different picture, which is the
        same signal the reset path in :meth:`note_chunk` already trusts.
        """
        finished = self._completed.get(key)
        if finished is None:
            return False
        at, completed_total = finished
        if completed_total != total:
            return False
        return now - at <= SESSION_TTL_SECONDS

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
        if self._already_finished(key, chunk.total, moment):
            logger.debug("Ignoring a GRP_DATA chunk for image %s, which is already complete", key)
            return None
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
        self._completed[key] = (moment, entry.total)
        while len(self._completed) > MAX_PENDING_IMAGES:
            del self._completed[min(self._completed, key=lambda k: self._completed[k][0])]
        bitstream, metadata_byte = result
        return bitstream, metadata_byte, recovered


reassembler = ChannelDataReassembler()

_decode_tasks: set[asyncio.Task] = set()


def describe_data_type(data_type: int, payload: bytes = b"") -> str:
    """Name what a GRP_DATA frame carries, for a human reading a log.

    ``payload`` is only consulted for :data:`DATA_TYPE_MCO_APP`, whose content is
    an envelope: the type alone says nothing about which codec is inside.
    """
    if data_type == DATA_TYPE_AEIC_IMAGE:
        return "AEIC image"
    if data_type == DATA_TYPE_MCO_IMAGE:
        return "MCOimg image (codec not supported here)"
    if data_type == DATA_TYPE_MCMP:
        return "MCMP text over GRP_DATA"
    if data_type == DATA_TYPE_MCO_APP:
        subtype = mco_app_subtype(payload)
        if subtype is None:
            return "MCO Advanced app data (envelope unreadable)"
        kind, version = subtype
        if kind == MCO_APP_SUBTYPE_MCO_IMAGE:
            return f"MCOimg v{version} image (codec not supported here)"
        if kind == MCO_APP_SUBTYPE_MCMP:
            return f"MCMP v{version} text over GRP_DATA"
        return f"MCO Advanced app data, subtype {kind} v{version}"
    return f"unknown data type 0x{data_type:04X}"


def carries_an_image(data_type: int, payload: bytes = b"") -> bool:
    """Whether a frame we cannot decode was nonetheless somebody's picture.

    This is the difference between a notice worth raising to a person and one
    worth keeping at debug. Text over GRP_DATA is ordinary traffic that arrives
    by other means anyway; an image is a thing someone deliberately sent that
    will never appear, so silence about it is the bug being fixed here.
    """
    if data_type == DATA_TYPE_MCO_IMAGE:
        return True
    if data_type != DATA_TYPE_MCO_APP:
        return False
    subtype = mco_app_subtype(payload)
    return subtype is not None and subtype[0] == MCO_APP_SUBTYPE_MCO_IMAGE


MARKER_UNSUPPORTED_PREFIX = "mediax:"
"""Marker row for media we cannot decode: ``mediax:<arrival id>``.

Deliberately holds only the id, not the codec wording. The wording belongs to the
UI, which can change it without a migration, and an id cannot accidentally contain
``": "`` -- which the client's sender parser would split on, turning the marker
into a sender name and a fragment.
"""


def unsupported_marker_text(media_id: int) -> str:
    return f"{MARKER_UNSUPPORTED_PREFIX}{media_id}"


async def _note_undecodable(
    parsed: ParsedChannelData,
    *,
    conversation_key: str,
    broadcast_fn=None,
    now: float | None = None,
) -> None:
    """Keep and announce a frame this build cannot turn into a picture.

    Three things happen for an image, and none of them used to. The payload is
    stored verbatim so a decoder added later can still read a picture received
    today; the first blob of an arrival mints a marker row so the conversation
    shows that something came in and why it cannot be shown; and it is logged at
    INFO, because the frame is well formed and correctly refused, and the only
    failure was that it happened in silence.

    Text stays at debug and is not stored. It is ordinary channel traffic that
    also arrives decoded by other means, so keeping it would be hoarding, not
    recovery.
    """
    from app.repository import UnsupportedMediaRepository

    description = describe_data_type(parsed.data_type, parsed.payload)
    if not carries_an_image(parsed.data_type, parsed.payload):
        logger.debug(
            "Ignoring a GRP_DATA frame on channel %d: %s", parsed.channel_index, description
        )
        return

    moment = int(time.time() if now is None else now)
    try:
        media_id, started_new = await UnsupportedMediaRepository.append_blob(
            conversation_key=conversation_key,
            data_type=parsed.data_type,
            codec_label=description,
            payload=parsed.payload,
            now=moment,
        )
    except Exception:
        # Never take the radio path down over storage. The frame is already lost
        # to us either way; the log line is what is left.
        logger.exception("Could not store an undecodable GRP_DATA image")
        return

    if not started_new:
        logger.debug(
            "Kept another blob of undecodable GRP_DATA image %d on channel %d: %s",
            media_id,
            parsed.channel_index,
            description,
        )
        return

    logger.info(
        "Keeping an image received on channel %d that this build cannot decode: %s. "
        "It is stored, and will open if support for that codec is added.",
        parsed.channel_index,
        description,
    )
    await _place_unsupported_marker(
        media_id, conversation_key=conversation_key, received_at=moment, broadcast_fn=broadcast_fn
    )


async def _place_unsupported_marker(
    media_id: int, *, conversation_key: str, received_at: int, broadcast_fn=None
) -> None:
    """Give a kept-but-undecodable arrival its place in the conversation."""
    from app.repository import MessageRepository, UnsupportedMediaRepository

    try:
        message_id = await MessageRepository.create(
            msg_type="CHAN",
            text=unsupported_marker_text(media_id),
            received_at=received_at,
            conversation_key=conversation_key,
            sender_key=None,
        )
        if message_id is None:
            return
        await UnsupportedMediaRepository.bind_message(media_id, message_id)
    except Exception:
        logger.exception("Could not place a marker for an undecodable GRP_DATA image")
        return
    if broadcast_fn is not None:
        await _announce_marker_row(message_id, broadcast_fn)


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
        await _note_undecodable(
            parsed, conversation_key=conversation_key, broadcast_fn=broadcast_fn
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
    arrived_at = int(time.time())
    # The arrival time is what makes two sends of the SAME picture two rows.
    #
    # The session key is content-addressed on purpose -- one stored bitstream and
    # one decoded PNG however many times a picture arrives. But the CHAN dedup
    # index covers (type, conversation_key, text, sender_timestamp), and the
    # marker text is that same content hash, so with no timestamp every resend
    # collapsed onto the first row: `create` returned None, nothing was announced,
    # and re-sending a photo to a channel could never produce a bubble -- for
    # ever, since the row has no expiry. That is precisely what someone does when
    # a picture does not show up, so the retry was guaranteed to fail too.
    #
    # Duplicate suppression does not live here. A repeater's re-flood carries the
    # same sender_prefix + img_id, which `_already_finished` drops before it ever
    # reaches this function, so anything arriving here is a distinct send. Keeping
    # both layers guessing at "same picture" is what split the notion of identity
    # in the first place.
    message_id = await MessageRepository.create(
        msg_type="CHAN",
        text=marker_text(key),
        received_at=arrived_at,
        sender_timestamp=arrived_at,
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
        await _announce_marker_row(message_id, broadcast_fn)
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


async def _announce_marker_row(message_id: int | None, broadcast_fn) -> None:
    """Push a freshly minted marker row to the UI as an ordinary message.

    Without this the row reached the database and stopped there. The only event
    this path emitted was ``aeic_image_session``, which NOTHING on the client
    handles -- the bubbles poll over HTTP instead -- so an image received on a
    channel appeared no earlier than the next time the conversation was fetched.
    Sitting in the channel it was sent to, you saw nothing at all: the reported
    symptom, and it made a working transfer indistinguishable from a dropped one.

    A marker row is a real row in a real conversation, so it is announced the way
    every other inbound message is.
    """
    from app.repository import MessageRepository
    from app.services.messages import broadcast_message

    if message_id is None:
        # No row to announce. `create` returns None when the write did not produce
        # one, and there is nothing for a client to render in that case.
        return
    message = await MessageRepository.get_by_id(message_id)
    if message is None:  # pragma: no cover - the row was just written
        return
    broadcast_message(message=message, broadcast_fn=broadcast_fn)
