"""MCO Advanced's binary GRP_DATA image transport: framing, parity, reassembly.

This is the format AEIC images actually travel in between MCO Advanced peers --
``PAYLOAD_TYPE_GRP_DATA`` (0x06) blobs carried by the companion command
``CMD_SEND_CHANNEL_DATA`` (62) and delivered back as
``RESP_CODE_CHANNEL_DATA_RECV`` (27). RemoteTerm's own ``aei1:`` text framing
(:mod:`app.imaging.aeic.text_transport`) is a RemoteTerm-only dialect; THIS is
the interoperable one.

Ported from ``lib/services/image_chunk_transport.dart`` on meshcore-open's
``rename-mco-advanced`` branch, which its own header calls "the SINGLE SOURCE OF
TRUTH for the on-air chunk framing". Every constant and guard below mirrors it
deliberately; if the two disagree, that file wins.

## Wire format

One GRP_DATA blob per chunk, at most :data:`BLOB_BYTES`::

    off  size  field
    0    2     sender_prefix  selfPublicKey[0..1], big-endian. In EVERY chunk:
                              the firmware supplies no sender identity on
                              RESP_CODE_CHANNEL_DATA_RECV, and chunk 0 may be
                              the one that is lost.
    2    1     img_id         uint8, random-ish per image
    3    1     idx<<4 | total idx 0..15 (HIGH nibble), total 1..15 (low).
                              idx == total  =>  this is the XOR parity chunk.
    4    ..    body

Data chunk body:

* chunk 0: ``[meta]`` + image bytes, where ``meta`` is the same
  ``aspect(4) | resolution(2) | rate(2)`` byte
  :class:`~app.imaging.aeic.text_transport.AeicStreamMetadata` already encodes.
* every other chunk: image bytes.

Parity chunk body: ``[len_xor]`` + XOR of every data body, each zero-padded to
:data:`BODY_BYTES`. ``len_xor`` is the XOR of the data bodies' LENGTHS, which is
what makes single-loss recovery self-describing -- a lost chunk's length comes
back as ``len_xor ^ XOR(lengths that arrived)``, so the last (short) chunk is
rebuildable without transmitting a total length.

:data:`BODY_BYTES` is ``163 - 4 - 1``: the parity chunk spends one body byte on
that length XOR, and subtracting it from EVERY chunk is what keeps the parity
chunk itself inside one blob. The cost is exactly one wasted byte per data
chunk.

## Integrity

No app-level checksum, matching upstream. The LoRa PHY CRCs every packet and
MeshCore verifies a 2-byte HMAC per packet, so a corrupted chunk never arrives.
What no lower layer can see is a cross-image MERGE -- two senders colliding on
``sender_prefix + img_id + channel`` inside one TTL, about 1/65536 per concurrent
pair. Upstream accepts that risk because the 2 bytes to detect it pushed the
measured ft32 mean past the single-chunk capacity; we accept it to stay
byte-compatible.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CMD_SEND_CHANNEL_DATA = 62
"""Companion command that emits a GRP_DATA packet."""

RESP_CODE_CHANNEL_DATA_RECV = 27
"""Inbound companion frame carrying a GRP_DATA blob."""

OUT_PATH_UNKNOWN = 0xFF
"""``path_len`` value that asks the firmware for flood routing."""

DATA_TYPE_AEIC_IMAGE = 0xAE1C
"""Application data type for an AEIC image chunk stream. ``0x0000`` is rejected
by the firmware, which is why upstream picked a nonzero constant."""

DATA_TYPE_MCO_IMAGE = 0xFFF0
"""MCOimg, a DIFFERENT image codec that also rides GRP_DATA. We recognise the
type so an MCOimg frame is reported as unsupported rather than fed to the AEIC
decoder, which would produce a confident, wrong picture."""

DATA_TYPE_MCMP = 0xFFF1
"""MCMP compressed text over GRP_DATA. Recognised for the same reason."""

DATA_TYPE_MCO_APP = 0x0120
"""MCO Advanced's *official* application data type, which supersedes the two
``0xFFF*`` developer-namespace types above for everything it carries.

Its body is an envelope rather than a payload::

    senderNameLen(varuint) senderName(utf8) subtypeVersion(u8) body
    subtypeVersion: high nibble = content subtype, low nibble = content version

