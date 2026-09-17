import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Mark messages that a historical decrypt sweep recovered.

    Adds ``recovered_at`` (INTEGER, wall-clock seconds) to ``messages``: the
    moment a sweep stored the row, as opposed to ``received_at``, which stays
    the moment the packet was heard. NULL for everything ingested live.

    Unread state is keyed on ``last_read_at``, so a message recovered from a
    three-week-old packet would otherwise land silently below the read
    boundary and never be surfaced. ``recovered_at`` is what lets the unread
    queries treat it as new while the conversation still shows it in its
    chronological place.

    No backfill is possible: rows stored by earlier sweeps are
    indistinguishable from live ones, so they stay NULL and read as live.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in await tables_cursor.fetchall()}
    if "messages" not in existing_tables:
        await conn.commit()
        return

    col_cursor = await conn.execute("PRAGMA table_info(messages)")
    message_columns = {row[1] for row in await col_cursor.fetchall()}
    if "recovered_at" not in message_columns:
        await conn.execute("ALTER TABLE messages ADD COLUMN recovered_at INTEGER")

    await conn.commit()
