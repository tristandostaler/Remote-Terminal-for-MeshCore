"""Storage for AEIC image sessions.

An AEIC image arrives as 1-2 ``aei1:`` text messages. This repository holds the
partial set until every chunk is in, then keeps the reassembled bitstream and
(once decoded) the PNG.

The bitstream is kept deliberately: decode needs a 958 MiB model bundle that may
not be installed yet, and a stored bitstream means the picture can be rendered
later -- after the download finishes -- without asking the sender to retransmit
150 bytes over LoRa.
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from app.database import db

SESSION_TTL_SECONDS = 86_400
MAX_CACHED_SESSIONS = 500

OUTGOING_PREFIX = "self"
"""Key prefix for images WE sent. Not a hex prefix, so it cannot collide with
the ``peer_key[:12]`` prefix an inbound session gets."""


def session_key(peer_key: str | None, session_id: int) -> str:
    """Compose the primary key for an INBOUND session.

    The wire session id is only 2 base36 characters, so it is unique per
    *sender*, not globally. Keying on the sender prefix as well is what stops two
    peers who happen to pick the same id inside one TTL from merging their images
    into one corrupt picture -- the failure mode no lower layer can see.

    Outbound sessions must NOT use this: see :func:`outgoing_session_key`.
    """
    prefix = (peer_key or "unknown")[:12].lower()
    return f"{prefix}:{session_id:04d}"


def outgoing_session_key(message_id: int | None = None) -> str:
    """The primary key for an image we sent ourselves.

    Deliberately NOT derived from the wire session id. That id is two base36
    characters -- 1296 values -- because all it has to be is unique per sender
    inside one receiver's reassembly window. Reusing it as a local storage key
    put every outgoing image into a 1296-slot namespace with a 24 h TTL, which
    collides at roughly 14% for twenty photos a day: two sends of the same shape
    hit :meth:`AeicImageRepository.create_session`'s existing-row branch, pass
    its metadata check, and the second silently overwrote the first's bitstream
    while ``COALESCE(message_id, ?)`` kept the first message on the row. The
    older bubble then rendered the newer picture and the newer message had no
    session at all.

    Keyed on the sent message's id when there is one, so the key is stable and
    reproducible; otherwise a random id, because a send that produced no message
    row (a bot whose send was dropped by moderation, for instance) still has to
    be storable without stepping on anything.
    """
    if message_id is not None:
        return f"{OUTGOING_PREFIX}:m{message_id}"
    return f"{OUTGOING_PREFIX}:{uuid4().hex[:16]}"


class AeicImageRepository:
    @staticmethod
    async def create_session(
        *,
        key: str,
        message_id: int | None,
        direction: str,
        conversation_type: str,
        conversation_key: str,
        peer_public_key: str | None,
        square_size: int,
        aspect_code: int,
        rate_code: int,
        total_chunks: int,
        state: str,
        ttl_seconds: int = SESSION_TTL_SECONDS,
    ) -> None:
        now = int(time.time())
        async with db.tx() as conn:
            async with conn.execute(
                "SELECT message_id, square_size, aspect_code, rate_code, total_chunks "
                "FROM aeic_image_sessions WHERE session_key=?",
                (key,),
            ) as cursor:
                existing = await cursor.fetchone()
            if existing is not None:
                metadata = (square_size, aspect_code, rate_code, total_chunks)
                if tuple(existing[1:]) != metadata:
                    raise ValueError("AEIC session key conflicts with existing metadata")
                await conn.execute(
                    "UPDATE aeic_image_sessions "
                    "SET message_id=COALESCE(message_id, ?), expires_at=? "
                    "WHERE session_key=?",
                    (message_id, now + ttl_seconds, key),
                )
                return
            await conn.execute(
                """
                INSERT INTO aeic_image_sessions (
                    session_key, message_id, direction, conversation_type,
                    conversation_key, peer_public_key, square_size, aspect_code,
                    rate_code, total_chunks, state, created_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    message_id,
                    direction,
                    conversation_type,
                    conversation_key,
                    peer_public_key,
                    square_size,
                    aspect_code,
                    rate_code,
                    total_chunks,
                    state,
                    now,
                    now + ttl_seconds,
                ),
            )

    @staticmethod
    async def add_chunk(key: str, index: int, payload: str) -> bool:
        """Store one chunk. Returns True when this call inserted a new one."""
        async with db.tx() as conn:
            async with conn.execute(
                "INSERT OR IGNORE INTO aeic_image_chunks (session_key, chunk_index, payload) "
                "VALUES (?, ?, ?)",
                (key, index, payload),
            ) as cursor:
                return cursor.rowcount > 0

    @staticmethod
    async def get(key: str) -> dict[str, Any] | None:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM aeic_image_sessions WHERE session_key=?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                return None
            async with conn.execute(
                "SELECT chunk_index, payload FROM aeic_image_chunks "
                "WHERE session_key=? ORDER BY chunk_index",
                (key,),
            ) as cursor:
                chunks = {r["chunk_index"]: r["payload"] for r in await cursor.fetchall()}
        session = dict(row)
        session["chunks"] = chunks
        return session

    @staticmethod
    async def get_by_message(message_id: int) -> dict[str, Any] | None:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT session_key FROM aeic_image_sessions WHERE message_id=? LIMIT 1",
                (message_id,),
            ) as cursor:
                row = await cursor.fetchone()
        return await AeicImageRepository.get(row["session_key"]) if row else None

    @staticmethod
    async def set_message_id(key: str, message_id: int) -> None:
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE aeic_image_sessions SET message_id=COALESCE(message_id, ?) "
                "WHERE session_key=?",
                (message_id, key),
            )

    @staticmethod
    async def store_bitstream(key: str, bitstream: bytes) -> None:
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE aeic_image_sessions SET bitstream=?, state='complete' WHERE session_key=?",
                (bitstream, key),
            )

    @staticmethod
    async def store_png(key: str, png: bytes) -> None:
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE aeic_image_sessions SET png=?, state='decoded', decode_error=NULL "
                "WHERE session_key=?",
                (png, key),
            )

    @staticmethod
    async def store_decode_error(key: str, detail: str) -> None:
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE aeic_image_sessions SET decode_error=? WHERE session_key=?",
                (detail[:500], key),
            )

    @staticmethod
    async def enforce_cache_limit() -> None:
        """Drop expired sessions, then the oldest beyond the cache ceiling.

        Called before anything that adds a session. A decoded 512x512 PNG is
        ~600 KB, so an unbounded cache would grow the database by half a gigabyte
        per thousand images received.
        """
        now = int(time.time())
        async with db.tx() as conn:
            await conn.execute("DELETE FROM aeic_image_sessions WHERE expires_at <= ?", (now,))
            await conn.execute(
                """
                DELETE FROM aeic_image_sessions WHERE session_key IN (
                    SELECT session_key FROM aeic_image_sessions
                    ORDER BY created_at DESC LIMIT -1 OFFSET ?
                )
                """,
                (MAX_CACHED_SESSIONS,),
            )
