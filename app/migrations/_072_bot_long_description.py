import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Add the ``long_description`` column to bots.

    The one-line ``description`` is what the bots list shows; this is the few
    extra lines the editor's Settings tab shows underneath it. Existing rows
    start empty — the library backfills built-ins on the next seeding pass
    (``app/bots/library.ensure_seeded``), custom bots stay blank until their
    BOT_META declares one.
    """
    table_check = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bots'"
    )
    if not await table_check.fetchone():
        return

    cursor = await conn.execute("PRAGMA table_info(bots)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "long_description" not in columns:
        await conn.execute("ALTER TABLE bots ADD COLUMN long_description TEXT NOT NULL DEFAULT ''")

    await conn.commit()
