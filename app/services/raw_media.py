"""The raw-data transport shared by the SAR image and voice formats.

Both formats advertise themselves as a text envelope and then move their
fragments as raw MeshCore packets, through ``CMD_SEND_RAW_DATA``. That send is
one piece of code with two callers, and it used to live in
``app/services/voice.py`` simply because voice landed first -- so a fix to how
images are fetched showed up as a change to the voice module, and a failure while
opening a picture reported itself as a voice error. It has its own home now.

Nothing here knows which format it is carrying, so nothing here may say "voice"
or "image": these messages surface verbatim in the UI.

There are two transports, and this module picks between them. Two rules decide,
and only these two:

**What we initiate** is decided by ``contacts.raw_media_text_transport`` (default
on). On, our fetch requests go out over :mod:`app.services.raw_media_text` --
this is a switch, not a fallback, so it does not wait for a raw send to fail
first. Off, they go out raw and firmware without ``CMD_SEND_RAW_DATA`` raises
:class:`RawDataUnsupportedError` rather than quietly spending 2.5x the airtime.

**What we send in reply** mirrors the transport the request arrived on, whatever
the switch says. A raw fetch request answered over text could not be read by the
client that sent it, and a text request answered raw would leave our radio and
arrive nowhere -- so mirroring is not a preference here, it is the only reply
that can work. It is also what keeps MeshCore SAR clients working with the
switch on: they ask raw, they get raw.

Read :mod:`app.services.raw_media_text` before changing anything here -- the
tunnel costs roughly 2.5x the airtime, so the choice is not a detail.
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum

from meshcore import EventType

from app.repository import ContactAdvertPathRepository
from app.services import raw_media_text

logger = logging.getLogger(__name__)

MAX_RAW_MEDIA_PATH_LEN = 0x3F
"""Ceiling of the 6-bit path-length field: the top two bits carry the hash mode."""
MAX_RAW_MEDIA_PATH_BYTES = 64
"""MeshCore's ``MAX_PATH_SIZE``. A wider path hash buys fewer hops inside it."""
RAW_MEDIA_FRAGMENT_DELAY_SECONDS = 0.350
RAW_MEDIA_TEXT_CHUNK_DELAY_SECONDS = 1.2
"""Gap between text-tunnel chunks. Much wider than the raw gap above, because a
chunk is a full-sized message rather than a bare fragment: firing 40 of them off
back to back would hold the band and lose most of them."""
UNSUPPORTED_CMD_ERROR_CODE = 1
"""``ERR_CODE_UNSUPPORTED_CMD`` in the companion protocol's error table."""


class MediaTransport(Enum):
    """Which of the two transports carried, or will carry, a payload.

    Threaded through :func:`dispatch_raw_media_payload` so a handler answering a
    request knows how that request reached it. Without it the reply transport
    would have to be guessed from remembered peer behaviour, which is a second
    source of truth for something the arriving packet already states.
    """

    RAW = "raw"
    TEXT = "text"


class RawDataUnsupportedError(RuntimeError):
    """The node's firmware does not implement ``CMD_SEND_RAW_DATA`` (25).

    A distinct type because this is not a transient radio failure: without that
    command neither the SAR image nor the voice transport can move a single
    fragment in either direction, so retrying can never succeed and the caller
    should say what has to change instead.

    Only reaches a caller when the text transport is unavailable too: switched
    off for the contact, or the contact is not one we can send a message to. With
    the switch on, a payload that has to go raw (mirroring a raw request) and
    cannot is caught here and re-sent over text instead.
    """


def _is_unsupported_cmd(payload: object) -> bool:
    return isinstance(payload, dict) and (
        payload.get("error_code") == UNSUPPORTED_CMD_ERROR_CODE
        or payload.get("code_string") == "ERR_CODE_UNSUPPORTED_CMD"
    )


