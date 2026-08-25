"""Tests for database migration(s)."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration078:
    """Test migration 078: renaming the media text switch now that it is a switch.

    Migration 077 called the column ``raw_media_text_fallback``, when text really
    was a fallback behind a raw attempt. It now decides the transport outright, so
    the name moved with the meaning. The value has to survive the move: someone who
    turned it off did so to keep their airtime, and silently handing them the
    default back would spend it.
    """

    @staticmethod
    async def _db_at(version: int) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await set_version(conn, version)
        await conn.execute("CREATE TABLE contacts (public_key TEXT PRIMARY KEY, name TEXT)")
        await conn.commit()
        return conn

    @pytest.mark.asyncio
    async def test_carries_an_opt_out_across_the_rename(self):
        conn = await self._db_at(77)
        try:
            await conn.execute(
                "ALTER TABLE contacts ADD COLUMN raw_media_text_fallback INTEGER NOT NULL DEFAULT 1"
            )
            await conn.execute(
                "INSERT INTO contacts (public_key, name, raw_media_text_fallback) VALUES (?, ?, 0)",
                ("ab" * 32, "Alice"),
            )
            await conn.execute(
                "INSERT INTO contacts (public_key, name, raw_media_text_fallback) VALUES (?, ?, 1)",
                ("cd" * 32, "Bob"),
            )
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute("PRAGMA table_info(contacts)")
            columns = {row["name"] for row in await cursor.fetchall()}
            assert "raw_media_text_transport" in columns
            assert "raw_media_text_fallback" not in columns, "the old name outlived the rename"

            cursor = await conn.execute(
                "SELECT public_key, raw_media_text_transport FROM contacts ORDER BY public_key"
            )
            rows = await cursor.fetchall()
            assert [row["raw_media_text_transport"] for row in rows] == [0, 1]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_adds_the_column_when_the_database_never_saw_077(self):
        """A database restored from before 077, or one whose contacts table was
        created without it. There is nothing to carry over, so the default applies."""
        conn = await self._db_at(77)
        try:
            await conn.execute(
                "INSERT INTO contacts (public_key, name) VALUES (?, ?)", ("ef" * 32, "Carol")
            )
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute(
                "SELECT raw_media_text_transport FROM contacts WHERE public_key = ?", ("ef" * 32,)
            )
            row = await cursor.fetchone()
            assert row is not None and row["raw_media_text_transport"] == 1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_a_no_op_when_the_column_is_already_renamed(self):
        """The runner is version-gated, but a database can arrive with the new column
        already present -- a fresh schema, or a restore. Re-running must not fail and
        must not reset a deliberate opt-out."""
        conn = await self._db_at(77)
        try:
            await conn.execute(
                "ALTER TABLE contacts ADD COLUMN raw_media_text_transport "
                "INTEGER NOT NULL DEFAULT 1"
            )
            await conn.execute(
                "INSERT INTO contacts (public_key, name, raw_media_text_transport) "
                "VALUES (?, ?, 0)",
                ("ab" * 32, "Alice"),
            )
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute(
                "SELECT raw_media_text_transport FROM contacts WHERE public_key = ?", ("ab" * 32,)
            )
            row = await cursor.fetchone()
            assert row is not None and row["raw_media_text_transport"] == 0
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_survives_a_database_with_no_contacts_table(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 77)
            await conn.commit()

            await run_migrations(conn)

            assert await get_version(conn) == LATEST_SCHEMA_VERSION
        finally:
            await conn.close()