Recognised, never decoded. AEIC did NOT move here -- upstream's
``channel_app_data_helper.dart`` defines subtypes for MCOimg (1) and MCMP (2)
only, and ``image_chunk_transport.dart`` still puts AEIC on the air as a bare
:data:`DATA_TYPE_AEIC_IMAGE`. The type is listed so a frame from a current MCO
Advanced build reports what it is instead of "unknown data type 0x0120", which
reads like a protocol fault rather than a codec this build has no decoder for."""

MCO_APP_SUBTYPE_MCO_IMAGE = 0x01
MCO_APP_SUBTYPE_MCMP = 0x02

BLOB_BYTES = 163
HEADER_BYTES = 4
PARITY_LENGTH_BYTES = 1
BODY_BYTES = BLOB_BYTES - HEADER_BYTES - PARITY_LENGTH_BYTES
"""158. Every chunk pays the parity chunk's length byte; see the module docstring."""

METADATA_BYTES = 1
"""Chunk 0's body starts with the stream metadata byte."""

MAX_DATA_CHUNKS = 15
"""``total`` is a nibble, and ``idx == total`` is reserved for parity."""

CHUNK_ZERO_IMAGE_BYTES = BODY_BYTES - METADATA_BYTES
"""157 image bytes in chunk 0, 158 in the rest."""

GRP_DATA_PLACEHOLDER_KEY = "remoteterm_grp_data_frame"
"""Marks the synthetic ``CHANNEL_MSG_RECV`` event dispatched for a frame 27.

Frame 27 is a response to ``CMD_SYNC_NEXT_MESSAGE`` -- MCO Advanced's own sync
tracker lists it beside the msg-recv codes -- but meshcore-py's ``get_msg`` only
resolves on CONTACT_MSG_RECV / CHANNEL_MSG_RECV / ERROR / NO_MORE_MSGS. Our
adapter consumes the frame, so without this the caller that asked the firmware
for it was left waiting for a reply that had already arrived: the auto-fetch
loop calls ``get_msg`` with **no timeout** and therefore hung forever on the
first queued image, taking all push-driven message fetching down with it, and
the drain loops burned their 2 s timeouts and stopped early. The adapter now
dispatches a CHANNEL_MSG_RECV carrying this key so the waiter resolves and the
queue keeps draining; every consumer of pulled channel messages must skip it.

Local convention between the adapter and this app's own drain code. Never on
the wire, and nothing outside this codebase sees it.
"""


def grp_data_placeholder_payload() -> dict:
    """The payload of the synthetic event standing in for one frame 27."""
    return {GRP_DATA_PLACEHOLDER_KEY: True}


def is_grp_data_placeholder(payload) -> bool:
    """Whether a pulled CHANNEL_MSG_RECV is the adapter's stand-in, not a message."""
    return isinstance(payload, dict) and bool(payload.get(GRP_DATA_PLACEHOLDER_KEY))


class ChannelDataFormatError(ValueError):
    """A GRP_DATA blob is not well-formed."""


@dataclass(frozen=True)
class ChannelDataChunk:
    """One parsed GRP_DATA blob."""

    sender_prefix: int
    img_id: int
    index: int
    total: int
    body: bytes

    @property
    def is_parity(self) -> bool:
        """``idx == total`` marks the XOR parity chunk."""
        return self.index == self.total


def sender_prefix_for(public_key: bytes) -> int:
    """``publicKey[0..1]`` as a big-endian uint16, upstream's identity field."""
    if len(public_key) < 2:
        raise ChannelDataFormatError("public key too short for a sender prefix")
    return ((public_key[0] & 0xFF) << 8) | (public_key[1] & 0xFF)


LOCAL_ALIAS_PREFIX_XOR = 0xFFFF
"""Complement applied to our own sender prefix in the copy handed to apps.

An app on the virtual companion node has no identity of its own: it reads
SELF_INFO from this server and so believes its public key *is* the radio's. MCO
Advanced drops any image chunk whose ``senderPrefix`` equals its own -- upstream
``image_chunk_transport.dart`` returns ``ImageChunkStatus.fromSelf`` before
reassembly -- which is correct on a real radio, where the only way to hear your
own prefix is a repeater re-flooding your own packet, and fatal here, where every
picture RemoteTerm sends carries exactly that prefix.

So the copy relayed to apps is relabelled. The bytes on air keep the true prefix,
because that is what the mesh must see; only the local copy is aliased, so the
app treats it as another node's picture and renders it instead of discarding it.
A complement is used rather than a random value so all chunks of one image agree
without carrying any state, and so the alias is reversible by eye in a log.
"""


def aliased_sender_prefix(sender_prefix: int) -> int:
    """The prefix to show an app for a chunk this node sent. See the constant."""
    return (sender_prefix ^ LOCAL_ALIAS_PREFIX_XOR) & 0xFFFF


