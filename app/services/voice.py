"""Bounded MeshCore SAR-compatible voice session transport.

The raw-data send that moves fragments is shared with the image format and lives
in :mod:`app.services.raw_media`.
"""

from __future__ import annotations

import asyncio
import logging

from app.keystore import get_public_key
from app.repository import ContactRepository, VoiceRepository
from app.services.raw_media import (
    RAW_MEDIA_FRAGMENT_DELAY_SECONDS,
    RAW_MEDIA_TEXT_CHUNK_DELAY_SECONDS,
    MediaTransport,
    send_raw_to_contact,
)
from app.voice_protocol import (
    MAX_VOICE_PACKETS,
    VoiceFetchRequest,
    VoicePacket,
    encode_fragment_ack,
    parse_fragment_ack,
)
from app.websocket import broadcast_event

logger = logging.getLogger(__name__)


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


async def handle_raw_voice_payload(
    payload: bytes, radio_manager, *, transport: MediaTransport = MediaTransport.RAW
) -> bool:
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
        # The ACK exists for SAR clients that retry on its absence; nothing here
        # consumes one (see the parse at the end of this function). A fragment that
        # arrived over text came from RemoteTerm -- nothing else speaks rmt1: -- so
        # an ACK there is a whole extra message per fragment bought for a consumer
        # that does not exist. That doubles the cost of a recording.
        if peer_key and transport is not MediaTransport.TEXT:
            contact = await ContactRepository.get_by_key(peer_key)
            if contact is not None:
                try:
                    await send_raw_to_contact(
                        radio_manager,
                        contact,
                        encode_fragment_ack(packet.session_id, packet.index),
                        transport=transport,
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
        # A re-ask carries only the gaps, so this loop is both the first delivery
        # and every retry of it.
        gap = (
            RAW_MEDIA_TEXT_CHUNK_DELAY_SECONDS
            if transport is MediaTransport.TEXT
            else RAW_MEDIA_FRAGMENT_DELAY_SECONDS
        )
        sent_count = 0
        for index, data in fragments:
            if wanted is not None and index not in wanted:
                continue
            if sent_count:
                await asyncio.sleep(gap)
            await send_raw_to_contact(
                radio_manager,
                contact,
                VoicePacket(request.session_id, index, data).encode(),
                transport=transport,
            )
            sent_count += 1
        return True

    return parse_fragment_ack(payload) is not None
