import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Add the per-conversation image codec selector and AEIC session storage.

    ``image_codec`` picks which codec an outbound photo uses for the
    conversation: ``ie4`` (the SAR-compatible AVIF/JPEG fragment transport, the
    default and what every existing conversation keeps) or ``aeic`` (the neural
    codec carried as ``aei1:`` basE91 text).

    AEIC gets its own session tables rather than reusing ``image_sessions``.
    That table's CHECK constraints encode IE4's shape -- format in (0, 1),
    dimensions 1..256, fragments of 1..152 *bytes* -- and AEIC is 512x512 with
    *text* chunks. Widening the constraints to admit both would leave a table
    where no row's shape can be trusted from the schema alone.
    """
    for table in ("contacts", "channels"):
        table_check = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        )
        if not await table_check.fetchone():
            continue

        cursor = await conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cursor.fetchall()}
        if "image_codec" not in columns:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN image_codec TEXT DEFAULT 'ie4'")

    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS aeic_image_sessions (
            -- "<sender key prefix>:<base36 session id>". The session id is only
            -- 2 base36 chars on the wire, so it is unique per SENDER, not
            -- globally; the composite key is what keeps two peers from merging
            -- their images.
            session_key TEXT PRIMARY KEY,
            message_id INTEGER,
            direction TEXT NOT NULL CHECK(direction IN ('incoming', 'outgoing')),
            conversation_type TEXT NOT NULL CHECK(conversation_type IN ('PRIV', 'CHAN')),
            conversation_key TEXT NOT NULL,
            peer_public_key TEXT,
            square_size INTEGER NOT NULL CHECK(square_size IN (256, 512, 768, 1024)),
            aspect_code INTEGER NOT NULL CHECK(aspect_code BETWEEN 0 AND 15),
            rate_code INTEGER NOT NULL CHECK(rate_code BETWEEN 0 AND 3),
            total_chunks INTEGER NOT NULL CHECK(total_chunks BETWEEN 1 AND 36),
            state TEXT NOT NULL,
            -- The reassembled rANS payload, kept so a decode can be retried
            -- (or deferred until the model bundle is installed) without asking
            -- the sender to retransmit.
            bitstream BLOB,
            -- The decoded picture as PNG. Nullable: reassembly and decode are
            -- separate steps, and decode needs a 958 MiB bundle that may not be
            -- installed yet.
            png BLOB,
            decode_error TEXT,
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE SET NULL
        );
        CREATE TABLE IF NOT EXISTS aeic_image_chunks (
            session_key TEXT NOT NULL,
            chunk_index INTEGER NOT NULL CHECK(chunk_index BETWEEN 0 AND 35),
            -- A slice of the basE91 stream, not independently decodable.
            payload TEXT NOT NULL CHECK(length(payload) BETWEEN 1 AND 156),
            PRIMARY KEY (session_key, chunk_index),
            FOREIGN KEY (session_key) REFERENCES aeic_image_sessions(session_key)
                ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_aeic_sessions_expiry
            ON aeic_image_sessions(expires_at);
        CREATE INDEX IF NOT EXISTS idx_aeic_sessions_message
            ON aeic_image_sessions(message_id);
        """
    )
    await conn.commit()