def aliased_public_key(public_key: str) -> str | None:
    """The whole key an app should resolve an aliased chunk back to.

    Two bytes of identity are all a chunk header carries, so an app that cannot
    match them against a contact has nothing to print but the bytes themselves --
    "Node 68b8" where the sender's name belongs. This is that alias widened to a
    full key: the same 32 bytes as ours with only the first two complemented, so
    it can be served as a contact named after this radio and the picture arrives
    with a name on it.

    None when the key is not a full 32-byte key -- there is no identity to alias
    while the radio has not reported itself.
    """
    try:
        raw = bytes.fromhex(public_key)
    except ValueError:
        return None
    if len(raw) != 32:
        return None
    prefix = aliased_sender_prefix(sender_prefix_for(raw))
    return bytes(((prefix >> 8) & 0xFF, prefix & 0xFF)).hex() + raw[2:].hex()


def relabel_chunk_sender(blob: bytes, sender_prefix: int) -> bytes:
    """Rewrite a chunk's 2-byte sender prefix, leaving everything else alone.

    Safe for every chunk of an image, parity included: the XOR parity covers the
    bodies only, and each chunk carries its own header, so relabelling them all
    keeps recovery working.
    """
    if len(blob) < HEADER_BYTES:
        return blob
    return bytes(((sender_prefix >> 8) & 0xFF, sender_prefix & 0xFF)) + bytes(blob[2:])


def new_image_id() -> int:
    """A per-image uint8. Random rather than sequential so two senders that both
    restart do not immediately collide on 0."""
    return secrets.randbelow(256)


def build_chunk_blob(
    *, sender_prefix: int, img_id: int, index: int, total: int, body: bytes
) -> bytes:
    """Assemble one blob. ``index == total`` builds the parity chunk."""
    if not 0 <= sender_prefix <= 0xFFFF:
        raise ChannelDataFormatError(f"sender prefix {sender_prefix} out of range")
    if not 0 <= img_id <= 0xFF:
        raise ChannelDataFormatError(f"image id {img_id} out of range")
    if not 1 <= total <= MAX_DATA_CHUNKS:
        raise ChannelDataFormatError(f"total {total} must be 1..{MAX_DATA_CHUNKS}")
    if not 0 <= index <= total:
        raise ChannelDataFormatError(f"index {index} must be 0..{total}")
    if len(body) > BODY_BYTES + PARITY_LENGTH_BYTES:
        raise ChannelDataFormatError(f"body of {len(body)} exceeds the blob")
    return (
        bytes(
            (
                (sender_prefix >> 8) & 0xFF,
                sender_prefix & 0xFF,
                img_id & 0xFF,
                ((index & 0x0F) << 4) | (total & 0x0F),
            )
        )
        + body
    )


def parse_chunk_blob(blob: bytes) -> ChannelDataChunk | None:
    """Parse one blob, or None when it is not a well-formed chunk.

    None rather than raising: this runs on every inbound GRP_DATA frame, and a
    frame from some other application is not an error.
    """
    if len(blob) <= HEADER_BYTES:
        return None
    total = blob[3] & 0x0F
    index = (blob[3] >> 4) & 0x0F
    if total < 1 or index > total:
        return None
    return ChannelDataChunk(
        sender_prefix=((blob[0] & 0xFF) << 8) | (blob[1] & 0xFF),
        img_id=blob[2] & 0xFF,
        index=index,
        total=total,
        body=bytes(blob[HEADER_BYTES:]),
    )


def split_image_bodies(bitstream: bytes, metadata_byte: int) -> list[bytes]:
    """Cut a bitstream into chunk bodies. Chunk 0 carries the metadata byte."""
    if not bitstream:
        raise ChannelDataFormatError("refusing to frame an empty bitstream")
    if not 0 <= metadata_byte <= 0xFF:
        raise ChannelDataFormatError(f"metadata byte {metadata_byte} out of range")

    bodies = [bytes((metadata_byte,)) + bitstream[:CHUNK_ZERO_IMAGE_BYTES]]
    offset = CHUNK_ZERO_IMAGE_BYTES
    while offset < len(bitstream):
        bodies.append(bitstream[offset : offset + BODY_BYTES])
        offset += BODY_BYTES
    if len(bodies) > MAX_DATA_CHUNKS:
        raise ChannelDataFormatError(
            f"bitstream needs {len(bodies)} chunks, more than the {MAX_DATA_CHUNKS} "
            "this framing can address"
        )
    return bodies


