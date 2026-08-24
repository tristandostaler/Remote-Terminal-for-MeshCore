"""The ``aei1:`` text transport -- AEIC bitstreams as ordinary MeshCore messages.

MCO Advanced carries AEIC over a binary GRP_DATA (0x06) chunk stream with data
type ``0xAE1C``. RemoteTerm carries it as **text**, basE91-encoded, exactly the
way :mod:`app.compression.mcmp` carries compressed prose. The reason is that a
text message is the one transport every route in this app already handles: it is
ACKed on DMs, it survives the channel encrypt/decrypt path, bots see it, the
message table stores it, and no radio-layer or firmware-command work is needed.

.. note:: **This transport is the interim choice, not the destination.**

   The blocker is capability, not preference: the Python ``meshcore`` library
   exposes no ``CMD_SEND_CHANNEL_DATA`` (62), and RemoteTerm's channel ingest
   only decrypts GROUP_TEXT (0x05), never GROUP_DATA (0x06). **Once command 62
   is available, migrate to the binary 0xAE1C transport** -- it is what MCO
   Advanced speaks, so it buys real interoperability, and it drops the ~23%
   basE91 expansion, which turns most two-message photos back into one packet.

   What that migration needs, and what this module already does to keep it
   cheap:

   * an outbound path for command 62 (or a raw GRP_DATA frame builder), plus a
     GROUP_DATA decrypt/ingest route alongside the GROUP_TEXT one;
   * :class:`AeicStreamMetadata` reused as-is -- its byte is deliberately
     bit-for-bit MCO Advanced's chunk-0 metadata byte
     (``aspect(4) | resolution(2) | rate(2)``), so nothing here has to be
     re-derived or re-specified;
   * upstream's XOR parity chunk, which the binary framing carries and this one
     deliberately omits (see below);
   * keeping ``aei1:`` decoding on the inbound side regardless, so images from
     peers that only speak the text form still render.

## Why this fits at all

AEIC-SE at ft32 produces a *tiny* payload -- measured mean 156 B, max 209 B over
upstream's 26-image corpus -- and basE91 expands by only ~23%::

    117 B bitstream -> 144 chars -> ONE message
    166 B bitstream -> 205 chars -> two messages
    209 B bitstream -> 257 chars -> two messages

against a 156-byte message budget. So a 512x512 colour photo is one or two text
messages. The existing IE4 path sends a 256x256 greyscale AVIF as an envelope
plus 15-40 raw fragments, so this is an order of magnitude less airtime.

## Wire format

Fixed-width header, no delimiters -- the basE91 alphabet contains ``:``, so a
delimited header would be ambiguous, and at 156 bytes every character counts::

    aei1<sid:2><idx:1><tot:1>[<meta:2> only when idx == 0]<basE91 payload>

    field  chars  base36, lowercase
    aei1     4    prefix and format version
    sid      2    session id, 0..1295, random per image, scoped to the sender
    idx      1    chunk index, 0..35
    tot      1    total chunk count, 1..36
    meta     2    chunk 0 ONLY: the stream metadata byte, see below

Header cost is 10 chars on chunk 0 and 8 on the rest.

The metadata byte is bit-for-bit the same byte MCO Advanced puts in its chunk 0,
``aspect(4) | resolution(2) | rate(2)``, so a future binary transport -- or a
gateway bridging the two -- needs no second definition. The aspect nibble names
the source photo's shape *before* it was stretched into the square, which the
codec cannot recover from the pixels; the receiver letterboxes back to it.

## Reassembly and the parts that are deliberately absent

The chunks are text slices of ONE basE91 stream, so reassembly is string
concatenation in index order followed by a single decode -- basE91 is stateful
across the stream and must not be decoded per chunk.

There is **no parity chunk and no app-level checksum**, both on purpose:

* Parity. MCO Advanced spends a third packet on an XOR chunk to survive one
  loss. Here an image is usually 1-2 messages, so parity would cost +50-100%
  airtime; DMs are ACKed by MeshCore and retried by the normal send path, and an
  incomplete channel image is surfaced in the UI rather than silently rendered.
  Adding parity later is a prefix bump to ``aei2:``, not a change here.
* Checksum. The LoRa PHY CRCs every packet and MeshCore verifies a 2-byte HMAC,
  so a corrupted chunk never reaches this layer. What no lower layer can see is
  two senders colliding on the same session id inside one TTL -- which is why a
  session is keyed by *sender* as well as by id.
"""

from __future__ import annotations

import math
import re
import secrets
from dataclasses import dataclass

from app.compression.mcmp import decode_base91, encode_base91

PREFIX = "aei1"
SESSION_ID_CHARS = 2
INDEX_CHARS = 1
TOTAL_CHARS = 1
META_CHARS = 2

HEADER_CHARS = len(PREFIX) + SESSION_ID_CHARS + INDEX_CHARS + TOTAL_CHARS
"""Header on a chunk after the first: 8 characters."""

