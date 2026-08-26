"""What the conversation shows for media this build cannot decode.

The box in the conversation reads its detail from here, and its retry posts here.
Retry exists today knowing it will usually fail: the point is that the bytes are
kept, so the same button turns an image received months ago into a picture on the
day a decoder for its codec ships. See migration 079.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.imaging.aeic.channel_data import DATA_TYPE_MCO_APP, DATA_TYPE_MCO_IMAGE
from app.repository import UnsupportedMediaRepository

router = APIRouter(prefix="/unsupported-media", tags=["unsupported-media"])


def _decoder_reason(data_type: int) -> str:
    """Why this arrival cannot be shown, in the words the box will display."""
    if data_type in (DATA_TYPE_MCO_IMAGE, DATA_TYPE_MCO_APP):
        return (
            "This is an MCOimg picture. RemoteTerm has no decoder for that codec, so it "
            "cannot be shown here yet. The data has been kept: if MCOimg support is added, "
            "this picture will open without the sender resending it."
        )
    return (
        f"This picture arrived as an unrecognised format (data type 0x{data_type:04X}). "
        "The data has been kept, so it can be decoded if support for that format is added."
    )


def _payload(arrival) -> dict:
    return {
        "id": arrival.id,
        "conversation_key": arrival.conversation_key,
        "data_type": arrival.data_type,
        "codec_label": arrival.codec_label,
        "received_at": arrival.received_at,
        "blob_count": arrival.blob_count,
        "total_bytes": arrival.total_bytes,
        "decoded": False,
        "reason": _decoder_reason(arrival.data_type),
    }


@router.get("/{media_id}")
async def get_unsupported_media(media_id: int) -> dict:
    arrival = await UnsupportedMediaRepository.get(media_id)
    if arrival is None:
        raise HTTPException(status_code=404, detail="kept media not found")
    return _payload(arrival)


@router.post("/{media_id}/decode")
async def retry_decode(media_id: int) -> dict:
    """Try again to turn a kept arrival into a picture.

    There is nothing to dispatch to yet, so this reports why rather than
    pretending to work. It is wired up regardless, because the alternative -- no
    button until a decoder exists -- means the person looking at the box has no
    way to find out that anything changed, and the kept bytes would sit there
    unreachable. When a decoder lands, it is called from here and every arrival
    already in the database becomes readable.
    """
    arrival = await UnsupportedMediaRepository.get(media_id)
    if arrival is None:
        raise HTTPException(status_code=404, detail="kept media not found")
    return _payload(arrival)
