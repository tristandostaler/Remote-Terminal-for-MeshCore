import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Add the virtual-node admin-commands switch to app_settings (default off).

    Apps connected to the virtual companion node may only change radio
    settings (name, frequency, TX power, ...) when this is on.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    if "app_settings" not in {row[0] for row in await tables_cursor.fetchall()}:
        await conn.commit()
        return
    col_cursor = await conn.execute("PRAGMA table_info(app_settings)")
    columns = {row[1] for row in await col_cursor.fetchall()}
    if "virtual_node_allow_admin_commands" not in columns:
        await conn.execute(
            "ALTER TABLE app_settings "
            "ADD COLUMN virtual_node_allow_admin_commands INTEGER DEFAULT 0"
        )
        await conn.commit()
