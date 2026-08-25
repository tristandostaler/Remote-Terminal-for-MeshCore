import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Rename ``contacts.raw_media_text_fallback`` to ``raw_media_text_transport``.

    Migration 077 added the column as a *fallback*: raw was always tried first
    and text only rescued a node whose firmware had no ``CMD_SEND_RAW_DATA``. It
    is now a switch -- on, our fetch requests go out as ``rmt1:`` text without
    waiting for a raw send to fail. Same column, same default, different meaning,
    so the name has to move with it: a flag called ``fallback`` that decides the
    first choice of transport is a name that lies.

    The stored value carries over unchanged. Both spellings mean "text is
    allowed", so a contact someone had already switched off stays off.

    ``RENAME COLUMN`` needs SQLite 3.25 (2018) and every realistic Python 3.11
    build is far past it, but migration 026 rebuilt a table rather than trust it,
    so the add-and-copy path stays here as a fallback: failing this migration
    would take startup down with it.
    """
    table_check = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
    )
    if not await table_check.fetchone():
        return

    cursor = await conn.execute("PRAGMA table_info(contacts)")
    columns = {row[1] for row in await cursor.fetchall()}

    if "raw_media_text_transport" in columns:
        await conn.commit()
        return

    if "raw_media_text_fallback" not in columns:
        # A database that never saw 077 -- nothing to carry over.
        await conn.execute(
            "ALTER TABLE contacts ADD COLUMN raw_media_text_transport INTEGER NOT NULL DEFAULT 1"
        )
        await conn.commit()
        return

    try:
        await conn.execute(
            "ALTER TABLE contacts RENAME COLUMN raw_media_text_fallback TO raw_media_text_transport"
        )
    except aiosqlite.OperationalError:
        logger.info("RENAME COLUMN unavailable; copying the text transport switch instead")
        await conn.execute(
            "ALTER TABLE contacts ADD COLUMN raw_media_text_transport INTEGER NOT NULL DEFAULT 1"
        )
        await conn.execute("UPDATE contacts SET raw_media_text_transport = raw_media_text_fallback")

    await conn.commit()
