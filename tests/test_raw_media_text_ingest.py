"""An rmt1: chunk is transport framing and must never reach the message table.

A picture arriving over the fallback is 30-80 of these. Stored, they would bury
the conversation they belong to, be searched, be fed to bots, and be counted in
unread badges. So the interception happens in ``_store_direct_message`` -- the
one point every DM ingest route passes through -- before storage, dedup and
broadcast.
"""

import pytest

from app.image_protocol import ImageFetchRequest
from app.services import dm_ingest
from app.services.raw_media_text import encode_chunks, reset_pending_transfers

SENDER = "ab" * 32


class _ExplodingRepository:
    """Any use at all is a failure: a chunk must not reach storage or dedup."""

    def __getattr__(self, name):
        raise AssertionError(f"a raw media text chunk reached the message layer via {name}()")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    reset_pending_transfers()
    # Nothing should be dispatched by these tests; they are about storage.
    monkeypatch.setattr(dm_ingest, "note_inbound_text_chunk", _record_chunk)
    _recorded.clear()
    yield
    _recorded.clear()
    reset_pending_transfers()


_recorded: list[tuple[str, str | None]] = []


async def _record_chunk(*, text, sender_key):
    _recorded.append((text, sender_key))
    return True


async def _store(text: str, *, outgoing: bool = False):
    return await dm_ingest._store_direct_message(
        packet_id=None,
        conversation_key=SENDER,
        text=text,
        sender_timestamp=1_700_000_000,
        received_at=1_700_000_000,
        path=None,
        path_len=None,
        outgoing=outgoing,
        txt_type=0,
        signature=None,
        sender_name=None,
        sender_key=None if outgoing else SENDER,
        realtime=True,
        broadcast_fn=lambda *args, **kwargs: None,
        update_last_contacted_key=None,
        best_effort_content_dedup=False,
        linked_packet_dedup=False,
        message_repository=_ExplodingRepository(),
        contact_repository=_ExplodingRepository(),
        raw_packet_repository=_ExplodingRepository(),
    )


def _chunks() -> list[str]:
    return encode_chunks(
        ImageFetchRequest("45abcdef", "aabbccddeeff", tuple(range(140))).encode(),
        transfer_id=12,
    )


async def test_an_inbound_chunk_is_consumed_instead_of_stored():
    chunks = _chunks()
    assert len(chunks) == 2

    for chunk in chunks:
        assert await _store(chunk) is None

    assert [text for text, _sender in _recorded] == chunks
    assert {sender for _text, sender in _recorded} == {SENDER}


async def test_our_own_echoed_chunk_is_dropped_without_being_processed():
    """The radio echoes our own outgoing DMs back through ingest. Feeding those to
    the reassembler would make us answer our own fetch request -- and send the
    whole picture to ourselves.
    """
    for chunk in _chunks():
        assert await _store(chunk, outgoing=True) is None

    assert _recorded == []


async def test_an_image_envelope_is_still_an_ordinary_message():
    """Only the tunnel framing is swallowed. The IE4 envelope is the visible
    message that announces the picture and must keep its bubble."""
    with pytest.raises(AssertionError, match="reached the message layer"):
        await _store("IE4:jbxb73:1:2:g:o:12c")


async def test_ordinary_prose_is_still_an_ordinary_message():
    with pytest.raises(AssertionError, match="reached the message layer"):
        await _store("hello there")
