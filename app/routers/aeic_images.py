"""HTTP surface for the AEIC neural image codec.

Three groups of endpoints:

* ``/aeic/status`` and ``/aeic/model/*`` -- is the codec usable, and manage the
  958 MiB model download.
* ``/aeic/send`` -- encode a prepared square and transmit it as ``aei1:`` text.
* ``/aeic/sessions/*`` and ``/aeic/messages/*`` -- read a received image.

The **browser** prepares the pixels: it stretches the photo to a 512x512 square
and POSTs raw packed RGB, exactly as it already does for the IE4 path. The server
never decodes a JPEG, which is why the AEIC extra is two packages and not a full
imaging stack.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response

from app.config import settings
from app.event_handlers import track_pending_ack
from app.imaging.aeic.constants import SQUARE_SIZE
from app.imaging.aeic.ingest import decode_session
from app.imaging.aeic.prepare import RGB_BYTES_EXPECTED, AeicImagePrepareError
from app.imaging.aeic.service import AeicUnavailable, aeic_service
from app.imaging.aeic.text_transport import (
    AeicTextFormatError,
    new_session_id,
)
from app.imaging.aeic.transport import (
    AeicTarget,
    AeicTransportUnavailable,
    resolve_message_budget,
)
from app.models import AeicStatusResponse
from app.repository import (
    AeicImageRepository,
    ChannelRepository,
    ContactRepository,
    MessageRepository,
)
from app.services.message_send import (
    send_channel_message_to_channel,
    send_direct_message_to_contact,
)
from app.services.radio_runtime import radio_runtime as radio_manager
from app.websocket import broadcast_error, broadcast_event

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/aeic", tags=["aeic"])


@router.get("/status", response_model=AeicStatusResponse)
async def get_status() -> dict:
    """Whether this install can encode/decode, plus model download progress."""
    return aeic_service.status()


@router.post("/model/download", response_model=AeicStatusResponse)
async def start_model_download(scope: Literal["full", "send"] = "full") -> dict:
    """Begin (or resume) the model download.

    ``scope=send`` fetches only the 65 MiB that makes *sending* work, for a host
    that cannot spare the 893 MiB of synthesis weights or the ~1.4 GiB of memory
    each reconstruction needs. It is also what the server fetches by itself on
    startup, so this route is for retrying rather than for opting in.

    Idempotent while a download is in flight: a second call is a no-op rather
    than a second concurrent fetch of the same 832 MiB file.
    """
    # Refused when the codec is switched off, rather than spending 958 MiB of
    # somebody's bandwidth on a model that nothing is allowed to load. The UI
    # already hides the button in that state; this covers a direct API call.
    if settings.enable_aeic is False:
        raise HTTPException(
            status_code=409,
            detail=(
                "The AI image codec is switched off on this server "
                "(MESHCORE_ENABLE_AEIC=false); refusing to download its model."
            ),
        )
    if not aeic_service.start_download(send_half_only=scope == "send"):
        logger.info("AEIC model download already in progress; ignoring request")
    return aeic_service.status()


@router.post("/model/download/cancel", response_model=AeicStatusResponse)
async def cancel_model_download() -> dict:
    """Stop the download. Partial files are kept so it can be resumed."""
    aeic_service.cancel_download()
    return aeic_service.status()


@router.post("/send")
async def send_aeic_image(
    request: Request,
    conversation_type: str = Query(pattern="^(PRIV|CHAN)$"),
    conversation_key: str = Query(min_length=12, max_length=64),
    source_width: int = Query(ge=1, le=65535),
    source_height: int = Query(ge=1, le=65535),
) -> dict:
    """Encode a 512x512 RGB square and send it as one or two ``aei1:`` messages.

    ``source_width``/``source_height`` are the ORIGINAL photo's dimensions, not
    the square's. The codec encodes a square with the frame stretched to fit, and
    that stretch is not invertible from the pixels -- so the shape travels in the
    metadata byte and the receiver letterboxes back to it.
    """
    radio_manager.require_connected()
    content_type = request.headers.get("content-type", "").split(";", 1)[0]
    if content_type != "application/octet-stream":
        raise HTTPException(status_code=415, detail="image input must be application/octet-stream")
    rgb = await request.body()
    if len(rgb) != RGB_BYTES_EXPECTED:
        raise HTTPException(
            status_code=422,
            detail=(
                f"expected {RGB_BYTES_EXPECTED} bytes of {SQUARE_SIZE}x{SQUARE_SIZE} "
                f"packed RGB, got {len(rgb)}"
            ),
        )

    # Resolve the destination and its per-message text budget BEFORE spending
    # ~0.3 s of CPU on the encode.
    if conversation_type == "PRIV":
        contact = await ContactRepository.get_by_key_or_prefix(conversation_key)
        if contact is None or len(contact.public_key) != 64:
            raise HTTPException(status_code=404, detail="image destination contact not found")
        budget = await resolve_message_budget("PRIV")
        channel = None
    else:
        channel = await ChannelRepository.get_by_key(conversation_key)
        if channel is None:
            raise HTTPException(status_code=404, detail="image destination channel not found")
        contact = None
        budget = await resolve_message_budget("CHAN", radio_manager=radio_manager)

    session_id = new_session_id()
    normalized_key = contact.public_key if contact else channel.key  # type: ignore[union-attr]

    async def emit_text(chunk: str):
        """Send one framed chunk through the normal message path.

        The transport calls this per chunk and does not care which send it is;
        that indirection is what lets the binary 0xAE1C transport replace it
        without this route changing.
        """
        if contact is not None:
            return await send_direct_message_to_contact(
                contact=contact,
                text=chunk,
                radio_manager=radio_manager,
                broadcast_fn=broadcast_event,
                track_pending_ack_fn=track_pending_ack,
                now_fn=time.time,
                message_repository=MessageRepository,
                contact_repository=ContactRepository,
            )
        return await send_channel_message_to_channel(
            channel=channel,
            channel_key_upper=channel.key.upper(),  # type: ignore[union-attr]
            key_bytes=bytes.fromhex(channel.key),  # type: ignore[union-attr]
            text=chunk,
            radio_manager=radio_manager,
            broadcast_fn=broadcast_event,
            error_broadcast_fn=broadcast_error,
            now_fn=time.time,
            temp_radio_slot=0,
            message_repository=MessageRepository,
        )

    target = AeicTarget(
        conversation_type=conversation_type,  # type: ignore[arg-type]
        conversation_key=normalized_key,
        emit_text=emit_text,
        message_budget=budget,
        radio_manager=radio_manager,
    )

    try:
        result, bitstream, metadata = await aeic_service.send_image(
            rgb,
            target,
            # The browser sends an already-squared 512px frame, so the original
            # shape has to come from the query rather than from the pixels.
            source_width=source_width,
            source_height=source_height,
            session_id=session_id,
        )
    except AeicUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (AeicTextFormatError, AeicImagePrepareError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except AeicTransportUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    # The session row is written by aeic_service.send_image, so both this route
    # and the bot send path record an outgoing image identically. Its key comes
    # back on the result rather than being recomputed here: it is derived from
    # the sent message's id, not from the 1296-value wire session id.
    return {
        "session_key": result.storage_key,
        "transport": result.transport,
        "bitstream_bytes": result.payload_bytes,
        "chunk_count": result.chunk_count,
        "messages": [m.model_dump() if m is not None else None for m in result.emitted],
    }


@router.get("/messages/{message_id}")
async def get_session_for_message(message_id: int) -> dict:
    """The AEIC session anchored to a message, for rendering it as a picture."""
    session = await AeicImageRepository.get_by_message(message_id)
    if session is None:
        raise HTTPException(status_code=404, detail="no AEIC image session for that message")
    return _session_payload(session)


@router.get("/sessions/{session_key}")
async def get_session(session_key: str) -> dict:
    session = await AeicImageRepository.get(session_key)
    if session is None:
        raise HTTPException(status_code=404, detail="AEIC image session not found")
    return _session_payload(session)


@router.post("/sessions/{session_key}/decode")
async def retry_decode(session_key: str) -> dict:
    """Decode (or re-decode) a reassembled session.

    The path that matters: an image received before the model was installed is
    held as a stored bitstream, and this turns it into a picture afterwards
    without asking the sender to retransmit.
    """
    session = await AeicImageRepository.get(session_key)
    if session is None:
        raise HTTPException(status_code=404, detail="AEIC image session not found")
    if not session["bitstream"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"session is incomplete ({len(session['chunks'])} of "
                f"{session['total_chunks']} chunks received)"
            ),
        )
    reason = aeic_service.unavailable_reason(for_decode=True)
    if reason is not None:
        raise HTTPException(status_code=503, detail=reason)
    await decode_session(session_key, bytes(session["bitstream"]), broadcast_event)
    refreshed = await AeicImageRepository.get(session_key)
    assert refreshed is not None
    return _session_payload(refreshed)


@router.get("/sessions/{session_key}/content")
async def get_session_content(session_key: str) -> Response:
    """The decoded picture as a PNG."""
    session = await AeicImageRepository.get(session_key)
    if session is None:
        raise HTTPException(status_code=404, detail="AEIC image session not found")
    if not session["png"]:
        detail = session["decode_error"] or (
            "image has not been decoded yet"
            if session["bitstream"]
            else f"session is incomplete ({len(session['chunks'])} of "
            f"{session['total_chunks']} chunks received)"
        )
        raise HTTPException(status_code=409, detail=detail)
    return Response(
        bytes(session["png"]),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _session_payload(session: dict) -> dict:
    received = len(session["chunks"])
    total = session["total_chunks"]
    return {
        "session_key": session["session_key"],
        "message_id": session["message_id"],
        "state": session["state"],
        "square_size": session["square_size"],
        "aspect_code": session["aspect_code"],
        "rate_code": session["rate_code"],
        "total_chunks": total,
        "received_chunks": received,
        "missing_indices": [i for i in range(total) if i not in session["chunks"]],
        "bitstream_bytes": len(session["bitstream"]) if session["bitstream"] else 0,
        "decoded": bool(session["png"]),
        "decode_error": session["decode_error"],
    }