def build_parity_body(bodies: list[bytes]) -> bytes:
    """``[len_xor] + XOR(bodies zero-padded to BODY_BYTES)``."""
    xor = bytearray(BODY_BYTES)
    length_xor = 0
    for body in bodies:
        length_xor ^= len(body)
        for i, value in enumerate(body):
            xor[i] ^= value
    return bytes((length_xor & 0xFF,)) + bytes(xor)


def build_image_chunks(
    bitstream: bytes,
    metadata_byte: int,
    *,
    sender_prefix: int,
    img_id: int,
    with_parity: bool = True,
) -> list[bytes]:
    """Every blob for one image, data chunks in order then the parity chunk.

    Parity is upstream's default and costs one extra packet, which buys recovery
    from any single lost chunk. It is worth it here in a way it was not for the
    text transport: there each chunk is an ACKed/retried text message, whereas a
    GRP_DATA blob is fire-and-forget.
    """
    bodies = split_image_bodies(bitstream, metadata_byte)
    total = len(bodies)
    blobs = [
        build_chunk_blob(
            sender_prefix=sender_prefix, img_id=img_id, index=index, total=total, body=body
        )
        for index, body in enumerate(bodies)
    ]
    if with_parity:
        blobs.append(
            build_chunk_blob(
                sender_prefix=sender_prefix,
                img_id=img_id,
                index=total,
                total=total,
                body=build_parity_body(bodies),
            )
        )
    return blobs


@dataclass
class PendingImage:
    """Chunks collected for one ``(sender_prefix, img_id)`` on one channel."""

    total: int
    bodies: dict[int, bytes] = field(default_factory=dict)
    parity_body: bytes | None = None
    first_seen: float = 0.0
    last_seen: float = 0.0

    @property
    def is_complete(self) -> bool:
        return len(self.bodies) == self.total and all(
            index in self.bodies for index in range(self.total)
        )

    @property
    def is_recoverable(self) -> bool:
        """Exactly one data chunk missing and parity in hand."""
        return self.parity_body is not None and len(self.bodies) == self.total - 1


def recover_missing_body(entry: PendingImage) -> bytes | None:
    """Rebuild the one missing data body from parity, or None if it cannot be.

    The guards mirror upstream exactly, and they matter: without them a single
    flipped bit in the parity length byte silently yields a TRUNCATED image
    reported as complete, which is worse than failing because the caller has no
    way to know the bytes are wrong.
    """
    if not entry.is_recoverable or entry.parity_body is None:
        return None
    missing = next((i for i in range(entry.total) if i not in entry.bodies), None)
    if missing is None:
        return None

    parity = entry.parity_body
    length_xor = parity[0] & 0xFF
    xor = bytearray(BODY_BYTES)
    for i, value in enumerate(parity[PARITY_LENGTH_BYTES:]):
        if i >= BODY_BYTES:
            break
        xor[i] = value
    for body in entry.bodies.values():
        length_xor ^= len(body)
        for i, value in enumerate(body):
            xor[i] ^= value

    if length_xor > BODY_BYTES:
        return None  # corrupt parity
    if missing == 0 and length_xor < METADATA_BYTES:
        return None  # chunk 0 must at least hold the metadata byte
    # Only the LAST data chunk may be short; every earlier one is full by
    # construction.
    if missing < entry.total - 1 and length_xor != BODY_BYTES:
        return None
    return bytes(xor[:length_xor])


def assemble(entry: PendingImage) -> tuple[bytes, int] | None:
    """``(bitstream, metadata_byte)`` once every data chunk is in hand."""
    first = entry.bodies.get(0)
    if first is None or len(first) < METADATA_BYTES:
        # Chunk 0 carries the metadata byte; anything shorter cannot be a chunk 0
        # from this framing, so keep waiting rather than guessing.
        return None
    out = bytearray(first[METADATA_BYTES:])
    for index in range(1, entry.total):
        body = entry.bodies.get(index)
        if body is None:
            return None
        out += body
    return bytes(out), first[0]


@dataclass(frozen=True)
class ParsedChannelData:
    """A parsed ``RESP_CODE_CHANNEL_DATA_RECV`` (27) frame.

    Fixed 9-byte header, no path bytes and no sender identity -- which is why
    the sender prefix lives inside the chunk itself.
    """

    snr_raw: int
    channel_index: int
    path_len_byte: int
    data_type: int
    payload: bytes

    @property
    def snr_db(self) -> float:
        return self.snr_raw / 4.0

    @property
    def arrived_by_flood(self) -> bool:
        return self.path_len_byte != OUT_PATH_UNKNOWN

    @property
    def hop_count(self) -> int | None:
        return (self.path_len_byte & 0x3F) if self.arrived_by_flood else None


