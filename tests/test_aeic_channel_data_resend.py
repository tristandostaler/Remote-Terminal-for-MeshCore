"""Sending the same picture to a channel more than once.

These run against a real database rather than a spy, because the behaviour being
pinned belongs to the CHAN dedup unique index -- and the bug was that the index
and the content-addressed session key disagreed about what "the same picture"
means.
"""

from __future__ import annotations

from app.imaging.aeic import channel_data_ingest as cdi
from app.imaging.aeic.channel_data import (
    DATA_TYPE_AEIC_IMAGE,
    ParsedChannelData,
    build_image_chunks,
)
from app.imaging.aeic.channel_data_ingest import handle_channel_data
from app.imaging.aeic.text_transport import AeicStreamMetadata

CHANNEL = "0d24f5830b449668b8c2" + "00" * 6
META = AeicStreamMetadata(square_size=512, aspect_code=2).encode()
BITSTREAM = bytes(range(123))
"""123 bytes: what MCO Advanced put on air for a 512x512 photo, in one 128-byte
blob with the recovery packet switched off. Its own capacity note puts the ft32
mean at 155.8 B, so a single-chunk image is the ordinary case, not an edge one."""


class _Harness:
    """A real database, decoding off, and a clock we control."""

    def __init__(self, test_db, monkeypatch):
        import app.repository.aeic_image as aeic_mod
        import app.repository.messages as messages_mod
        import app.repository.unsupported_media as unsup_mod
        from app.config import settings

        for module in (messages_mod, aeic_mod, unsup_mod):
            module.db = test_db
        # Decoding off is the reported situation, and it is the case that must
        # still put something in the conversation.
        monkeypatch.setattr(settings, "enable_aeic", False)
        self.db = test_db
        self.announced: list[str] = []
        self._now = 1000.0
        monkeypatch.setattr(cdi.time, "time", lambda: self._now)

    def _broadcast(self, name, payload, **_kw):
        if name == "message":
            self.announced.append(payload["text"])

    async def receive(self, *, at: float, img_id: int, bitstream: bytes = BITSTREAM) -> None:
        """One arrival of an image, as a fresh sender would send it."""
        self._now = at
        blob = build_image_chunks(bitstream, META, sender_prefix=0x1234, img_id=img_id)[0]
        await handle_channel_data(
            ParsedChannelData(0, 1, 0xFF, DATA_TYPE_AEIC_IMAGE, blob),
            conversation_key=CHANNEL,
            broadcast_fn=self._broadcast,
        )

    async def marker_rows(self) -> list[str]:
        async with self.db.readonly() as conn:
            async with conn.execute(
                "SELECT text FROM messages WHERE conversation_key = ? ORDER BY id",
                (CHANNEL,),
            ) as cursor:
                return [row["text"] for row in await cursor.fetchall()]

    async def sessions(self) -> list[str]:
        async with self.db.readonly() as conn:
            async with conn.execute("SELECT session_key FROM aeic_image_sessions") as cursor:
                return [row["session_key"] for row in await cursor.fetchall()]


async def test_each_send_of_one_picture_gets_its_own_bubble(test_db, monkeypatch):
    """The reported failure: the same photo sent three times showed up never.

    The session key is a hash of the bitstream, the marker text is that key, and
    the CHAN dedup index covers the marker text -- so with no sender_timestamp
    every resend was swallowed by `INSERT OR IGNORE`, `create` returned None, and
    nothing was announced. Re-sending is exactly what someone does when a picture
    does not appear, so the obvious way to check was also guaranteed to fail.
    """
    harness = _Harness(test_db, monkeypatch)
    cdi.reassembler = cdi.ChannelDataReassembler()

    # The real timings: 21:44:35, then 21:49:00 and 21:49:14.
    await harness.receive(at=1000, img_id=5)
    await harness.receive(at=1265, img_id=6)
    await harness.receive(at=1279, img_id=7)

    assert len(await harness.marker_rows()) == 3, "a resend of the same photo left no row"
    assert len(harness.announced) == 3, "a resend was never pushed to the conversation"


async def test_the_picture_is_still_stored_only_once(test_db, monkeypatch):
    """Three bubbles, one bitstream and one decoded PNG.

    Content addressing is what buys this, and it is the reason the key is NOT
    made unique per arrival: a 600 KB PNG per resend would be a poor trade for a
    bubble. The rows all name the same session, which is why the client resolves
    a marker by its key rather than by message id -- the session can only be
    bound to the first row.
    """
    harness = _Harness(test_db, monkeypatch)
    cdi.reassembler = cdi.ChannelDataReassembler()

    await harness.receive(at=1000, img_id=5)
    await harness.receive(at=1265, img_id=6)

    assert len(await harness.sessions()) == 1
    assert len(set(await harness.marker_rows())) == 1, "the rows name different sessions"


async def test_a_repeater_reflood_does_not_add_a_bubble(test_db, monkeypatch):
    """Duplicate suppression lives in one place, and this proves it still does.

    A re-flooded copy carries the same sender prefix and image id, so
    `_already_finished` drops it before it reaches storage. That guard -- not the
    message index -- is what a duplicate means here, which is why giving each
    arrival its own row is safe.
    """
    harness = _Harness(test_db, monkeypatch)
    cdi.reassembler = cdi.ChannelDataReassembler()

    await harness.receive(at=1000, img_id=5)
    await harness.receive(at=1004, img_id=5)  # the same image, a few seconds later

    assert len(await harness.marker_rows()) == 1, "a re-flood produced a second bubble"
    assert len(harness.announced) == 1


async def test_two_pictures_are_two_bubbles(test_db, monkeypatch):
    """The base case, so the dedup change cannot pass by collapsing everything."""
    harness = _Harness(test_db, monkeypatch)
    cdi.reassembler = cdi.ChannelDataReassembler()

    await harness.receive(at=1000, img_id=5)
    await harness.receive(at=1265, img_id=6, bitstream=bytes(reversed(range(123))))

    rows = await harness.marker_rows()
    assert len(rows) == 2
    assert len(set(rows)) == 2, "two different pictures shared one session"
