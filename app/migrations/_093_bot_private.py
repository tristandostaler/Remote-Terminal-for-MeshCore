import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Adds ``bots.private``.

    A private bot still answers its commands but is never advertised: ``help``
    and ``bots`` leave it out of their lists, and the tinyllm bot leaves it out
    of its notes about this node's bots. Off for every existing bot.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    if "bots" in {row[0] for row in await tables_cursor.fetchall()}:
        col_cursor = await conn.execute("PRAGMA table_info(bots)")
        columns = {row[1] for row in await col_cursor.fetchall()}
        if "private" not in columns:
            await conn.execute("ALTER TABLE bots ADD COLUMN private INTEGER DEFAULT 0")
    await conn.commit()
