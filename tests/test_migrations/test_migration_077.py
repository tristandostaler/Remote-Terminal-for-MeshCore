"""Tests for database migration(s)."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration077:
    """Test migration 077: the per-contact media text fallback switch."""

    @staticmethod
    async def _contacts_db() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await set_version(conn, 76)
        await conn.execute(
            """
            CREATE TABLE contacts (
                public_key TEXT PRIMARY KEY,
                name TEXT
            )
            """
        )
        await conn.commit()
        return conn

    @pytest.mark.asyncio
    async def test_adds_the_column_defaulting_to_on(self):
        """On by default, and on for contacts that already exist.

        Off would mean every conversation on a node without CMD_SEND_RAW_DATA
        silently keeps its old behaviour -- pictures that cannot be opened -- with
        no indication that a switch would fix it.
        """
        conn = await self._contacts_db()
        try:
            await conn.execute(
                "INSERT INTO contacts (public_key, name) VALUES (?, ?)", ("ab" * 32, "Alice")
            )
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 76
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            cursor = await conn.execute("PRAGMA table_info(contacts)")
            columns = {row["name"]: row for row in await cursor.fetchall()}
            assert "raw_media_text_fallback" in columns
            assert columns["raw_media_text_fallback"]["notnull"] == 1

            cursor = await conn.execute(
                "SELECT raw_media_text_fallback FROM contacts WHERE public_key = ?", ("ab" * 32,)
            )
            row = await cursor.fetchone()
            assert row is not None and row["raw_media_text_fallback"] == 1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_contact_added_later_also_defaults_to_on(self):
        conn = await self._contacts_db()
        try:
            await run_migrations(conn)

            await conn.execute(
                "INSERT INTO contacts (public_key, name) VALUES (?, ?)", ("cd" * 32, "Bob")
            )
            await conn.commit()

            cursor = await conn.execute(
                "SELECT raw_media_text_fallback FROM contacts WHERE public_key = ?", ("cd" * 32,)
            )
            row = await cursor.fetchone()
            assert row is not None and row["raw_media_text_fallback"] == 1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_leaves_an_existing_column_and_its_values_alone(self):
        """Re-running must not reset someone's deliberate opt-out back to on."""
        conn = await self._contacts_db()
        try:
            await conn.execute(
                "ALTER TABLE contacts ADD COLUMN raw_media_text_fallback INTEGER NOT NULL DEFAULT 1"
            )
            await conn.execute(
                "INSERT INTO contacts (public_key, name, raw_media_text_fallback) VALUES (?, ?, 0)",
                ("ef" * 32, "Carol"),
            )
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute(
                "SELECT raw_media_text_fallback FROM contacts WHERE public_key = ?", ("ef" * 32,)
            )
            row = await cursor.fetchone()
            assert row is not None and row["raw_media_text_fallback"] == 0
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_survives_a_database_with_no_contacts_table(self):
        """The runner applies every migration to a fresh database in order, so this
        one must not assume the table it alters exists yet."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 76)
            await conn.commit()

            await run_migrations(conn)

            assert await get_version(conn) == LATEST_SCHEMA_VERSION
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_no_channels_column_is_added(self):
        """The raw transport is contact-directed even for a picture announced on a
        channel, so a channel column would never be read. Adding one would invite
        a dead toggle in the UI."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 76)
            await conn.execute("CREATE TABLE contacts (public_key TEXT PRIMARY KEY, name TEXT)")
            await conn.execute("CREATE TABLE channels (key TEXT PRIMARY KEY, name TEXT)")
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute("PRAGMA table_info(channels)")
            columns = {row["name"] for row in await cursor.fetchall()}
            assert "raw_media_text_fallback" not in columns
        finally:
            await conn.close()
