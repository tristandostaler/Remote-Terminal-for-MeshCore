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


def self_node_identity() -> tuple[str, str] | None:
    """``(public key hex, node name)`` of the connected radio, or None.

    None while the radio has not reported itself yet -- at startup, or between
    reconnects -- which every caller treats as "cannot attribute this", never as
    an error.
    """
    from app.services.radio_runtime import radio_runtime

    try:
        meshcore = radio_runtime.meshcore
        self_info = (meshcore.self_info if meshcore else None) or {}
        public_key = str(self_info.get("public_key") or "")
        if len(public_key) < 4:
            return None
        return public_key, str(self_info.get("name") or "")
    except Exception:
        return None


def self_sender_prefix() -> int | None:
    """This node's 2-byte GRP_DATA sender prefix, or None while it is unknown.

    Every picture this node puts on the air carries it -- including the ones an
    app on the virtual companion node sends, since the app's identity *is* this
    radio's. It is therefore both the echo filter on the RF path and the "this
    one is ours" test on the app path.
    """
    identity = self_node_identity()
    if identity is None:
        return None
    try:
        key = bytes.fromhex(identity[0])
    except ValueError:
        return None
    if len(key) < 2:
        return None
    return ((key[0] & 0xFF) << 8) | (key[1] & 0xFF)


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


LOCAL_MARKER_PREFIXES = (MARKER_PREFIX, MARKER_UNSUPPORTED_PREFIX)
"""Every marker prefix that stands in for media instead of for words.

One definition, because the consumers are scattered: the companion protocol, web
push, MQTT fanout and the bot engine each have to recognise a marker row, and a
second copy of this tuple is how one of them ends up publishing
``aeib:grp:1c1e08f41fd4dd96`` to somebody.
"""


def is_local_marker(text: str | None) -> bool:
    """Whether a stored message body is a marker rather than something someone wrote.

    A marker row is a convention between this server and its own web UI, which
    renders the picture the row points at. Anything that reads a message as text
    -- a push notification body, an MQTT payload, a bot matching a command --
    has to skip it, or it publishes the convention itself.
    """
    return bool(text) and str(text).startswith(LOCAL_MARKER_PREFIXES)


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


TEXT_BLOB_DEDUP_SECONDS = 60.0
"""How long a text blob is remembered so the same one is stored once.

The same GRP_DATA blob reaches :func:`handle_channel_data` from both delivery
paths (raw RF and companion frame 27) and again from a repeater's re-flood, all
within seconds of each other -- the image reassembler absorbs that idempotently
and text needs its own answer. A v2 blob carries no timestamp of its own, so
there is nothing to dedup a repeat against; a minute is far longer than the gap
between the paths and short enough that genuinely re-sending the same words a
little later still lands in the conversation.
"""

MAX_REMEMBERED_TEXT_BLOBS = 256

_recent_text_blobs: dict[tuple[str, int, bytes], float] = {}


def _already_stored_text(key: tuple[str, int, bytes], *, now: float) -> bool:
    """Whether this exact blob was stored moments ago (and remember it if not)."""
    for stale in [
        k for k, seen in _recent_text_blobs.items() if now - seen > TEXT_BLOB_DEDUP_SECONDS
    ]:
        del _recent_text_blobs[stale]
    if key in _recent_text_blobs:
        return True
    _recent_text_blobs[key] = now
    while len(_recent_text_blobs) > MAX_REMEMBERED_TEXT_BLOBS:
        del _recent_text_blobs[min(_recent_text_blobs, key=lambda k: _recent_text_blobs[k])]
    return False


