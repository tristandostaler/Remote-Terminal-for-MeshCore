"""Tests for database migration(s)."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


async def _media_schema_db() -> aiosqlite.Connection:
    """A database at version 74 holding one bound and one orphaned session."""
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await set_version(conn, 74)
    await conn.execute(
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL)"
    )
    await conn.execute("INSERT INTO messages (id, text) VALUES (5, 'IE4:...')")
    for table in ("image_sessions", "voice_sessions"):
        await conn.execute(
            f"CREATE TABLE {table} (session_id TEXT PRIMARY KEY, message_id INTEGER)"
        )
        await conn.executescript(
            f"""
            INSERT INTO {table} (session_id, message_id) VALUES ('aaaaaaaa', 5);
            INSERT INTO {table} (session_id, message_id) VALUES ('bbbbbbbb', NULL);
            INSERT INTO {table} (session_id, message_id) VALUES ('cccccccc', 404);
            """
        )
    await conn.commit()
    return conn


class TestMigration075:
    """Test migration 075: media sessions pinned by the messages that show them."""

    @pytest.mark.asyncio
    async def test_backfills_the_binding_each_session_already_carried(self):
        """Media already in the database must be pinned, not swept on next start."""
        conn = await _media_schema_db()
        try:
            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 74
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            cursor = await conn.execute(
                "SELECT kind, session_id, message_id FROM media_session_messages "
                "ORDER BY kind, session_id"
            )
            rows = [tuple(row) for row in await cursor.fetchall()]
            assert rows == [("image", "aaaaaaaa", 5), ("voice", "aaaaaaaa", 5)]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_does_not_pin_a_session_whose_message_is_already_gone(self):
        """Session 'cccccccc' names message 404, which no longer exists.

        Inserting it would fail the foreign key and abort the whole migration
        chain, so the backfill has to check rather than trust the column.
        """
        conn = await _media_schema_db()
        try:
            await run_migrations(conn)

            cursor = await conn.execute(
                "SELECT COUNT(*) FROM media_session_messages WHERE message_id=404"
            )
            row = await cursor.fetchone()
            assert row is not None and row[0] == 0
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_the_table_already_exists(self):
        conn = await _media_schema_db()
        try:
            await conn.execute(
                """
                CREATE TABLE media_session_messages (
                    kind TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    PRIMARY KEY (kind, session_id, message_id)
                )
                """
            )
            await conn.execute("INSERT INTO media_session_messages VALUES ('image', 'aaaaaaaa', 5)")
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute("SELECT COUNT(*) FROM media_session_messages")
            row = await cursor.fetchone()
            assert row is not None and row[0] == 2
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_survives_a_database_without_the_tables(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 74)
            await conn.commit()

            await run_migrations(conn)

            assert await get_version(conn) == LATEST_SCHEMA_VERSION
        finally:
            await conn.close()
