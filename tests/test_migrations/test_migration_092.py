"""Tests for migration 092: bot_engine_settings.author_contact."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration092:
    @pytest.mark.asyncio
    async def test_adds_author_contact_defaulting_to_empty(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute(
                "CREATE TABLE bot_engine_settings (id INTEGER PRIMARY KEY CHECK (id = 1), "
                "command_prefix TEXT DEFAULT '!')"
            )
            await conn.execute("INSERT INTO bot_engine_settings (id) VALUES (1)")
            await set_version(conn, 91)
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 91
            assert await get_version(conn) == LATEST_SCHEMA_VERSION
            cursor = await conn.execute("SELECT author_contact FROM bot_engine_settings")
            row = await cursor.fetchone()
            assert row["author_contact"] == ""
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_the_column_already_exists(self):
        from app.migrations._092_bot_author_contact import migrate

        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute(
                "CREATE TABLE bot_engine_settings (id INTEGER PRIMARY KEY, "
                "author_contact TEXT DEFAULT '')"
            )
            await migrate(conn)
            await migrate(conn)
        finally:
            await conn.close()
