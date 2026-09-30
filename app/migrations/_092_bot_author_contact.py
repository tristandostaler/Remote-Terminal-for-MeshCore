import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Adds ``bot_engine_settings.author_contact``.

    How to reach whoever runs this node's bots: the ``source`` built-in answers
    ``!author`` / ``!source`` with it. Engine-wide rather than a per-bot setting
    so it is set once, next to the command prefix, and survives a bot reset.
    Empty (the default) makes the reply ask people to DM the node.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    if "bot_engine_settings" in {row[0] for row in await tables_cursor.fetchall()}:
        col_cursor = await conn.execute("PRAGMA table_info(bot_engine_settings)")
        columns = {row[1] for row in await col_cursor.fetchall()}
        if "author_contact" not in columns:
            await conn.execute(
                "ALTER TABLE bot_engine_settings ADD COLUMN author_contact TEXT DEFAULT ''"
            )
    await conn.commit()
