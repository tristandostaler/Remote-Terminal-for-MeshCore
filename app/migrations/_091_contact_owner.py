import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Contact owner notes: the saved firmware ``owner.info`` plus operator notes.

    ``contact_owner`` has deliberately **no** foreign key onto ``contacts``.
    Everything else in it can be fetched again, but the operator's notes were
    typed by hand. Contacts come and go (bulk deletes, re-adds from adverts),
    and a cascade would quietly throw those notes away.

    Also adds ``app_settings.owner_info_refresh_days``, which paces the
    background owner-info sweep (default weekly, 0 = off).
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_owner (
            public_key TEXT PRIMARY KEY,
            firmware_owner_info TEXT,
            firmware_version TEXT,
            fetched_at INTEGER,
            attempted_at INTEGER,
            attempt_status TEXT,
            notes TEXT NOT NULL DEFAULT '',
            notes_updated_at INTEGER,
            notified_at INTEGER
        )
        """
    )

    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    if "app_settings" in {row[0] for row in await tables_cursor.fetchall()}:
        col_cursor = await conn.execute("PRAGMA table_info(app_settings)")
        columns = {row[1] for row in await col_cursor.fetchall()}
        if "owner_info_refresh_days" not in columns:
            await conn.execute(
                "ALTER TABLE app_settings ADD COLUMN owner_info_refresh_days INTEGER DEFAULT 7"
            )
    await conn.commit()
