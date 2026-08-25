"""The ``rmt1:`` tunnel -- raw-media payloads as ordinary text messages.

The SAR image (``IE4:``) and voice (``VE3:``) formats move their fragments as
raw MeshCore packets, through ``CMD_SEND_RAW_DATA`` (25). Firmware that does not
implement that command answers ``ERR_CODE_UNSUPPORTED_CMD``, and on such a node
neither format can move a single byte in either direction: not the fetch request
out, not the fragments back. See :mod:`app.services.raw_media`.

This module is the way out. It carries the *same payload bytes* the raw transport
would have carried, basE91-encoded inside ordinary direct messages, so the
receiving end can hand them to the very same
:func:`app.services.raw_media.dispatch_raw_media_payload` a real raw-data push
goes to. Nothing above or below this tunnel knows it is there -- no protocol
module changes, no session or fragment logic changes.

It is the same trick :mod:`app.imaging.aeic.text_transport` plays for AEIC, and
for the same reason: a text message is the one transport every route in this app
already handles.

## What this costs

A great deal of airtime. basE91 expands by ~23% and a header eats 9 characters,
so one 158-byte image fragment does not fit in a 156-byte message -- it becomes
**two**::

    158 B fragment -> 195 chars -> 2 messages
    a 20-fragment picture -> 40 messages -> minutes, not seconds

The raw transport would have moved that picture in 20 packets. So the tunnel is
strictly a fallback, never a preference: it is what you use when the alternative
is a picture that cannot be opened at all. It is also why the per-contact switch
exists (``Contact.raw_media_text_fallback``) -- someone metering a shared band
may prefer the plain failure.

## Direct messages only, and never stored

The raw transport is contact-directed even for a picture announced on a channel:
both fetch handlers resolve a *contact* and answer it. The tunnel matches that,
which also keeps a 40-message transfer off a shared channel.

These messages are machine noise, not conversation. They are sent below the
message layer (no row, no bubble, no ACK tracking) and dropped on the way in
before storage, in :func:`app.services.dm_ingest._store_direct_message`. A
recipient without this feature sees the raw ``rmt1:`` lines as chat, which is
ugly but harmless -- and is the honest signal that the peer cannot join in.

## Wire format

Fixed-width header, no delimiters after the prefix: the basE91 alphabet contains
``:``, so a delimited header would be ambiguous::

    rmt1:<sid:2><idx:1><tot:1><basE91 payload>

    field  chars  base36, lowercase
    rmt1:    5    prefix and format version
    sid      2    transfer id, 0..1295, random per payload, scoped to the sender
    idx      1    chunk index, 0..35
    tot      1    total chunk count, 1..36

Nine characters, leaving 147 of a 156-byte message for payload -- 36 chunks is
~3.8 KB, an order of magnitude more than the largest payload either format
produces (a 158-byte fragment; a 153-byte fetch request).

## Loss, and what is deliberately absent

No parity, no checksum, no retransmission inside the tunnel. A lost chunk loses
its whole payload, because basE91 is stateful across the stream and a partial
transfer cannot be decoded. That is survivable precisely because it looks like
exactly what the layer above already handles: a lost *fragment*. The requester
asks again with ``missing_indices`` and only the gaps are re-sent. A lost fetch
request is retried by the person tapping the picture again.

A transfer whose chunks never all arrive is dropped after
:data:`TRANSFER_TTL_SECONDS`, so a half-delivered payload cannot pin memory or
merge into a later one.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass

from app.compression.mcmp import decode_base91, encode_base91

PREFIX = "rmt1:"
SESSION_ID_CHARS = 2
INDEX_CHARS = 1
TOTAL_CHARS = 1

HEADER_CHARS = len(PREFIX) + SESSION_ID_CHARS + INDEX_CHARS + TOTAL_CHARS
"""Nine characters, on every chunk."""

MAX_TRANSFER_ID = 36**SESSION_ID_CHARS - 1
MAX_CHUNKS = 36**TOTAL_CHARS
"""``tot`` is one base36 character, so at most 36 chunks."""

DEFAULT_MESSAGE_BUDGET = 156
"""Bytes of text a MeshCore direct message carries. Matches
:data:`app.imaging.aeic.text_transport.DEFAULT_MESSAGE_BUDGET` and the
frontend's ``DM_HARD_LIMIT``. No ``"sender: "`` prefix to subtract -- the tunnel
never runs on a channel."""

TRANSFER_TTL_SECONDS = 180
"""How long a partially received transfer is kept. Comfortably longer than the
slowest plausible multi-chunk delivery, short enough that abandoned halves go
away on their own."""

MAX_PENDING_TRANSFERS = 64
"""Cap on half-received transfers held in memory. One peer answering a fetch
request has a single transfer in flight at a time, so this is far above normal
and exists only so a peer spraying chunk 0 of new ids cannot grow the map."""

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_CHUNK_RE = re.compile(
    rf"^{re.escape(PREFIX)}(?P<sid>[0-9a-z]{{{SESSION_ID_CHARS}}})"
    rf"(?P<idx>[0-9a-z])(?P<tot>[0-9a-z])(?P<payload>.+)$",
    re.DOTALL,
)


class RawMediaTextFormatError(ValueError):
    """A payload cannot be framed, or a chunk is not well-formed ``rmt1:``."""


def _base36(value: int, width: int) -> str:
    if value < 0 or value >= 36**width:
        raise RawMediaTextFormatError(f"{value} does not fit in {width} base36 chars")
    out = ""
    for _ in range(width):
        value, digit = divmod(value, 36)
        out = _BASE36[digit] + out
    return out


def chunk_capacity(message_budget: int = DEFAULT_MESSAGE_BUDGET) -> int:
    """Payload characters that fit in one chunk of a ``message_budget`` message."""
    capacity = message_budget - HEADER_CHARS
    if capacity < 1:
        raise RawMediaTextFormatError(
            f"message budget {message_budget} is too small to carry a chunk header"
        )
    return capacity


def new_transfer_id() -> int:
    """A random transfer id.

    Scoped to the sender by :func:`transfer_key`, so 1296 values is ample: a
    collision needs one peer to have two transfers in flight at once inside the
    same reassembly window.
    """
    return secrets.randbelow(MAX_TRANSFER_ID + 1)


def encode_chunks(
    payload: bytes,
    *,
    transfer_id: int | None = None,
    message_budget: int = DEFAULT_MESSAGE_BUDGET,
) -> list[str]:
    """Frame one raw-media payload as a list of ready-to-send message bodies."""
    if not payload:
        raise RawMediaTextFormatError("refusing to frame an empty payload")
    tid = new_transfer_id() if transfer_id is None else transfer_id
    if not 0 <= tid <= MAX_TRANSFER_ID:
        raise RawMediaTextFormatError(f"transfer id {tid} out of range")

    text = encode_base91(payload)
    capacity = chunk_capacity(message_budget)
    slices = [text[offset : offset + capacity] for offset in range(0, len(text), capacity)]
    if len(slices) > MAX_CHUNKS:
        raise RawMediaTextFormatError(
            f"payload needs {len(slices)} chunks, more than the {MAX_CHUNKS} the format can address"
        )

    head = PREFIX + _base36(tid, SESSION_ID_CHARS)
    total = _base36(len(slices), TOTAL_CHARS)
    return [
        head + _base36(index, INDEX_CHARS) + total + payload_slice
        for index, payload_slice in enumerate(slices)
    ]


@dataclass(frozen=True)
class RawMediaTextChunk:
    """One parsed ``rmt1:`` message."""

    transfer_id: int
    index: int
    total: int
    payload: str
    """This chunk's slice of the basE91 stream. NOT independently decodable."""