FIRST_HEADER_CHARS = HEADER_CHARS + META_CHARS
"""Header on chunk 0: 10 characters."""

MAX_SESSION_ID = 36**SESSION_ID_CHARS - 1
MAX_CHUNKS = 36**TOTAL_CHARS
"""``tot`` is one base36 character, so at most 36 chunks."""

DEFAULT_MESSAGE_BUDGET = 156
"""Bytes of text a MeshCore message carries. Matches the frontend's
``DM_HARD_LIMIT`` / ``CHANNEL_HARD_LIMIT``; channels must subtract the
``"sender: "`` prefix, which :func:`chunk_capacities` takes as an argument."""

RESOLUTION_CODES = (512, 256, 768, 1024)
"""Square sizes addressable by the 2-bit resolution code. 512 is code 0 because
it is the only size the current decoder supports; the rest exist so a future
model can be signalled without a format change."""

RATE_WIRE_STANDARD = 0
"""Wire code for ft32, the one shipping rate point."""

RATE_WIRE_HIGH = 1
"""Reserved for ft16. Not shipping, but the code stays allocated so an
ft32-only build and a future ft16-capable build agree on the nibble."""

ASPECT_CODES: tuple[tuple[int, int], ...] = (
    (1, 1),  # 0  square
    (5, 4),  # 1  landscape
    (4, 3),  # 2
    (3, 2),  # 3
    (16, 10),  # 4
    (16, 9),  # 5
    (2, 1),  # 6
    (21, 9),  # 7
    (4, 5),  # 8  portrait
    (3, 4),  # 9
    (2, 3),  # 10
    (10, 16),  # 11
    (9, 16),  # 12
    (1, 2),  # 13
    (9, 21),  # 14
    (1, 1),  # 15 unknown -> render square
)

ASPECT_UNKNOWN = 15
"""Wire code for "shape unknown"; the receiver renders it square, unstretched."""

_ASPECT_TOLERANCE = 0.18
"""Half a step between 21:9 and the next ratio out. Beyond that we would be
asserting a shape the sender never had, so the code becomes "unknown"."""

_BASE36 = "0123456789abcdefghijklmnopqrstuvwxyz"
_CHUNK_RE = re.compile(
    rf"^{PREFIX}(?P<sid>[0-9a-z]{{{SESSION_ID_CHARS}}})"
    rf"(?P<idx>[0-9a-z])(?P<tot>[0-9a-z])(?P<rest>.*)$",
    re.DOTALL,
)


class AeicTextFormatError(ValueError):
    """A chunk is not well-formed ``aei1:`` text."""


def _base36(value: int, width: int) -> str:
    if value < 0 or value >= 36**width:
        raise AeicTextFormatError(f"{value} does not fit in {width} base36 chars")
    out = ""
    for _ in range(width):
        value, digit = divmod(value, 36)
        out = _BASE36[digit] + out
    return out


def aspect_code_for(width: int, height: int) -> int:
    """The :data:`ASPECT_CODES` entry closest to ``width / height``.

    Compared in log space so 4:3 and 3:4 are equally far from square. Returns
    :data:`ASPECT_UNKNOWN` for a ratio outside roughly 21:9..9:21 rather than
    snapping a panorama onto 21:9 and letterboxing it wrongly.
    """
    if width <= 0 or height <= 0:
        return ASPECT_UNKNOWN
    target = math.log(width / height)
    best, best_error = ASPECT_UNKNOWN, math.inf
    for code, (w, h) in enumerate(ASPECT_CODES):
        if code == ASPECT_UNKNOWN:
            continue  # duplicate of 1:1
        error = abs(math.log(w / h) - target)
        if error < best_error:
            best, best_error = code, error
    return best if best_error <= _ASPECT_TOLERANCE else ASPECT_UNKNOWN


@dataclass(frozen=True)
class AeicStreamMetadata:
    """Contents of the single metadata byte carried by chunk 0."""

    square_size: int = 512
    rate_code: int = RATE_WIRE_STANDARD
    aspect_code: int = 0

    @property
    def aspect_ratio(self) -> float:
        w, h = ASPECT_CODES[self.aspect_code & 0x0F]
        return w / h

    @property
    def is_square(self) -> bool:
        """True when the image should be rendered square, unstretched."""
        return self.aspect_code in (0, ASPECT_UNKNOWN)

    def encode(self) -> int:
        try:
            code = RESOLUTION_CODES.index(self.square_size)
        except ValueError:
            raise AeicTextFormatError(
                f"square size {self.square_size} is not representable; must be "
                f"one of {RESOLUTION_CODES}"
            ) from None
        return ((self.aspect_code & 0x0F) << 4) | ((code & 0x03) << 2) | (self.rate_code & 0x03)

    @classmethod
    def decode(cls, byte: int) -> AeicStreamMetadata | None:
        """Decode the wire byte, or None for a rate this build cannot handle.

        Deliberately never falls back to a default rate: an unknown code means
        the sender is running a format we cannot decode, and guessing would hand
        the wrong model a bitstream it will happily turn into garbage.
        """
        rate = byte & 0x03
        code = (byte >> 2) & 0x03
        if rate not in (RATE_WIRE_STANDARD, RATE_WIRE_HIGH):
            return None
        if code >= len(RESOLUTION_CODES):
            return None
        return cls(
            square_size=RESOLUTION_CODES[code],
            rate_code=rate,
            aspect_code=(byte >> 4) & 0x0F,
        )


