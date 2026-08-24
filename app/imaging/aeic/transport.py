"""The AEIC transport seam -- the one file to change when command 62 lands.

An AEIC image is a 117-209 byte bitstream. Getting it on air is a *separate*
concern from producing it, and there are two ways to do it:

* :class:`TextChunkTransport` -- basE91-encode the bitstream and send it as
  ordinary ``aei1:`` text messages. Implemented, and what ships today.
* :class:`ChannelDataTransport` -- MCO Advanced's binary GRP_DATA (0x06) chunk
  stream under data type ``0xAE1C``. **Not implemented**: it needs
  ``CMD_SEND_CHANNEL_DATA`` (62), which the Python ``meshcore`` library does not
  expose. This is where it goes when it does.

Everything that sends an AEIC image -- the ``/aeic/send`` route and the bot
``send_image`` family -- goes through :func:`select_transport`, so neither has to
know which one it got. That is the point of this module: the migration is
implementing one method here and flipping one capability probe, not editing every
call site.

## What a caller provides

An :class:`AeicTarget`. It deliberately carries the union of what BOTH transports
need, and each uses only its own fields:

* ``emit_text`` and ``message_budget`` -- the text transport
* ``radio_manager`` -- the binary transport

A caller that fills in all of them works with either transport unchanged, which
is what makes the switch invisible to it. ``/aeic/send`` and the bot API both do.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from app.imaging.aeic.text_transport import (
    DEFAULT_MESSAGE_BUDGET,
    AeicStreamMetadata,
    encode_chunks,
    new_session_id,
)

logger = logging.getLogger(__name__)

TEXT_TRANSPORT = "text/aei1"
CHANNEL_DATA_TRANSPORT = "binary/0xAE1C"

CMD_SEND_CHANNEL_DATA = 62
"""The companion command that emits a GRP_DATA packet. Named here so the
capability probe and the eventual implementation share one constant."""

DATA_TYPE_AEIC_IMAGE = 0xAE1C
"""MCO Advanced's application data type for an AEIC image chunk stream."""


class AeicTransportUnavailable(RuntimeError):
    """The requested transport cannot run on this install."""


@dataclass
class AeicTarget:
    """Where an image is going, and the means to get it there.

    ``emit_text`` is what makes the text transport work without knowing whether
    it is talking to the HTTP route (which wants stored ``Message`` rows) or a
    bot (which dispatches through its own send path). It returns whatever the
    caller's send produced -- a ``Message``, or ``None`` -- and the transport
    just collects them.
    """

    conversation_type: Literal["PRIV", "CHAN"]
    conversation_key: str

    emit_text: Callable[[str], Awaitable[Any]] | None = None
    """Send one text message. Required by :class:`TextChunkTransport`."""

    message_budget: int = DEFAULT_MESSAGE_BUDGET
    """Text bytes one message carries to this destination. Channels must
    subtract the ``"sender: "`` prefix the firmware adds."""

    radio_manager: Any = None
    """Required by :class:`ChannelDataTransport`; unused by the text one."""


@dataclass(frozen=True)
class AeicSendResult:
    transport: str
    session_id: int
    chunk_count: int
    payload_bytes: int
    emitted: list[Any] = field(default_factory=list)
    """Whatever ``emit_text`` returned per chunk. Empty for a binary transport,
    which creates no message rows."""

    storage_key: str | None = None
    """Row key of the local session recording this send.

    Filled in by :meth:`app.imaging.aeic.service.AeicService.send_image` after it
    records the send; transports never set it, because how a send is stored
    locally is not a transport's concern. ``None`` when the recording failed --
    the image is on air either way.
    """


class AeicTransport(ABC):
    """One way of putting an AEIC bitstream on air."""

    name: str

    @property
    @abstractmethod
    def available(self) -> bool:
        """Whether this transport can run here."""

    @abstractmethod
    async def send(
        self,
        bitstream: bytes,
        metadata: AeicStreamMetadata,
        target: AeicTarget,
        *,
        session_id: int | None = None,
    ) -> AeicSendResult: ...