MAX_ENVELOPE_NAME_BYTES = 127
"""Longest sender name read out of an envelope.

The length is a varuint and only the one-byte form is accepted: the field holds
a radio name, the continuation form starts at 128 bytes, and a two-byte length
is far likelier to mean this is not the envelope we think than to mean somebody
has a 200-character name.
"""


@dataclass(frozen=True)
class ChannelDataEnvelope:
    """The sender-named envelope both GRP_DATA payload families start with.

    ``senderNameLen(varuint) senderName(utf8) [subtypeVersion(u8)] body`` --
    MCO Advanced's ``channel_binary_data_helper.dart`` (legacy ``0xFFF*``
    types, no subtype byte) and ``channel_app_data_helper.dart``
    (:data:`DATA_TYPE_MCO_APP`, which adds it). ``subtype`` and ``version`` are
    None for the legacy shape.
    """

    sender_name: str
    body: bytes
    subtype: int | None = None
    version: int | None = None


def parse_envelope(payload: bytes, *, with_subtype: bool) -> ChannelDataEnvelope | None:
    """Split a GRP_DATA payload into its sender name and body.

    Returns None when the payload cannot be that envelope -- too short, an
    implausible name length, or a name that is not UTF-8 -- so a frame from
    another application is reported as unreadable rather than described (or
    decoded) confidently.
    """
    if not payload:
        return None
    name_len = payload[0]
    if name_len & 0x80 or name_len > MAX_ENVELOPE_NAME_BYTES:
        return None
    body_at = 1 + name_len
    if body_at > len(payload):
        return None
    try:
        sender_name = payload[1:body_at].decode("utf-8")
    except UnicodeDecodeError:
        return None
    if not with_subtype:
        return ChannelDataEnvelope(sender_name=sender_name, body=bytes(payload[body_at:]))
    if body_at >= len(payload):
        return None
    packed = payload[body_at]
    return ChannelDataEnvelope(
        sender_name=sender_name,
        body=bytes(payload[body_at + 1 :]),
        subtype=packed >> 4,
        version=packed & 0x0F,
    )


def mco_app_subtype(payload: bytes) -> tuple[int, int] | None:
    """Read ``(subtype, version)`` out of a :data:`DATA_TYPE_MCO_APP` body.

    Enough of the envelope to *name* the content and no more; the decoders live
    elsewhere (:mod:`app.imaging.aeic.channel_data_text` for MCMP). Returns
    None when the payload is not that envelope, which keeps a malformed frame
    from being described confidently.
    """
    envelope = parse_envelope(payload, with_subtype=True)
    if envelope is None or envelope.subtype is None or envelope.version is None:
        return None
    return envelope.subtype, envelope.version


def parse_channel_data_frame(frame: bytes) -> ParsedChannelData | None:
    """Parse a companion frame 27, or None if it is not one."""
    if len(frame) < 9 or frame[0] != RESP_CODE_CHANNEL_DATA_RECV:
        return None
    data_len = frame[8]
    if len(frame) < 9 + data_len:
        return None
    snr = frame[1] - 256 if frame[1] >= 128 else frame[1]
    return ParsedChannelData(
        snr_raw=snr,
        channel_index=frame[4],
        path_len_byte=frame[5],
        data_type=frame[6] | (frame[7] << 8),
        payload=bytes(frame[9 : 9 + data_len]),
    )


def build_send_command(channel_index: int, data_type: int, blob: bytes) -> bytes:
    """The ``CMD_SEND_CHANNEL_DATA`` frame for one blob.

    ``[0x3E][channel_idx][path_len][type_lo][type_hi][...blob...]``. The firmware
    replies with ``RESP_CODE_OK`` (0x00) -- NOT ``RESP_CODE_SENT`` -- or an error.
    """
    if not 0 <= channel_index <= 0xFF:
        raise ChannelDataFormatError(f"channel index {channel_index} out of range")
    if not 0 <= data_type <= 0xFFFF:
        raise ChannelDataFormatError(f"data type {data_type} out of range")
    if len(blob) > BLOB_BYTES:
        raise ChannelDataFormatError(f"blob of {len(blob)} exceeds {BLOB_BYTES} bytes")
    return (
        bytes(
            (
                CMD_SEND_CHANNEL_DATA,
                channel_index & 0xFF,
                OUT_PATH_UNKNOWN,
                data_type & 0xFF,
                (data_type >> 8) & 0xFF,
            )
        )
        + blob
    )
