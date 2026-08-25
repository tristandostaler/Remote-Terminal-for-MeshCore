"""Bounded MeshCore SAR-compatible voice session transport.

Also home to :func:`send_raw_to_contact`, the raw-data send that both the voice
and the SAR image transports use to move fragments. Anything shared with images
is worded for "raw media", not voice: its failures surface verbatim in the UI,
and "raw voice send failed" while opening a picture told the reader nothing.
"""

from __future__ import annotations

import asyncio
import logging

from meshcore import EventType

from app.keystore import get_public_key
from app.repository import ContactAdvertPathRepository, ContactRepository, VoiceRepository
from app.voice_protocol import (
    MAX_VOICE_PACKETS,
    VoiceFetchRequest,
    VoicePacket,
    encode_fragment_ack,
    parse_fragment_ack,
)
from app.websocket import broadcast_event

logger = logging.getLogger(__name__)
MAX_RAW_MEDIA_PATH_LEN = 0x3F
"""Ceiling of the 6-bit path-length field: the top two bits carry the hash mode."""
MAX_RAW_MEDIA_PATH_BYTES = 64
"""MeshCore's ``MAX_PATH_SIZE``. A wider path hash buys fewer hops inside it."""
RAW_MEDIA_FRAGMENT_DELAY_SECONDS = 0.350
UNSUPPORTED_CMD_ERROR_CODE = 1
"""``ERR_CODE_UNSUPPORTED_CMD`` in the companion protocol's error table."""


class RawDataUnsupportedError(RuntimeError):
    """The node's firmware does not implement ``CMD_SEND_RAW_DATA`` (25).

    A distinct type because this is not a transient radio failure: without that
    command neither the SAR image nor the voice transport can move a single
    fragment in either direction, so retrying can never succeed and the caller
    should say what has to change instead.
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


async def send_raw_to_contact(radio_manager, contact, payload: bytes) -> None:
    """Send one raw-data frame to a contact. Shared by voice AND image transfer."""
    route = await _raw_route_for_contact(contact)
    frame = _raw_frame_for_contact(contact, payload, route=route)
    async with radio_manager.radio_operation("raw_media_send", blocking=True) as mc:
        result = await mc.commands.send_raw_data(frame)
    if result is None or result.type == EventType.ERROR:
        detail = result.payload if result is not None else "no radio response"
        if result is not None and _is_unsupported_cmd(result.payload):
            version = getattr(radio_manager, "firmware_version", None)
            raise RawDataUnsupportedError(
                f"This node's firmware{f' ({version})' if version else ''} cannot send "
                "raw data packets, which the standard image and voice formats use to "
                "move fragments. Update the node to a firmware build that supports "
                "CMD_SEND_RAW_DATA."
            )
        raise RuntimeError(f"raw data send failed: {detail}")


async def request_voice_session(radio_manager, session: dict) -> None:
    peer_key = session.get("peer_public_key")
    if not peer_key:
        raise ValueError("voice sender identity is unavailable")
    contact = await ContactRepository.get_by_key(peer_key)
    if contact is None:
        raise ValueError("voice sender is not a known contact")
    public_key = get_public_key()
    if public_key is None or len(public_key) < 6:
        raise RuntimeError("local radio public key is unavailable")
    have = {index for index, _data in session["fragments"]}
    missing = tuple(index for index in range(session["packet_count"]) if index not in have)
    request = VoiceFetchRequest(
        session_id=session["session_id"],
        requester_key6=public_key[:6].hex(),
        missing_indices=missing if have else (),
    )
    await send_raw_to_contact(radio_manager, contact, request.encode())


async def handle_raw_voice_payload(payload: bytes, radio_manager) -> bool:
    packet = VoicePacket.parse(payload)
    if packet is not None:
        session = await VoiceRepository.get(packet.session_id)
        if session is None or packet.index >= session["packet_count"]:
            logger.debug(
                "Ignoring voice fragment for unknown/invalid session %s", packet.session_id
            )
            return True
        await VoiceRepository.add_fragment(packet.session_id, packet.index, packet.codec2_data)
        broadcast_event(
            "voice_session",
            {
                "session_id": packet.session_id,
                "received": len(session["fragments"]) + 1,
                "total": session["packet_count"],
            },
        )
        peer_key = session.get("peer_public_key")
        if peer_key:
            contact = await ContactRepository.get_by_key(peer_key)
            if contact is not None:
                try:
                    await send_raw_to_contact(
                        radio_manager, contact, encode_fragment_ack(packet.session_id, packet.index)
                    )
                except Exception as exc:
                    logger.debug("Voice fragment ACK failed: %s", exc)
        return True

    request = VoiceFetchRequest.parse(payload)
    if request is not None:
        contact = await ContactRepository.get_by_key_or_prefix(request.requester_key6)
        if contact is None:
            logger.warning("Voice fetch requester %s is unknown", request.requester_key6)
            return True
        session = await VoiceRepository.get(request.session_id)
        if session is None:
            return True
        wanted = set(request.missing_indices) if request.missing_indices else None
        fragments = session["fragments"][:MAX_VOICE_PACKETS]
        sent_count = 0
        for index, data in fragments:
            if wanted is not None and index not in wanted:
                continue
            if sent_count:
                await asyncio.sleep(RAW_MEDIA_FRAGMENT_DELAY_SECONDS)
            await send_raw_to_contact(
                radio_manager, contact, VoicePacket(request.session_id, index, data).encode()
            )
            sent_count += 1
        return True

    return parse_fragment_ack(payload) is not None
