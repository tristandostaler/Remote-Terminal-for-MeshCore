"""Tests for migration 094: bots.timeout_seconds."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration094:
    @pytest.mark.asyncio
    async def test_adds_timeout_seconds_defaulting_to_ten(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await conn.execute("CREATE TABLE bots (id TEXT PRIMARY KEY, name TEXT)")
            await conn.execute("INSERT INTO bots (id, name) VALUES ('a', 'ping')")
            await set_version(conn, 93)
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 93
            assert await get_version(conn) == LATEST_SCHEMA_VERSION
            cursor = await conn.execute("SELECT timeout_seconds FROM bots")
            assert (await cursor.fetchone())["timeout_seconds"] == 10
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_the_column_already_exists(self):
        from app.migrations._094_bot_timeout import migrate

        conn = await aiosqlite.connect(":memory:")
        try:
            await conn.execute(
                "CREATE TABLE bots (id TEXT PRIMARY KEY, timeout_seconds REAL DEFAULT 10)"
            )
            await migrate(conn)
            await migrate(conn)
        finally:
            await conn.close()