@dataclass(frozen=True)
class AeicChunk:
    """One parsed ``aei1:`` message."""

    session_id: int
    index: int
    total: int
    payload: str
    """The chunk's slice of the basE91 stream. NOT independently decodable."""

    metadata: AeicStreamMetadata | None
    """Present on chunk 0 only; None on every later chunk."""


def chunk_capacities(message_budget: int = DEFAULT_MESSAGE_BUDGET) -> tuple[int, int]:
    """``(chunk 0 payload chars, later chunk payload chars)`` for a budget.

    Callers on a channel must pass ``156 - len(sender_name) - 2`` so the
    ``"sender: "`` prefix the firmware adds inside the encrypted payload is
    accounted for.
    """
    first = message_budget - FIRST_HEADER_CHARS
    rest = message_budget - HEADER_CHARS
    if first < 1 or rest < 1:
        raise AeicTextFormatError(
            f"message budget {message_budget} is too small to carry a chunk header"
        )
    return first, rest


def new_session_id() -> int:
    """A random per-image session id.

    Scoped to the sender by the caller, so 1296 values is ample: a collision
    needs the same sender to have two images in flight inside one reassembly
    window.
    """
    return secrets.randbelow(MAX_SESSION_ID + 1)


def encode_chunks(
    bitstream: bytes,
    metadata: AeicStreamMetadata,
    *,
    session_id: int | None = None,
    message_budget: int = DEFAULT_MESSAGE_BUDGET,
) -> list[str]:
    """Frame an AEIC bitstream as a list of ready-to-send text messages."""
    if not bitstream:
        raise AeicTextFormatError("refusing to frame an empty bitstream")
    sid = new_session_id() if session_id is None else session_id
    if not 0 <= sid <= MAX_SESSION_ID:
        raise AeicTextFormatError(f"session id {sid} out of range")

    text = encode_base91(bitstream)
    first_capacity, rest_capacity = chunk_capacities(message_budget)

    slices = [text[:first_capacity]]
    offset = first_capacity
    while offset < len(text):
        slices.append(text[offset : offset + rest_capacity])
        offset += rest_capacity
    if len(slices) > MAX_CHUNKS:
        raise AeicTextFormatError(
            f"bitstream needs {len(slices)} chunks, more than the {MAX_CHUNKS} the "
            "format can address"
        )

    total = len(slices)
    head = PREFIX + _base36(sid, SESSION_ID_CHARS)
    out: list[str] = []
    for index, payload in enumerate(slices):
        chunk = head + _base36(index, INDEX_CHARS) + _base36(total, TOTAL_CHARS)
        if index == 0:
            chunk += _base36(metadata.encode(), META_CHARS)
        out.append(chunk + payload)
    return out


def is_aeic_chunk(text: str) -> bool:
    """Cheap prefix test, for the ingest hot path."""
    return text.startswith(PREFIX)


def parse_chunk(text: str) -> AeicChunk | None:
    """Parse one ``aei1:`` message, or None if it is not one.

    None rather than an exception: this runs on every inbound message, and a
    message that merely happens to start with ``aei1`` is not an error.
    """
    match = _CHUNK_RE.match(text)
    if match is None:
        return None
    session_id = int(match["sid"], 36)
    index = int(match["idx"], 36)
    total = int(match["tot"], 36)
    rest = match["rest"]
    if total < 1 or index >= total:
        return None

    metadata = None
    if index == 0:
        if len(rest) < META_CHARS:
            return None
        try:
            meta_byte = int(rest[:META_CHARS], 36)
        except ValueError:
            return None
        metadata = AeicStreamMetadata.decode(meta_byte)
        if metadata is None:
            return None
        rest = rest[META_CHARS:]
    if not rest:
        return None
    return AeicChunk(
        session_id=session_id,
        index=index,
        total=total,
        payload=rest,
        metadata=metadata,
    )


def reassemble(chunks: dict[int, str], total: int) -> bytes | None:
    """Concatenate chunk payloads in index order and basE91-decode once.

    Returns None while any chunk is still missing. basE91 is stateful across the
    stream, so the concatenation must happen before the decode -- decoding each
    chunk separately would corrupt every boundary.
    """
    if len(chunks) != total or any(index not in chunks for index in range(total)):
        return None
    return decode_base91("".join(chunks[index] for index in range(total)))