def is_tunnel_chunk(text: str) -> bool:
    """Cheap prefix test, for the message ingest hot path."""
    return text.startswith(PREFIX)


def parse_chunk(text: str) -> RawMediaTextChunk | None:
    """Parse one ``rmt1:`` message, or None if it is not one.

    None rather than an exception: this runs on every inbound direct message, and
    text that merely happens to start with ``rmt1:`` is not an error.
    """
    match = _CHUNK_RE.match(text)
    if match is None:
        return None
    total = int(match["tot"], 36)
    index = int(match["idx"], 36)
    if total < 1 or index >= total:
        return None
    return RawMediaTextChunk(
        transfer_id=int(match["sid"], 36),
        index=index,
        total=total,
        payload=match["payload"],
    )


def reassemble(chunks: dict[int, str], total: int) -> bytes | None:
    """Concatenate chunk payloads in index order and basE91-decode once.

    Returns None while any chunk is still missing, or when the assembled text is
    not valid basE91. The decode must happen after the concatenation: basE91 is
    stateful across the stream, so decoding chunk by chunk corrupts every
    boundary.
    """
    if len(chunks) != total or any(index not in chunks for index in range(total)):
        return None
    try:
        payload = decode_base91("".join(chunks[index] for index in range(total)))
    except Exception:
        return None
    return payload or None


def transfer_key(sender_key: str, transfer_id: int) -> str:
    """Reassembly key: ``"<sender prefix>:<transfer id>"``.

    The transfer id is only two base36 characters on the wire, so it is unique
    per *sender*, not globally. Keying by sender is what stops two peers
    answering fetch requests at the same moment from merging their chunks into
    one corrupt payload.
    """
    return f"{sender_key[:12].lower()}:{transfer_id}"


@dataclass
class _PendingTransfer:
    total: int
    chunks: dict[int, str]
    expires_at: float


_pending: dict[str, _PendingTransfer] = {}


def note_chunk(
    chunk: RawMediaTextChunk,
    *,
    sender_key: str,
    now: float,
) -> bytes | None:
    """Record one inbound chunk; return the payload once the transfer completes.

    Returns None while chunks are still missing, and None again for any later
    duplicate of a completed transfer -- the entry is dropped on completion, so a
    retransmitted chunk starts a new incomplete transfer rather than replaying a
    payload that has already been dispatched.
    """
    _prune(now)
    key = transfer_key(sender_key, chunk.transfer_id)
    pending = _pending.get(key)
    if pending is None or pending.total != chunk.total:
        # A disagreeing total means this is a different transfer that reused the
        # id, not a continuation. Start over rather than mixing the two.
        pending = _PendingTransfer(total=chunk.total, chunks={}, expires_at=0.0)
        _pending[key] = pending
    pending.chunks[chunk.index] = chunk.payload
    pending.expires_at = now + TRANSFER_TTL_SECONDS

    payload = reassemble(pending.chunks, pending.total)
    if payload is None:
        _enforce_capacity()
        return None
    del _pending[key]
    return payload


def _prune(now: float) -> None:
    for key in [key for key, pending in _pending.items() if pending.expires_at <= now]:
        del _pending[key]


def _enforce_capacity() -> None:
    """Drop the transfers furthest from completing when the map is over its cap.

    Furthest-from-completing rather than oldest: a nearly complete transfer is
    the one worth keeping, and the flood this guards against is a peer opening
    many transfers it never finishes.
    """
    while len(_pending) > MAX_PENDING_TRANSFERS:
        victim = min(_pending, key=lambda key: len(_pending[key].chunks) / _pending[key].total)
        del _pending[victim]


def reset_pending_transfers() -> None:
    """Forget every half-received transfer. For tests and radio teardown."""
    _pending.clear()
