import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Adds ``bots.timeout_seconds``.

    How long one run of the bot may take before the engine stops it. It used
    to be a fixed 10 seconds for every bot, too short for a language model on a
    Pi; every existing bot keeps 10.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    if "bots" in {row[0] for row in await tables_cursor.fetchall()}:
        col_cursor = await conn.execute("PRAGMA table_info(bots)")
        columns = {row[1] for row in await col_cursor.fetchall()}
        if "timeout_seconds" not in columns:
            await conn.execute("ALTER TABLE bots ADD COLUMN timeout_seconds REAL DEFAULT 10")
    await conn.commit()
