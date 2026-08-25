"""Retention shared by the image and voice session caches.

Both caches used to be swept purely on age and count, which meant the media
behind a message could disappear while the message itself stayed in the
conversation forever. Retention is now shaped by the conversation: a session a
message still references is kept for as long as that message is, and only
sessions nothing references fall back to the old age-and-count sweep.
"""

from __future__ import annotations

import aiosqlite

MEDIA_TABLES = {"image": "image_sessions", "voice": "voice_sessions"}


def _table(kind: str) -> str:
    """Resolve a media kind to its session table, so no caller can inject one."""
    try:
        return MEDIA_TABLES[kind]
    except KeyError:
        raise ValueError(f"unknown media kind {kind!r}") from None


async def record_session_message(
    conn: aiosqlite.Connection, *, kind: str, session_id: str, message_id: int | None
) -> None:
    """Note that ``message_id`` shows this session, pinning it against the sweep.

    Several messages can reference one session -- a re-sent or pasted envelope,
    or media sent to yourself -- so every one is recorded rather than the row
    keeping only whichever arrived first. A send that produced no message row
    yet passes ``None`` and is pinned by the follow-up call that has the id.
    """
    _table(kind)
    if message_id is None:
        return
    await conn.execute(
        "INSERT OR IGNORE INTO media_session_messages (kind, session_id, message_id) "
        "VALUES (?, ?, ?)",
        (kind, session_id, message_id),
    )


async def sweep_unreferenced_sessions(
    conn: aiosqlite.Connection, *, kind: str, max_sessions: int, now: int
) -> int:
    """Drop expired and surplus sessions that no surviving message references.

    The newest-N cap now bounds only the unreferenced cache. Sessions a message
    still shows are deliberately outside it: capping those would put a hard limit
    on how far back the conversation can be read, which is the whole point of
    keeping them.

    Returns the number of surplus sessions dropped, as the callers reported
    before retention became message-shaped.
    """
    table = _table(kind)
    unreferenced = (
        "NOT EXISTS (SELECT 1 FROM media_session_messages r "
        f"WHERE r.kind = ? AND r.session_id = {table}.session_id)"
    )
    # A reference to a session that is already gone is dead weight: the cascade
    # only fires from the messages side, so nothing else would ever remove it.
    await conn.execute(
        f"DELETE FROM media_session_messages WHERE kind = ? "
        f"AND session_id NOT IN (SELECT session_id FROM {table})",
        (kind,),
    )
    await conn.execute(
        f"DELETE FROM {table} WHERE expires_at < ? AND {unreferenced}",
        (now, kind),
    )
    async with conn.execute(
        f"""
        DELETE FROM {table} WHERE session_id IN (
            SELECT session_id FROM {table} WHERE {unreferenced}
            ORDER BY created_at DESC, session_id DESC LIMIT -1 OFFSET ?
        )
        """,
        (kind, max_sessions),
    ) as cursor:
        return cursor.rowcount
