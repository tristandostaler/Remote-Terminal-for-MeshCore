"""Tests for migration 093: bots.private."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration093:
    @pytest.mark.asyncio
    async def test_adds_private_defaulting_to_off(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("CREATE TABLE bots (id TEXT PRIMARY KEY, name TEXT)")
            await conn.execute("INSERT INTO bots (id, name) VALUES ('a', 'wardriving')")
            await set_version(conn, 92)
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 92
            assert await get_version(conn) == LATEST_SCHEMA_VERSION
            cursor = await conn.execute("SELECT private FROM bots")
            row = await cursor.fetchone()
            assert row["private"] == 0
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_the_column_already_exists(self):
        from app.migrations._093_bot_private import migrate

        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute("CREATE TABLE bots (id TEXT PRIMARY KEY, private INTEGER DEFAULT 0)")
            await migrate(conn)
            await migrate(conn)
        finally:
            await conn.close()