async def _store_text_message(
    parsed: ParsedChannelData,
    *,
    conversation_key: str,
    broadcast_fn=None,
) -> bool:
    """Store a GRP_DATA blob that carries a message rather than media.

    MCO Advanced sends channel messages as GRP_DATA whenever its
    ``channelsSendAsBinary`` setting is on, which is the default, so this is
    ordinary chat traffic and not an edge case: without it every compressed
    message from a current MCO Advanced peer was reported as an unsupported
    payload and dropped.

    The row goes through the same fallback-channel-message writer the ``get_msg``
    drain uses, so dedup, reactions, bots and fanout see it exactly as they would
    the text form. Returns False when the blob is not text this build reads, so
    the caller can fall back to keeping it as undecodable media.
    """
    from app.imaging.aeic.channel_data_text import carries_text, decode_channel_data_text

    if not carries_text(parsed.data_type, parsed.payload):
        return False
    decoded = decode_channel_data_text(parsed.data_type, parsed.payload)
    if decoded is None:
        return False

    now = int(time.time())
    if _already_stored_text(
        (conversation_key.upper(), parsed.data_type, parsed.payload), now=time.time()
    ):
        logger.debug("Skipping a GRP_DATA text blob already stored on %s", conversation_key[:12])
        return True

    from app.repository import ChannelRepository
    from app.services.messages import create_fallback_channel_message

    channel = await ChannelRepository.get_by_key(conversation_key.upper())
    # The v3 container carries the sender's own clock; v2 carries nothing, so
    # arrival time is the best available -- and it is what the dedup index keys
    # on, which is why the same blob is remembered above rather than relying on
    # two arrivals landing in the same second.
    sender_timestamp = decoded.v3.timestamp if decoded.v3 is not None else now
    await create_fallback_channel_message(
        conversation_key=conversation_key,
        message_text=decoded.text,
        sender_timestamp=sender_timestamp,
        received_at=now,
        path=None,
        path_len=parsed.hop_count,
        txt_type=0,
        sender_name=decoded.sender_name or None,
        channel_name=channel.name if channel is not None else None,
        broadcast_fn=broadcast_fn if broadcast_fn is not None else _ignore_broadcast,
    )
    logger.info(
        "Stored an MCMP %s message from GRP_DATA on %s (%d payload bytes)",
        decoded.version,
        conversation_key[:12],
        decoded.payload_bytes,
    )
    return True


def _ignore_broadcast(_event: str, _data) -> None:
    """Broadcast sink for callers that pass none (tests, and the raw-RF path)."""


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
        try:
            stored = await _store_text_message(
                parsed, conversation_key=conversation_key, broadcast_fn=broadcast_fn
            )
        except Exception:
            # This runs on the inbound frame path; a failed write costs a log
            # line, not the link.
            logger.exception("Failed to store a GRP_DATA text message")
            stored = False
        if not stored:
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
    # Every chunk of one image carries the same sender prefix, so the blob that
    # completed it names the sender as well as any other would.
    completing = parse_chunk_blob(parsed.payload)
    try:
        await _store_and_decode(
            bitstream,
            metadata_byte,
            conversation_key=conversation_key,
            sender_prefix=completing.sender_prefix if completing is not None else None,
            broadcast_fn=broadcast_fn,
        )
    except Exception:
        logger.exception("Failed to store a reassembled GRP_DATA image")
    return True


async def _attribute_image(sender_prefix: int | None) -> tuple[str | None, str | None, bool]:
    """``(sender key, sender name, outgoing)`` for a picture from this prefix.

    Three answers, in the order they are worth having:

    * **Ours.** The prefix is this radio's, so the picture left on our identity
      -- which is what an app on the virtual companion node sends, since its
      identity is this radio's. It belongs in the conversation as something we
      sent, exactly as an app's *text* message already is.
    * **A peer we know.** Two bytes is upstream's identity field and is what the
      firmware gives us; resolved against the contact list it names the sender,
      and ``get_by_key_prefix`` declines an ambiguous match rather than guessing.
    * **Nobody we know.** Left unattributed, as before.

    Without this a picture arrived with no sender at all and the UI fell back to
    showing the conversation key -- a channel's shared secret rendered as though
    it were the author.
    """
    if sender_prefix is None:
        return None, None, False
    ours = self_sender_prefix()
    if ours is not None and sender_prefix == ours:
        identity = self_node_identity()
        if identity is not None:
            return identity[0], identity[1] or None, True
        return None, None, True

    from app.repository import ContactRepository

    try:
        contact = await ContactRepository.get_by_key_prefix(f"{sender_prefix:04x}")
    except Exception:
        logger.debug("Could not resolve the sender of a GRP_DATA image", exc_info=True)
        return None, None, False
    if contact is None:
        return None, None, False
    return contact.public_key, contact.name or None, False


async def _store_and_decode(
    bitstream: bytes,
    metadata_byte: int,
    *,
    conversation_key: str,
    sender_prefix: int | None = None,
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
    sender_key, sender_name, outgoing = await _attribute_image(sender_prefix)
    message_id = await MessageRepository.create(
        msg_type="CHAN",
        text=marker_text(key),
        received_at=arrived_at,
        sender_timestamp=arrived_at,
        conversation_key=conversation_key,
        sender_key=sender_key,
        sender_name=sender_name,
        outgoing=outgoing,
    )
    await AeicImageRepository.enforce_cache_limit()
    await AeicImageRepository.create_session(
        key=key,
        message_id=message_id,
        direction="outgoing" if outgoing else "incoming",
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
