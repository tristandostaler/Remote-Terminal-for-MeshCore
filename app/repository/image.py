from __future__ import annotations

import time
from typing import Any

from app.database import db
from app.repository.media_sessions import (
    record_session_message,
    sweep_unreferenced_sessions,
)


class ImageRepository:
    @staticmethod
    async def create_session(
        *,
        session_id: str,
        message_id: int | None,
        direction: str,
        conversation_type: str,
        conversation_key: str,
        peer_public_key: str | None,
        format_id: int,
        width: int,
        height: int,
        size_bytes: int,
        fragment_count: int,
        state: str,
        ttl_seconds: int,
    ) -> None:
        now = int(time.time())
        async with db.tx() as conn:
            async with conn.execute(
                "SELECT message_id, format, width, height, size_bytes, fragment_count "
                "FROM image_sessions WHERE session_id=?",
                (session_id,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                # More than one message can legitimately carry the same envelope:
                # a re-sent or pasted IE4 line, or an image sent to yourself,
                # which arrives back as a second message row. They describe one
                # picture and share its fragments, so the row keeps its first
                # message id rather than rejecting the others. Only the envelope
                # metadata is guarded -- disagreeing there means two different
                # pictures picked the same id, and merging them would corrupt both.
                metadata = (format_id, width, height, size_bytes, fragment_count)
                if tuple(existing[1:]) != metadata:
                    raise ValueError("image session ID describes a different picture")
                await conn.execute(
                    "UPDATE image_sessions SET message_id=COALESCE(message_id, ?), "
                    "peer_public_key=COALESCE(peer_public_key, ?), expires_at=? WHERE session_id=?",
                    (message_id, peer_public_key, now + ttl_seconds, session_id),
                )
                await record_session_message(
                    conn, kind="image", session_id=session_id, message_id=message_id
                )
                return
            await conn.execute(
                """
                INSERT INTO image_sessions (
                    session_id, message_id, direction, conversation_type, conversation_key,
                    peer_public_key, format, width, height, size_bytes, fragment_count,
                    state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    message_id,
                    direction,
                    conversation_type,
                    conversation_key,
                    peer_public_key,
                    format_id,
                    width,
                    height,
                    size_bytes,
                    fragment_count,
                    state,
                    now,
                    now + ttl_seconds,
                ),
            )
            await record_session_message(
                conn, kind="image", session_id=session_id, message_id=message_id
            )

    @staticmethod
    async def add_fragment(session_id: str, index: int, data: bytes) -> bool:
        async with db.tx() as conn:
            async with conn.execute(
                "SELECT fragment_count, size_bytes FROM image_sessions WHERE session_id=?",
                (session_id,),
            ) as cursor:
                session = await cursor.fetchone()
            if session is None or not 0 <= index < session[0]:
                raise ValueError("fragment does not belong to a known image session")
            expected = 152 if index < session[0] - 1 else session[1] - 152 * (session[0] - 1)
            if len(data) != expected:
                raise ValueError("image fragment size conflicts with envelope metadata")
            async with conn.execute(
                "SELECT image_data FROM image_fragments WHERE session_id=? AND fragment_index=?",
                (session_id, index),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                if bytes(existing[0]) != data:
                    raise ValueError("conflicting duplicate image fragment")
                return False
            await conn.execute(
                "INSERT INTO image_fragments (session_id, fragment_index, image_data) VALUES (?, ?, ?)",
                (session_id, index, data),
            )
            await conn.execute(
                """
                UPDATE image_sessions SET state=CASE WHEN fragment_count <=
                    (SELECT COUNT(*) FROM image_fragments WHERE session_id=?)
                    THEN 'complete' ELSE 'receiving' END WHERE session_id=?
                """,
                (session_id, session_id),
            )
            return True

    @staticmethod
    async def get(session_id: str) -> dict[str, Any] | None:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM image_sessions WHERE session_id=?", (session_id,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            result = dict(row)
            async with conn.execute(
                "SELECT fragment_index, image_data FROM image_fragments "
                "WHERE session_id=? ORDER BY fragment_index",
                (session_id,),
            ) as cursor:
                result["fragments"] = [(row[0], bytes(row[1])) for row in await cursor.fetchall()]
            return result

    @staticmethod
    async def enforce_cache_limit(max_sessions: int = 128) -> int:
        """Sweep age and surplus, but never a picture a message still shows."""
        async with db.tx() as conn:
            return await sweep_unreferenced_sessions(
                conn, kind="image", max_sessions=max_sessions, now=int(time.time())
            )
