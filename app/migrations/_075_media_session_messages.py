import aiosqlite

MEDIA_KINDS = (("image", "image_sessions"), ("voice", "voice_sessions"))


async def migrate(conn: aiosqlite.Connection) -> None:
    """Record every message that references an image or voice session.

    Media used to be swept on a 24 h TTL and a newest-128 cap, so an image or
    voice message older than that could still be read in the conversation while
    the picture or audio behind it was gone -- a 404 on ``/content`` for your own
    messages, and a fetch request the sender silently ignored for everyone
    else's. Sessions referenced by a message that still exists are now kept for
    as long as that message is.

    A separate table rather than a column because the relationship is genuinely
    many-to-one: re-sending or pasting an envelope, and sending media to
    yourself, all produce several messages describing one session. The single
    ``message_id`` on the session rows cannot express that, so pinning on it
    would have dropped media the moment its *first* message was deleted, while
    later copies still showed it in the conversation.

    ``ON DELETE CASCADE`` is what makes the whole thing work: deleting a message
    removes its reference, and a session nobody references becomes sweepable
    again with no extra bookkeeping anywhere.
    """
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS media_session_messages (
            kind TEXT NOT NULL CHECK(kind IN ('image', 'voice')),
            session_id TEXT NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (kind, session_id, message_id),
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_media_session_messages_session
            ON media_session_messages(kind, session_id);
        """
    )

    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in await tables_cursor.fetchall()}

    # Backfill the binding each session already carries, so media in the
    # database before this migration is pinned rather than swept on next start.
    if "messages" in existing_tables:
        for kind, table in MEDIA_KINDS:
            if table not in existing_tables:
                continue
            await conn.execute(
                f"""
                INSERT OR IGNORE INTO media_session_messages (kind, session_id, message_id)
                SELECT ?, s.session_id, s.message_id FROM {table} s
                WHERE s.message_id IS NOT NULL
                  AND EXISTS (SELECT 1 FROM messages m WHERE m.id = s.message_id)
                """,
                (kind,),
            )
    await conn.commit()
