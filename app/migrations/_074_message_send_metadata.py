import logging

import aiosqlite

logger = logging.getLogger(__name__)

# Keep in step with app.services.message_send.DEFAULT_MAX_MESSAGE_RETRIES.
DEFAULT_MAX_MESSAGE_RETRIES = 3


async def migrate(conn: aiosqlite.Connection) -> None:
    """Persist per-message compression and send-progress metadata.

    Adds to ``messages``:

    - ``compression`` (``mcmp2``/``mcmp3``, NULL when the body rode as plain
      text) plus ``plain_bytes`` / ``wire_bytes`` / ``payload_bytes`` so the
      conversation view can show the codec and its compression ratio without
      re-running the coder on every render. ``wire_bytes`` is what actually went
      on air; ``payload_bytes`` is the compressed-text segment the ratio is
      measured against (identical for v2, smaller than ``wire_bytes`` for v3,
      whose container carries a timestamp in front of the text).
    - ``send_attempts`` / ``send_max_attempts`` / ``send_state`` so an outgoing
      message can report "attempt 2 of 3", and so a cancelled or exhausted send
      is distinguishable from one that is still being retried. Delivery itself
      stays derived from ``acked`` -- one source of truth.

    Existing rows keep NULL metadata: their wire bytes are long gone, and
    attempts made before this migration were never counted. The UI treats NULL
    as "unknown" and simply omits those parts of the meta line.

    Also adds ``app_settings.max_message_retries`` (the configurable 1-10 cap on
    direct-message send attempts), seeded to the previous hardcoded value so
    upgrading changes nothing until the user moves the dial.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in await tables_cursor.fetchall()}

    if "messages" in existing_tables:
        col_cursor = await conn.execute("PRAGMA table_info(messages)")
        message_columns = {row[1] for row in await col_cursor.fetchall()}
        additions = (
            ("compression", "TEXT"),
            ("plain_bytes", "INTEGER"),
            ("wire_bytes", "INTEGER"),
            ("payload_bytes", "INTEGER"),
            ("send_attempts", "INTEGER"),
            ("send_max_attempts", "INTEGER"),
            ("send_state", "TEXT"),
        )
        for column, column_type in additions:
            if column not in message_columns:
                await conn.execute(f"ALTER TABLE messages ADD COLUMN {column} {column_type}")
        await conn.commit()

    if "app_settings" not in existing_tables:
        await conn.commit()
        return

    col_cursor = await conn.execute("PRAGMA table_info(app_settings)")
    settings_columns = {row[1] for row in await col_cursor.fetchall()}
    if "max_message_retries" not in settings_columns:
        await conn.execute(
            "ALTER TABLE app_settings ADD COLUMN max_message_retries INTEGER DEFAULT "
            f"{DEFAULT_MAX_MESSAGE_RETRIES}"
        )
        await conn.execute(
            "UPDATE app_settings SET max_message_retries = ? WHERE id = 1",
            (DEFAULT_MAX_MESSAGE_RETRIES,),
        )
    await conn.commit()
