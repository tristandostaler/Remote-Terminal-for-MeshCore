"""Keeping inbound media this build cannot decode.

The point of the store is that the bytes are still there when a decoder arrives,
so these tests are mostly about two things: blobs are kept verbatim and in order,
and one picture becomes one arrival rather than one per packet.
"""

import app.repository.unsupported_media as module
from app.repository.messages import MessageRepository
from app.repository.unsupported_media import (
    BLOB_GROUPING_WINDOW_SECONDS,
    MAX_BLOBS_PER_ARRIVAL,
    UnsupportedMediaRepository,
)

CHANNEL = "ab" * 16
MCOIMG = 0xFFF0


async def _append(payload: bytes, *, now: int, data_type: int = MCOIMG, channel: str = CHANNEL):
    return await UnsupportedMediaRepository.append_blob(
        conversation_key=channel,
        data_type=data_type,
        codec_label="MCOimg image (codec not supported here)",
        payload=payload,
        now=now,
    )


async def test_blobs_are_kept_verbatim_and_in_order(test_db):
    module.db = test_db
    payloads = [bytes([i]) * 10 for i in range(4)]

    media_id, started = await _append(payloads[0], now=1000)
    assert started is True
    for offset, payload in enumerate(payloads[1:], start=1):
        again_id, started_again = await _append(payload, now=1000 + offset)
        assert again_id == media_id
        assert started_again is False, "a later packet of one picture started a second arrival"

    # Verbatim and ordered, because a format we cannot parse is one we must not
    # normalise: a future decoder needs exactly what the radio handed us.
    assert await UnsupportedMediaRepository.blobs(media_id) == payloads

    arrival = await UnsupportedMediaRepository.get(media_id)
    assert arrival is not None
    assert arrival.blob_count == 4
    assert arrival.total_bytes == 40
    assert arrival.data_type == MCOIMG
    assert arrival.received_at == 1000


async def test_a_gap_starts_a_new_arrival(test_db):
    """Nothing in an unknown format tells us where one picture ends, so arrival
    time is the only signal. A quiet stretch means a different picture."""
    module.db = test_db

    first, _ = await _append(b"one", now=1000)
    second, started = await _append(b"two", now=1000 + BLOB_GROUPING_WINDOW_SECONDS + 1)

    assert started is True
    assert second != first


async def test_a_different_channel_or_codec_is_a_different_arrival(test_db):
    module.db = test_db

    first, _ = await _append(b"a", now=1000)
    other_channel, started_channel = await _append(b"b", now=1001, channel="cd" * 16)
    other_codec, started_codec = await _append(b"c", now=1002, data_type=0x0120)

    assert started_channel is True and other_channel != first
    assert started_codec is True and other_codec != first


async def test_one_arrival_cannot_grow_without_bound(test_db):
    """The window is extended by every packet, so a peer that never stops would
    otherwise pile everything into one arrival for ever."""
    module.db = test_db

    ids = set()
    for i in range(MAX_BLOBS_PER_ARRIVAL + 5):
        media_id, _started = await _append(b"x", now=1000 + i)
        ids.add(media_id)

    assert len(ids) > 1, "the cap never rolled over to a new arrival"


async def test_deleting_the_message_reclaims_the_payloads(test_db):
    """The whole retention rule. Nothing expires on a timer, so this is the only
    way the bytes are ever released -- and it has to actually work."""
    module.db = test_db
    import app.repository.messages as messages_module

    messages_module.db = test_db

    media_id, _started = await _append(b"payload", now=1000)
    message_id = await MessageRepository.create(
        msg_type="CHAN",
        text=f"mediax:{media_id}",
        received_at=1000,
        conversation_key=CHANNEL,
        sender_key=None,
    )
    assert message_id is not None
    await UnsupportedMediaRepository.bind_message(media_id, message_id)

    bound = await UnsupportedMediaRepository.get(media_id)
    assert bound is not None and bound.message_id == message_id

    await MessageRepository.delete_by_id(message_id)

    assert await UnsupportedMediaRepository.get(media_id) is None
    assert await UnsupportedMediaRepository.blobs(media_id) == []


async def test_an_unknown_arrival_reads_as_absent(test_db):
    module.db = test_db
    assert await UnsupportedMediaRepository.get(9999) is None
    assert await UnsupportedMediaRepository.blobs(9999) == []
