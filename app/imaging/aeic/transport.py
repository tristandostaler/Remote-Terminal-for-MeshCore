"""The AEIC transport seam -- the one file to change when command 62 lands.

An AEIC image is a 117-209 byte bitstream. Getting it on air is a *separate*
concern from producing it, and there are two ways to do it:

* :class:`TextChunkTransport` -- basE91-encode the bitstream and send it as
  ordinary ``aei1:`` text messages. Implemented, and what ships today.
* :class:`ChannelDataTransport` -- MCO Advanced's binary GRP_DATA (0x06) chunk
  stream under data type ``0xAE1C``, via ``CMD_SEND_CHANNEL_DATA`` (62). This is
  the interoperable one, and the default for channels.

Everything that sends an AEIC image -- the ``/aeic/send`` route and the bot
``send_image`` family -- goes through :func:`select_transport`, so neither has to
know which one it got.

The Python ``meshcore`` library exposes no ``send_channel_data`` helper, but it
does not need to: ``commands.send`` is a generic dispatcher, so the frame is
built in :mod:`app.imaging.aeic.channel_data` and handed straight to it. What
cannot be probed from Python is whether the *firmware* implements command 62;
the radio answers that by rejecting the first blob, which
:class:`AeicChannelDataUnsupported` turns into a clean fallback to text.

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

from meshcore.events import EventType

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


class AeicChannelDataUnsupported(AeicTransportUnavailable):
    """This radio's firmware does not accept ``CMD_SEND_CHANNEL_DATA``.

    Raised ONLY when the very first blob was rejected, i.e. nothing reached the
    air yet, so the caller can cleanly fall back to the text transport. A
    failure on any later blob raises the plain
    :class:`AeicTransportUnavailable` instead, because part of the image is
    already out and resending it by another route would duplicate it.
    """


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
    """MCO Advanced's binary GRP_DATA transport -- the interoperable one.

    Channels only: GRP_DATA is a group payload type with no DM equivalent, so
    :func:`select_transport` hands direct messages to the text transport.

    Preferred over text where it applies, for two reasons. It is what MCO
    Advanced actually speaks, so images interoperate rather than showing up as a
    line of basE91 on the peer; and it drops that ~23% expansion, which puts most
    photos back into a single packet.

    The wire format -- blob layout, per-chunk capacities, XOR parity -- lives in
    :mod:`app.imaging.aeic.channel_data`, ported from upstream's single source of
    truth. This class is only the radio plumbing: load the channel into a slot,
    build the frames, push them.

    Unlike the text transport this creates **no message rows**, because nothing
    textual crossed the air. The local record is the AEIC session written by
    :meth:`AeicService.send_image`.
    """

    name = CHANNEL_DATA_TRANSPORT

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
        """Emit the image as GRP_DATA blobs via ``CMD_SEND_CHANNEL_DATA``.

        Channels only: GRP_DATA is a group payload type, and there is no DM
        equivalent. A DM target falls back to the text transport, which is why
        :func:`select_transport` takes the conversation type.
        """
        from app.imaging.aeic.channel_data import (
            DATA_TYPE_AEIC_IMAGE,
            build_image_chunks,
            build_send_command,
            new_image_id,
            sender_prefix_for,
        )

        if target.conversation_type != "CHAN":
            raise AeicTransportUnavailable(
                "the binary 0xAE1C transport carries channel traffic only; a direct "
                "message has to use the text transport"
            )
        if target.radio_manager is None:
            raise AeicTransportUnavailable("the binary transport needs a radio_manager")

        radio_manager = target.radio_manager
        channel_key = target.conversation_key.upper()
        img_id = new_image_id() if session_id is None else (session_id & 0xFF)

        slot, self_key = await _load_channel_for_binary_send(radio_manager, channel_key)
        blobs = build_image_chunks(
            bitstream,
            metadata.encode(),
            sender_prefix=sender_prefix_for(self_key),
            img_id=img_id,
        )

        async with radio_manager.radio_operation("aeic_channel_data_send", blocking=True) as mc:
            for position, blob in enumerate(blobs):
                frame = build_send_command(slot, DATA_TYPE_AEIC_IMAGE, blob)
                # The firmware answers RESP_CODE_OK, not RESP_CODE_SENT -- a
                # GRP_DATA blob is fire-and-forget and there is nothing to ACK.
                result = await mc.commands.send(frame, [EventType.OK, EventType.ERROR])
                if result is not None and result.type != EventType.ERROR:
                    continue
                detail = getattr(result, "payload", "no response")
                if position == 0:
                    # Nothing is on air yet, so this is still recoverable. A
                    # firmware without command 62 rejects the very first blob,
                    # which is exactly what makes a clean fallback possible --
                    # and why the caller must not retry after a LATER failure.
                    raise AeicChannelDataUnsupported(
                        f"this radio rejected CMD_SEND_CHANNEL_DATA ({CMD_SEND_CHANNEL_DATA}): "
                        f"{detail}"
                    )
                raise AeicTransportUnavailable(
                    f"CMD_SEND_CHANNEL_DATA failed on blob {position} of {len(blobs)} "
                    f"({detail}); the image is partially on air and was not resent"
                )

        radio_manager.note_channel_slot_used(channel_key)
        logger.info(
            "Sent a %d-byte AEIC image as %d GRP_DATA blob(s) on channel %s",
            len(bitstream),
            len(blobs),
            channel_key[:12],
        )
        return AeicSendResult(
            transport=self.name,
            session_id=img_id,
            # The parity blob is a real packet on air and the caller's airtime
            # accounting should see it, so it is counted.
            chunk_count=len(blobs),
            payload_bytes=len(bitstream),
            emitted=[],
        )


async def _load_channel_for_binary_send(radio_manager, channel_key: str) -> tuple[int, bytes]:
    """``(radio slot, our public key)`` ready for a GRP_DATA send.

    GRP_DATA addresses a channel by radio SLOT, so the channel has to be resident
    in one before the blobs go out. This reuses the same slot planner the text
    channel send uses, so the two cannot disagree about which slot holds what.
    """
    from app.repository import ChannelRepository

    channel = await ChannelRepository.get_by_key(channel_key)
    if channel is None:
        raise AeicTransportUnavailable(f"channel {channel_key[:12]} is not known")

    slot, needs_configure, evicted = radio_manager.plan_channel_send_slot(
        channel_key, preferred_slot=0
    )
    async with radio_manager.radio_operation("aeic_channel_data_slot", blocking=True) as mc:
        if needs_configure:
            set_result = await mc.commands.set_channel(
                channel_idx=slot,
                channel_name=channel.name,
                channel_secret=bytes.fromhex(channel.key),
            )
            if set_result is None or set_result.type == EventType.ERROR:
                if evicted:
                    radio_manager.invalidate_cached_channel_slot(evicted)
                raise AeicTransportUnavailable(
                    f"could not load channel {channel_key[:12]} into radio slot {slot}"
                )
            radio_manager.note_channel_slot_loaded(channel_key, slot)
        self_info = mc.self_info or {}
        public_key = self_info.get("public_key") or ""

    if isinstance(public_key, str):
        try:
            public_key = bytes.fromhex(public_key)
        except ValueError:
            public_key = b""
    if len(public_key) < 2:
        raise AeicTransportUnavailable(
            "the radio did not report a public key, which every GRP_DATA chunk "
            "needs as its in-band sender identity"
        )
    return slot, bytes(public_key)


def channel_data_transport_available() -> bool:
    """Whether this install can attempt a GRP_DATA chunk stream.

    The library needs no ``send_channel_data`` helper: ``commands.send`` is a
    generic dispatcher, so the frame is built here and handed straight to it.
    What we cannot check from Python is whether the *firmware* implements
    command 62 -- there is no capability flag for it -- so this returns True
    whenever the dispatcher exists and the real answer comes from the radio
    rejecting the first blob, which
    :class:`ChannelDataTransport` turns into a clean fallback.
    """
    try:
        from meshcore.commands.base import CommandHandlerBase
    except ImportError:  # pragma: no cover - the library is a hard dependency
        return False
    return hasattr(CommandHandlerBase, "send")


def select_transport(
    conversation_type: str = "CHAN", *, prefer_binary: bool = True
) -> AeicTransport:
    """The best transport for this destination.

    Prefers the binary one on CHANNELS: it is what MCO Advanced speaks, so the
    image actually interoperates, and it drops basE91's ~23% expansion. Direct
    messages always use text -- GRP_DATA is a group payload type with no DM
    equivalent.

    Whether the firmware really implements command 62 cannot be known from here;
    the binary transport reports that by rejecting its first blob, and
    :meth:`AeicService.send_image` falls back to text on it.

    ``prefer_binary=False`` forces the text transport; used by tests and
    available to a caller that specifically needs a peer to see message rows.
    """
    if prefer_binary and conversation_type == "CHAN":
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
