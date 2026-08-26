"""Storage for inbound media this build has no decoder for.

See migration 079 for why the payloads are kept verbatim and why nothing here
expires on a timer.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.database import db

BLOB_GROUPING_WINDOW_SECONDS = 120
"""How long an arrival stays open for more blobs.

An unknown format gives us no image id and no chunk count, so the only signal
that two blobs belong to one picture is that they arrived close together on the
same channel with the same data type. Generous, because a multi-packet image on a
slow channel is minutes of airtime, and the cost of grouping too eagerly is one
box in the conversation covering two pictures rather than a wrong decode.
"""

MAX_BLOBS_PER_ARRIVAL = 64
"""Ceiling on blobs attributed to one arrival.

Not a size limit on real images -- the largest interoperable framing addresses 16
chunks -- but a bound on what a single grouped arrival can accumulate if the
window keeps being extended by a peer that never stops sending.
"""


@dataclass(frozen=True)
class UnsupportedMediaArrival:
    """One stored arrival of media we cannot decode."""

    id: int
    message_id: int | None
    conversation_key: str
    data_type: int
    codec_label: str
    received_at: int
    blob_count: int
    total_bytes: int


class UnsupportedMediaRepository:
    @staticmethod
    async def append_blob(
        *,
        conversation_key: str,
        data_type: int,
        codec_label: str,
        payload: bytes,
        now: int,
    ) -> tuple[int, bool]:
        """Store one blob. Returns ``(arrival id, started_a_new_arrival)``.

        ``True`` means this is the first blob of something new, which is what the
        caller turns into a message row -- so an image of eight blobs produces one
        box in the conversation, not eight.

        Grouping is decided in the database rather than in memory on purpose: a
        restart mid-image would otherwise split one picture into two boxes, and
        the arrival is already a row here.
        """
        async with db.tx() as conn:
            async with conn.execute(
                """
                SELECT m.id, COUNT(b.idx) AS blobs
                FROM unsupported_media AS m
                LEFT JOIN unsupported_media_blobs AS b ON b.media_id = m.id
                WHERE m.conversation_key = ?
                  AND m.data_type = ?
                  AND m.last_blob_at >= ?
                GROUP BY m.id
                ORDER BY m.last_blob_at DESC
                LIMIT 1
                """,
                (conversation_key, data_type, now - BLOB_GROUPING_WINDOW_SECONDS),
            ) as cursor:
                open_arrival = await cursor.fetchone()

            # Branching on the row itself rather than a flag, so "there is an open
            # arrival" and "use it" are the same test.
            if open_arrival is None or open_arrival["blobs"] >= MAX_BLOBS_PER_ARRIVAL:
                started_new = True
                async with conn.execute(
                    """
                    INSERT INTO unsupported_media (
                        message_id, conversation_key, data_type, codec_label,
                        received_at, last_blob_at
                    ) VALUES (NULL, ?, ?, ?, ?, ?)
                    """,
                    (conversation_key, data_type, codec_label, now, now),
                ) as cursor:
                    media_id = cursor.lastrowid
                next_index = 0
            else:
                started_new = False
                media_id = open_arrival["id"]
                next_index = open_arrival["blobs"]
                await conn.execute(
                    "UPDATE unsupported_media SET last_blob_at = ? WHERE id = ?",
                    (now, media_id),
                )

            await conn.execute(
                "INSERT OR IGNORE INTO unsupported_media_blobs (media_id, idx, payload) "
                "VALUES (?, ?, ?)",
                (media_id, next_index, payload),
            )
        assert media_id is not None
        return int(media_id), started_new

    @staticmethod
    async def bind_message(media_id: int, message_id: int) -> None:
        """Pin an arrival to the message that represents it in the conversation.

        This is the whole retention rule: the FK cascades, so deleting the message
        reclaims the payloads and nothing else ever does.
        """
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE unsupported_media SET message_id = ? WHERE id = ?",
                (message_id, media_id),
            )

    @staticmethod
    async def get(media_id: int) -> UnsupportedMediaArrival | None:
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT m.id, m.message_id, m.conversation_key, m.data_type, m.codec_label,
                       m.received_at,
                       COUNT(b.idx) AS blob_count,
                       COALESCE(SUM(LENGTH(b.payload)), 0) AS total_bytes
                FROM unsupported_media AS m
                LEFT JOIN unsupported_media_blobs AS b ON b.media_id = m.id
                WHERE m.id = ?
                GROUP BY m.id
                """,
                (media_id,),
            ) as cursor:
                row = await cursor.fetchone()
        if row is None:
            return None
        return UnsupportedMediaArrival(
            id=row["id"],
            message_id=row["message_id"],
            conversation_key=row["conversation_key"],
            data_type=row["data_type"],
            codec_label=row["codec_label"],
            received_at=row["received_at"],
            blob_count=row["blob_count"],
            total_bytes=row["total_bytes"],
        )

    @staticmethod
    async def blobs(media_id: int) -> list[bytes]:
        """Every stored payload, in arrival order. What a future decoder reads."""
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT payload FROM unsupported_media_blobs WHERE media_id = ? ORDER BY idx",
                (media_id,),
            ) as cursor:
                return [bytes(row["payload"]) for row in await cursor.fetchall()]
