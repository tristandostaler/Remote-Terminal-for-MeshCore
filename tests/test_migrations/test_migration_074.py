"""Tests for database migration(s)."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from app.send_attempts import DEFAULT_MAX_MESSAGE_RETRIES
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION

MESSAGE_METADATA_COLUMNS = {
    "compression",
    "plain_bytes",
    "wire_bytes",
    "payload_bytes",
    "send_attempts",
    "send_max_attempts",
    "send_state",
}


async def _messages_schema_db() -> aiosqlite.Connection:
    """A database at version 73 with a messages table and one legacy row."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await set_version(conn, 73)
    await conn.execute(
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            conversation_key TEXT NOT NULL,
            text TEXT NOT NULL,
            sender_timestamp INTEGER,
            received_at INTEGER NOT NULL,
            outgoing INTEGER DEFAULT 0,
            acked INTEGER DEFAULT 0
        )
        """
    )
    await conn.execute(
        "INSERT INTO messages (type, conversation_key, text, received_at, outgoing) "
        "VALUES ('PRIV', 'ab', 'older than the feature', 1700000000, 1)"
    )
    await conn.execute("CREATE TABLE app_settings (id INTEGER PRIMARY KEY CHECK (id = 1))")
    await conn.execute("INSERT INTO app_settings (id) VALUES (1)")
    await conn.commit()
    return conn


class TestMigration074:
    """Test migration 074: per-message compression and send-progress metadata."""

    @pytest.mark.asyncio
    async def test_adds_message_metadata_columns(self):
        conn = await _messages_schema_db()
        try:
            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 73
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            cursor = await conn.execute("PRAGMA table_info(messages)")
            columns = {row["name"] for row in await cursor.fetchall()}
            assert columns >= MESSAGE_METADATA_COLUMNS
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_existing_rows_keep_null_metadata(self):
        """Pre-migration messages have no wire bytes to recover, so they stay unknown."""
        conn = await _messages_schema_db()
        try:
            await run_migrations(conn)

            cursor = await conn.execute(
                "SELECT compression, plain_bytes, wire_bytes, payload_bytes, "
                "send_attempts, send_max_attempts, send_state FROM messages"
            )
            row = await cursor.fetchone()
            assert row is not None
            assert all(row[column] is None for column in MESSAGE_METADATA_COLUMNS)
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_seeds_the_previously_hardcoded_retry_cap(self):
        """Upgrading must not change send behaviour until the user moves the dial."""
        conn = await _messages_schema_db()
        try:
            await run_migrations(conn)

            cursor = await conn.execute("SELECT max_message_retries FROM app_settings WHERE id = 1")
            row = await cursor.fetchone()
            assert row is not None
            assert row["max_message_retries"] == DEFAULT_MAX_MESSAGE_RETRIES
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_columns_already_exist(self):
        conn = await _messages_schema_db()
        try:
            for column in MESSAGE_METADATA_COLUMNS:
                await conn.execute(f"ALTER TABLE messages ADD COLUMN {column} TEXT")
            await conn.execute("ALTER TABLE app_settings ADD COLUMN max_message_retries INTEGER")
            await conn.execute("UPDATE messages SET compression = 'mcmp3'")
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute("SELECT compression FROM messages")
            row = await cursor.fetchone()
            assert row is not None and row["compression"] == "mcmp3"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_survives_a_database_without_the_tables(self):
        """A partially built database must not abort the migration chain."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 73)
            await conn.commit()

            await run_migrations(conn)

            assert await get_version(conn) == LATEST_SCHEMA_VERSION
        finally:
            await conn.close()