class TextChunkTransport(AeicTransport):
    """basE91 the bitstream and send it as ``aei1:`` text messages.

    Works on every route this app already has: ACKed on DMs, survives the
    channel crypto path, visible to bots, stored in the message table. A
    117-byte bitstream is 144 characters, so a typical photo is one message and
    the measured worst case (209 B) is two.

    Chunks go out in index order. Chunk 0 carries the metadata byte and must
    arrive for the receiver to open a session, so it is sent first and a failure
    on it aborts the rest rather than stranding orphan continuation chunks.
    """

    name = TEXT_TRANSPORT

    @property
    def available(self) -> bool:
        return True

    async def send(
        self,
        bitstream: bytes,
        metadata: AeicStreamMetadata,
        target: AeicTarget,
        *,
        session_id: int | None = None,
    ) -> AeicSendResult:
        if target.emit_text is None:
            raise AeicTransportUnavailable("the text transport needs an emit_text on the target")
        sid = new_session_id() if session_id is None else session_id
        chunks = encode_chunks(
            bitstream, metadata, session_id=sid, message_budget=target.message_budget
        )
        emitted: list[Any] = []
        for chunk in chunks:
            emitted.append(await target.emit_text(chunk))
        logger.info(
            "Sent a %d-byte AEIC image as %d %s message(s) to %s %s",
            len(bitstream),
            len(chunks),
            self.name,
            target.conversation_type,
            target.conversation_key[:12],
        )
        return AeicSendResult(
            transport=self.name,
            session_id=sid,
            chunk_count=len(chunks),
            payload_bytes=len(bitstream),
            emitted=emitted,
        )


class ChannelDataTransport(AeicTransport):
    """MCO Advanced's binary GRP_DATA transport. NOT IMPLEMENTED YET.

    This is the intended destination, not a fallback: it is what MCO Advanced
    speaks (so images start interoperating), and it drops basE91's ~23%
    expansion, which puts most photos back into a single packet.

    ## Implementing it

    The wire format is fully specified upstream in
    ``lib/services/image_chunk_transport.dart`` on branch
    ``origin/rename-mco-advanced``. One GRP_DATA blob per chunk, at most 163
    bytes::

        off  size  field
        0    2     sender_prefix   selfPublicKey[0..1], in EVERY chunk -- the
                                   firmware supplies no sender identity on
                                   RESP_CODE_CHANNEL_DATA_RECV, and chunk 0 may
                                   be the one that is lost
        2    1     img_id          uint8, random-ish per image
        3    1     idx<<4 | total  idx 0..15, total 1..15; idx == total means
                                   this is the XOR parity chunk
        4    ..    body            chunk 0: [metadata byte] + image bytes
                                   others : image bytes
                                   parity : [len_xor] + XOR of every data body,
                                            each zero-padded to the body size

    Body capacity is ``163 - 4 - 1 = 158`` bytes: the parity chunk spends one
    body byte on the XOR of the data body lengths, which is what makes
    single-loss recovery self-describing (a lost chunk's length comes back as
    ``len_xor ^ XOR(lengths that arrived)``).

    Four things beyond this class are needed, and none of them are in this file:

    1. an outbound path for ``CMD_SEND_CHANNEL_DATA`` (62) -- either the
       ``meshcore`` library exposing it, or a raw frame built the way
       ``app/services/voice.py`` builds raw DM frames;
    2. a GROUP_DATA (0x06) decrypt/ingest route beside the GROUP_TEXT (0x05)
       one in ``app/packet_processor.py``, dispatching on data type 0xAE1C;
    3. reassembly that honours the parity chunk (``app/imaging/aeic/ingest.py``
       currently reassembles text chunks only);
    4. keeping ``aei1:`` decoding on the inbound side regardless, so peers that
       speak only the text form keep working.

    :class:`AeicStreamMetadata` needs no change: its byte already *is* upstream's
    chunk-0 metadata byte, so nothing has to be re-derived.
    """

    name = CHANNEL_DATA_TRANSPORT

    BLOB_BYTES = 163
    HEADER_BYTES = 4
    PARITY_LENGTH_BYTES = 1
    BODY_BYTES = BLOB_BYTES - HEADER_BYTES - PARITY_LENGTH_BYTES
    MAX_DATA_CHUNKS = 15

    @property
    def available(self) -> bool:
        return channel_data_transport_available()

    async def send(
        self,
        bitstream: bytes,
        metadata: AeicStreamMetadata,
        target: AeicTarget,
        *,
        session_id: int | None = None,
    ) -> AeicSendResult:
        raise AeicTransportUnavailable(
            "the binary 0xAE1C transport is not implemented yet: it needs "
            f"CMD_SEND_CHANNEL_DATA ({CMD_SEND_CHANNEL_DATA}), which this "
            "meshcore build does not expose. See ChannelDataTransport's docstring "
            "for the wire format and the four pieces the migration needs."
        )


