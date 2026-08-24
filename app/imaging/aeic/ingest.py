"""Reassembly of inbound ``aei1:`` image chunks.

ONE entry point, :func:`note_inbound_chunk`, called from every message ingest
route. There are three, and missing any of them means an image that silently
never appears:

* ``create_message_from_decrypted``  -- channel messages off raw RF
* ``create_fallback_channel_message`` -- channel messages from the get_msg drain
* ``_store_direct_message``          -- direct messages

The chunk text is left in the message body rather than being replaced, exactly
as the IE4 path leaves its ``IE4:`` envelope there. That keeps history, search
and dedup working on what actually crossed the air, and lets the frontend
recognise the message and render the picture in its place.

Decoding is deliberately decoupled from reassembly: it needs a 958 MiB bundle
that may not be installed, and it costs ~5 s of CPU. Reassembly stores the
bitstream and announces the image; the decode runs afterwards as a background
task, or later, when the model arrives.
"""

from __future__ import annotations

import asyncio
import logging

from app.imaging.aeic.service import aeic_service
from app.imaging.aeic.text_transport import is_aeic_chunk, parse_chunk, reassemble
from app.repository import AeicImageRepository
from app.repository.aeic_image import session_key as make_session_key

logger = logging.getLogger(__name__)

_decode_tasks: set[asyncio.Task] = set()


def aeic_body(text: str, sender_name: str | None) -> str:
    """Strip the ``"sender: "`` presentation prefix a channel message carries.

    The stored text of a channel message is ``f"{sender}: {body}"``; the protocol
    body is what came off the air. Mirrors ``_image_envelope_body`` in
    :mod:`app.routers.images`.
    """
    if sender_name:
        prefix = f"{sender_name}: "
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text


async def note_inbound_chunk(
    *,
    text: str,
    message_id: int | None,
    conversation_type: str,
    conversation_key: str,
    peer_public_key: str | None,
    sender_name: str | None = None,
    broadcast_fn=None,
) -> bool:
    """Record one inbound chunk. Returns True if the text was an AEIC chunk.

    Never raises: this runs inside message ingest, and a malformed chunk from a
    peer must not cost us the message.
    """
    body = aeic_body(text, sender_name)
    if not is_aeic_chunk(body):
        return False
    chunk = parse_chunk(body)
    if chunk is None:
        logger.debug("Ignoring malformed aei1: chunk: %r", body[:32])
        return False

    key = make_session_key(peer_public_key or conversation_key, chunk.session_id)
    try:
        await AeicImageRepository.enforce_cache_limit()
        if chunk.metadata is not None:
            await AeicImageRepository.create_session(
                key=key,
                message_id=message_id,
                direction="incoming",
                conversation_type=conversation_type,
                conversation_key=conversation_key,
                peer_public_key=peer_public_key,
                square_size=chunk.metadata.square_size,
                aspect_code=chunk.metadata.aspect_code,
                rate_code=chunk.metadata.rate_code,
                total_chunks=chunk.total,
                state="receiving",
            )
        elif await AeicImageRepository.get(key) is None:
            # A later chunk arrived before chunk 0. Nothing to anchor it to --
            # chunk 0 carries the metadata byte and the message id -- so drop it
            # and let the sender's chunk 0 (or a resend) create the session.
            logger.info(
                "aei1: chunk %d/%d for %s arrived before chunk 0; dropping",
                chunk.index,
                chunk.total,
                key,
            )
            return True
        await AeicImageRepository.add_chunk(key, chunk.index, chunk.payload)
    except Exception:
        logger.exception("Failed to store an inbound aei1: chunk for %s", key)
        return True

    session = await AeicImageRepository.get(key)
    if session is None:
        return True
    received = len(session["chunks"])
    total = session["total_chunks"]
    if broadcast_fn is not None:
        broadcast_fn(
            "aeic_image_session",
            {
                "session_key": key,
                "message_id": session["message_id"],
                "received": received,
                "total": total,
                "state": session["state"],
            },
        )

    if received == total and not session["bitstream"]:
        bitstream = reassemble(session["chunks"], total)
        if bitstream is None:
            return True
        await AeicImageRepository.store_bitstream(key, bitstream)
        logger.info(
            "Reassembled a %d-byte AEIC bitstream from %d message(s) for %s",
            len(bitstream),
            total,
            key,
        )
        _schedule_decode(key, bitstream, broadcast_fn)
    return True


def _schedule_decode(key: str, bitstream: bytes, broadcast_fn) -> None:
    """Decode in the background if the model is ready; otherwise leave it stored.

    A stored bitstream is not a failure state: the picture is 150 bytes that
    already crossed the mesh, and it will render as soon as the bundle is
    installed. That is why :func:`decode_session` is a separate, retryable step.
    """
    reason = aeic_service.unavailable_reason(for_decode=True)
    if reason is not None:
        logger.info("Holding AEIC image %s undecoded: %s", key, reason)
        return
    task = asyncio.create_task(decode_session(key, bitstream, broadcast_fn))
    _decode_tasks.add(task)
    task.add_done_callback(_decode_tasks.discard)


async def decode_session(key: str, bitstream: bytes | None, broadcast_fn=None) -> bool:
    """Decode a reassembled session into a PNG. Safe to retry."""
    if bitstream is None:
        session = await AeicImageRepository.get(key)
        if session is None or not session["bitstream"]:
            return False
        bitstream = bytes(session["bitstream"])
    try:
        png = await aeic_service.decode_to_png(bitstream)
    except Exception as exc:  # noqa: BLE001 - the reason is shown to the user
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning("AEIC decode failed for %s: %s", key, detail)
        await AeicImageRepository.store_decode_error(key, detail)
        if broadcast_fn is not None:
            broadcast_fn("aeic_image_session", {"session_key": key, "error": detail})
        return False
    await AeicImageRepository.store_png(key, png)
    logger.info("Decoded AEIC image %s into a %d-byte PNG", key, len(png))
    if broadcast_fn is not None:
        session = await AeicImageRepository.get(key)
        broadcast_fn(
            "aeic_image_session",
            {
                "session_key": key,
                "message_id": session["message_id"] if session else None,
                "received": session["total_chunks"] if session else None,
                "total": session["total_chunks"] if session else None,
                "state": "decoded",
            },
        )
    return True