def _raw_frame_for_contact(
    contact, payload: bytes, *, route: tuple[str, int, int] | None = None
) -> bytes:
    path, path_len, hash_mode = route or contact.effective_route_tuple()
    if path_len < 0:
        raise ValueError("raw media transfer requires a direct or learned route")
    if hash_mode not in (0, 1, 2):
        raise ValueError("contact route has an unsupported path hash mode")
    # Bound by what the packet header can express, nothing tighter. This used to
    # refuse anything past 3 hops, which is not a protocol limit -- it made
    # pictures and recordings unfetchable from any contact further away than
    # that, reported as though the mesh forbade it. Ordinary messages already
    # travel those routes; a fetch request is one small packet on the same path.
    hop_width = hash_mode + 1
    max_hops = min(MAX_RAW_MEDIA_PATH_LEN, MAX_RAW_MEDIA_PATH_BYTES // hop_width)
    if path_len > max_hops:
        raise ValueError(
            f"raw media transfer cannot use a {path_len}-hop route: the packet header "
            f"holds at most {max_hops} hops at this path hash width"
        )
    path_bytes = bytes.fromhex(path)
    if len(path_bytes) != path_len * hop_width:
        raise ValueError("contact route is not valid for raw media")
    packed_path_len = (hash_mode << 6) | path_len
    return bytes([packed_path_len]) + path_bytes + payload


async def _raw_route_for_contact(contact) -> tuple[str, int, int]:
    """Resolve a non-flood raw route, with a direct-advert zero-hop fallback."""
    route = contact.effective_route_tuple()
    if route[1] >= 0:
        return route

    advert_paths = await ContactAdvertPathRepository.get_recent_for_contact(
        contact.public_key, limit=1
    )
    if advert_paths and advert_paths[0].path_len == 0 and not advert_paths[0].path:
        logger.info(
            "Using most recently observed direct advert as zero-hop raw media route for %s",
            contact.public_key[:12],
        )
        return "", 0, 0
    return route


def uses_text_transport(contact) -> bool:
    """Whether this contact's media fragments travel as text. Defaults to yes.

    ``getattr`` rather than attribute access so a caller holding a contact-shaped
    object without the column (an older row, a test double) still gets the
    default rather than an AttributeError inside a send.
    """
    return bool(getattr(contact, "raw_media_text_transport", True))


def _resolve_transport(radio_manager, contact, requested: MediaTransport | None) -> MediaTransport:
    """Pick the transport for one send.

    ``requested`` is the transport a request arrived on, and it wins: a reply has
    to go back the way it came to be readable at all. It is ``None`` when we are
    the ones starting the exchange, and then the contact's switch decides.

    The one case where a pinned ``RAW`` is overridden is a node whose firmware has
    already answered ``ERR_CODE_UNSUPPORTED_CMD`` on this connection. Raw cannot
    work there, so text is the only remaining way to answer -- and going straight
    to it saves each of 40 fragments a doomed round trip rediscovering the limit.
    """
    if not uses_text_transport(contact):
        return MediaTransport.RAW
    if requested is None:
        return MediaTransport.TEXT
    if requested is MediaTransport.RAW and getattr(radio_manager, "raw_data_unsupported", False):
        return MediaTransport.TEXT
    return requested


async def send_raw_to_contact(
    radio_manager,
    contact,
    payload: bytes,
    *,
    transport: MediaTransport | None = None,
) -> None:
    """Send one raw-media payload to a contact, over whichever transport applies.

    ``transport`` is the transport a request arrived on, for a send that answers
    one; leave it ``None`` when starting an exchange. See :func:`_resolve_transport`.
    """
    if _resolve_transport(radio_manager, contact, transport) is MediaTransport.TEXT:
        await send_text_tunnel_to_contact(radio_manager, contact, payload)
        return

    route = await _raw_route_for_contact(contact)
    frame = _raw_frame_for_contact(contact, payload, route=route)
    async with radio_manager.radio_operation("raw_media_send", blocking=True) as mc:
        result = await mc.commands.send_raw_data(frame)
    if result is None or result.type == EventType.ERROR:
        detail = result.payload if result is not None else "no radio response"
        if result is not None and _is_unsupported_cmd(result.payload):
            # A property of this node's firmware, not of this payload or this
            # contact: remember it so the remaining fragments do not each spend a
            # doomed round trip discovering it again.
            radio_manager.raw_data_unsupported = True
            if uses_text_transport(contact):
                logger.info(
                    "Node cannot send raw data; carrying %d bytes over the text transport instead",
                    len(payload),
                )
                await send_text_tunnel_to_contact(radio_manager, contact, payload)
                return
            version = getattr(radio_manager, "firmware_version", None)
            raise RawDataUnsupportedError(
                f"This node's firmware{f' ({version})' if version else ''} cannot send "
                "raw data packets, which the standard image and voice formats use to "
                "move fragments. Either update the node to a firmware build that "
                "supports CMD_SEND_RAW_DATA, or turn on the text transport for this "
                "contact in Conversation features."
            )
        raise RuntimeError(f"raw data send failed: {detail}")


async def _radio_destination_for_contact(radio_manager, contact) -> dict:
    """Put the contact on the radio and return the dict ``send_msg`` wants.

    Its own short radio operation: the transfer that follows takes one operation
    per chunk rather than holding the lock for the whole minute-long send.
    """
    contact_data = contact.to_radio_dict()
    async with radio_manager.radio_operation("raw_media_text_prepare", blocking=True) as mc:
        add_result = await mc.commands.add_contact(contact_data)
        if add_result is not None and add_result.type == EventType.ERROR:
            logger.warning(
                "Could not add contact to radio before a text tunnel send: %s",
                add_result.payload,
            )
        return mc.get_contact_by_key_prefix(contact.public_key[:12]) or contact_data


async def send_text_tunnel_to_contact(radio_manager, contact, payload: bytes) -> None:
    """Carry one raw-media payload to a contact as ``rmt1:`` direct messages.

    Sent below the message layer on purpose -- no message row, no bubble, no ACK
    tracking. This is transport framing, and a 40-message picture transfer would
    otherwise bury the conversation it belongs to.
    """
    chunks = raw_media_text.encode_chunks(payload)
    destination = await _radio_destination_for_contact(radio_manager, contact)
    logger.info(
        "Sending %d byte raw media payload to %s as %d text chunk(s)",
        len(payload),
        contact.public_key[:12],
        len(chunks),
    )
    for index, chunk in enumerate(chunks):
        if index:
            await asyncio.sleep(RAW_MEDIA_TEXT_CHUNK_DELAY_SECONDS)
        async with radio_manager.radio_operation("raw_media_text_send", blocking=True) as mc:
            result = await mc.commands.send_msg(
                dst=destination, msg=chunk, timestamp=int(time.time())
            )
        if result is None or result.type == EventType.ERROR:
            detail = result.payload if result is not None else "no radio response"
            raise RuntimeError(f"raw media text send failed: {detail}")


async def dispatch_raw_media_payload(
    payload: bytes, radio_manager, *, transport: MediaTransport = MediaTransport.RAW
) -> None:
    """Hand one raw-media payload to whichever format claims it.

    The single dispatch point for both transports. A real ``RAW_DATA`` push and a
    reassembled text transfer are the same bytes and must be treated identically;
    a second copy of this sequence is how the two would drift apart.
    """
    from app.services.image import handle_raw_image_payload
    from app.services.voice import handle_raw_voice_payload

    if not await handle_raw_image_payload(payload, radio_manager, transport=transport):
        await handle_raw_voice_payload(payload, radio_manager, transport=transport)


async def note_inbound_text_chunk(*, text: str, sender_key: str | None, radio_manager=None) -> bool:
    """Consume one inbound ``rmt1:`` message. True when it was one.

    Called from DM ingest *before* storage: a tunnel chunk is transport framing,
    not conversation, so it must never become a message row. True is returned for
    any well-formed chunk, including ones that cannot be acted on -- storing it
    would only put machine noise in the chat.
    """
    chunk = raw_media_text.parse_chunk(text)
    if chunk is None:
        return False
    if not sender_key:
        logger.debug("Dropping a raw media text chunk from an unidentified sender")
        return True
    payload = raw_media_text.note_chunk(chunk, sender_key=sender_key, now=time.time())
    if payload is None:
        return True
    if radio_manager is None:
        from app.services.radio_runtime import radio_runtime

        radio_manager = radio_runtime
    logger.info(
        "Reassembled a %d byte raw media payload from %d text chunk(s) from %s",
        len(payload),
        chunk.total,
        sender_key[:12],
    )
    await dispatch_raw_media_payload(payload, radio_manager, transport=MediaTransport.TEXT)
    return True