def channel_data_transport_available() -> bool:
    """Whether this install could send a GRP_DATA chunk stream.

    Probes the radio library for ``CMD_SEND_CHANNEL_DATA`` rather than hardcoding
    False, so the day ``meshcore`` grows the command this returns True on its own
    and :func:`select_transport` starts preferring it -- at which point the only
    thing left to write is :meth:`ChannelDataTransport.send`.

    Deliberately conservative: it must never claim the transport works when the
    send would fail, because a failed image send is a wasted encode and a
    confused user.
    """
    try:
        from meshcore.commands import messaging
    except ImportError:  # pragma: no cover - the library is a hard dependency
        return False
    handler = getattr(messaging, "MessagingCommandHandler", None)
    if handler is None:
        return False
    # The library names its senders `send_*`; any of these appearing means the
    # GRP_DATA path exists. Checked by name because the command number itself is
    # an implementation detail the library does not export.
    return any(
        hasattr(handler, candidate)
        for candidate in ("send_channel_data", "send_chan_data", "send_group_data")
    )


def select_transport(*, prefer_binary: bool = True) -> AeicTransport:
    """The best transport this install can actually use.

    Prefers the binary one the moment it becomes usable -- it is smaller on air
    and interoperates with MCO Advanced -- and falls back to text otherwise,
    which is every install today.

    ``prefer_binary=False`` forces the text transport; used by tests and
    available to a caller that specifically needs a peer to see message rows.
    """
    if prefer_binary:
        binary = ChannelDataTransport()
        if binary.available:
            return binary
    return TextChunkTransport()


CHANNEL_SENDER_SEPARATOR_BYTES = 2
""" ``": "`` -- what a channel message's ``"sender: "`` prefix costs on top of
the radio name itself."""

UNKNOWN_RADIO_NAME_BYTES = 32
"""Assumed radio-name length when it cannot be read.

Deliberately pessimistic. The frontend assumes 10 for its compose counter, but a
counter that guesses low only mis-colours a number, whereas a chunk budget that
guesses low gets the message TRUNCATED by the radio -- and a truncated basE91
chunk decodes the whole image to garbage. Over-reserving costs at worst one extra
message; under-reserving costs the picture.
"""


async def resolve_message_budget(conversation_type: str, *, radio_manager: Any = None) -> int:
    """Text bytes one message can carry to this kind of destination.

    A channel message carries ``"sender: "`` inside the encrypted payload, so the
    radio's own name comes out of the chunk budget. A DM does not.
    """
    if conversation_type == "PRIV":
        return DEFAULT_MESSAGE_BUDGET
    name_bytes = UNKNOWN_RADIO_NAME_BYTES
    if radio_manager is not None:
        try:
            async with radio_manager.radio_operation("aeic_self_name") as mc:
                name = (mc.self_info.get("name", "") if mc.self_info else "") or ""
            if name:
                name_bytes = len(name.encode("utf-8"))
        except Exception:  # noqa: BLE001 - falls back to the pessimistic guess
            logger.debug("Could not read the radio name; assuming a long one")
    return DEFAULT_MESSAGE_BUDGET - name_bytes - CHANNEL_SENDER_SEPARATOR_BYTES
