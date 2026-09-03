import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Add clock_autofix_repeaters JSON list column to app_settings.

    Repeaters in this list (a subset of ``clock_sync_repeaters``) may be sent
    ``clkreboot`` -- which resets the repeater clock and reboots it -- followed by
    a fresh ``time`` sync when the periodic clock sync is refused because the
    repeater's clock is ahead of this server's. The firmware never moves a clock
    backwards, so a reboot is the only remote fix for a clock in the future.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    if "app_settings" not in {row[0] for row in await tables_cursor.fetchall()}:
        await conn.commit()
        return
    col_cursor = await conn.execute("PRAGMA table_info(app_settings)")
    columns = {row[1] for row in await col_cursor.fetchall()}
    if "clock_autofix_repeaters" not in columns:
        await conn.execute(
            "ALTER TABLE app_settings ADD COLUMN clock_autofix_repeaters TEXT DEFAULT '[]'"
        )
        await conn.commit()
