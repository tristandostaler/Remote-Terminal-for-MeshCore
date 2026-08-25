import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Add the per-contact text fallback switch for raw media transfers.

    Image (``IE4:``) and voice (``VE3:``) fragments normally move as raw MeshCore
    packets. On firmware without ``CMD_SEND_RAW_DATA`` that fails outright, and
    the fallback carries the same bytes as ``rmt1:`` text messages instead --
    see :mod:`app.services.raw_media_text`.

    Default on: without it those conversations simply cannot exchange a picture
    or a recording. The switch exists because the fallback costs roughly 2.5x
    the airtime, which someone on a busy or metered band may not want to spend.

    Contacts only, no channels column. The raw transport is contact-directed even
    for a picture announced on a channel -- both fetch handlers resolve a contact
    and answer it -- so the contact's setting is the one that governs, and a
    channel column would never be read.
    """
    table_check = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='contacts'"
    )
    if not await table_check.fetchone():
        return

    cursor = await conn.execute("PRAGMA table_info(contacts)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "raw_media_text_fallback" not in columns:
        await conn.execute(
            "ALTER TABLE contacts ADD COLUMN raw_media_text_fallback INTEGER NOT NULL DEFAULT 1"
        )

    await conn.commit()
