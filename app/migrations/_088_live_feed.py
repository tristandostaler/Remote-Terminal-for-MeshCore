import logging

import aiosqlite

logger = logging.getLogger(__name__)

LIVE_FEED_SETTINGS_COLUMNS: tuple[tuple[str, str], ...] = (
    ("live_feed_enabled", "INTEGER DEFAULT 0"),
    ("live_feed_url", "TEXT DEFAULT 'https://live.meshcore.ca'"),
    ("live_feed_region", "TEXT DEFAULT ''"),
    ("live_feed_channels", "TEXT DEFAULT '[\"Public\"]'"),
    ("live_feed_poll_interval", "INTEGER DEFAULT 300"),
)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Live feed comparison: mirror channel messages seen by a public CoreScope
    instance (live.meshcore.ca) so they can be compared with what this node heard.

    ``live_feed_messages`` is a local mirror of the remote channel-message list,
    one row per remote packet hash. It is joined to ``messages`` on the same
    key the channel-echo dedup index uses -- ``(conversation_key, text,
    COALESCE(sender_timestamp, 0))`` -- so "seen by both" means byte-identical
    plaintext, not a fuzzy match.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = {row[0] for row in await tables_cursor.fetchall()}

    if "app_settings" in tables:
        col_cursor = await conn.execute("PRAGMA table_info(app_settings)")
        columns = {row[1] for row in await col_cursor.fetchall()}
        for name, decl in LIVE_FEED_SETTINGS_COLUMNS:
            if name not in columns:
                await conn.execute(f"ALTER TABLE app_settings ADD COLUMN {name} {decl}")

    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS live_feed_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            packet_hash TEXT NOT NULL UNIQUE,
            channel_name TEXT NOT NULL,
            channel_key TEXT,
            sender TEXT,
            text TEXT NOT NULL,
            sender_timestamp INTEGER,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            repeats INTEGER NOT NULL DEFAULT 1,
            observers TEXT NOT NULL DEFAULT '[]',
            hops INTEGER,
            snr REAL,
            scope_name TEXT,
            fetched_at INTEGER NOT NULL
        )
        """
    )
    await conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_live_feed_messages_match
            ON live_feed_messages(channel_key, text, COALESCE(sender_timestamp, 0))
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_live_feed_messages_first_seen ON live_feed_messages(first_seen)"
    )

    await conn.commit()
