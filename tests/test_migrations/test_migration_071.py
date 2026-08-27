"""Tests for database migration 071: default bot scope."""

import json

import aiosqlite
import pytest

from app.bot_scope import default_bot_scope, is_default_bot_scope
from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION

BOTS_TABLE = """
    CREATE TABLE bots (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        enabled INTEGER DEFAULT 0,
        scope TEXT NOT NULL DEFAULT '{"channels": "all"}'
    )
"""


class TestMigration071:
    """Migration 071: retarget never-enabled bots off "all channels"."""

    @pytest.mark.asyncio
    async def test_retargets_only_disabled_bots_still_at_the_old_default(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 70)
            await conn.execute(BOTS_TABLE)
            rows = [
                # Never enabled, never scoped — a default, not a decision.
                ("untouched", 0, '{"channels": "all"}'),
                # Enabled on every channel: the operator's working config.
                ("in-service", 1, '{"channels": "all"}'),
                # Scoped by hand, whatever its enabled state.
                ("hand-scoped", 0, '{"channels": {"only": ["' + "A" * 32 + '"]}}'),
                ("muted", 0, '{"channels": "none"}'),
                ("excepting", 0, '{"channels": {"except": ["' + "B" * 32 + '"]}}'),
            ]
            for bot_id, enabled, scope in rows:
                await conn.execute(
                    "INSERT INTO bots (id, name, enabled, scope) VALUES (?, ?, ?, ?)",
                    (bot_id, bot_id, enabled, scope),
                )
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 70
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            async with conn.execute("SELECT id, scope FROM bots") as cursor:
                after = {row["id"]: json.loads(row["scope"]) for row in await cursor.fetchall()}

            # Only the channels half is 071's business. Later migrations add
            # sibling keys to the same dict (080 adds `rooms`), so compare that
            # half rather than the whole scope.
            assert is_default_bot_scope(after["untouched"])
            assert after["in-service"]["channels"] == "all"
            assert after["hand-scoped"]["channels"] == {"only": ["A" * 32]}
            assert after["muted"]["channels"] == "none"
            assert after["excepting"]["channels"] == {"except": ["B" * 32]}
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_matches_the_current_default_scope(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 70)
            await conn.execute(BOTS_TABLE)
            await conn.execute(
                "INSERT INTO bots (id, name, enabled, scope) VALUES (?, ?, 0, ?)",
                ("wx", "wx", '{"channels": "all"}'),
            )
            await conn.commit()

            await run_migrations(conn)

            async with conn.execute("SELECT scope FROM bots WHERE id = 'wx'") as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert json.loads(row["scope"])["channels"] == default_bot_scope()["channels"]
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_survives_a_malformed_scope_and_a_missing_table(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 70)
            await conn.execute(BOTS_TABLE)
            await conn.execute(
                "INSERT INTO bots (id, name, enabled, scope) VALUES ('junk', 'junk', 0, 'not json')"
            )
            await conn.commit()

            await run_migrations(conn)

            async with conn.execute("SELECT scope FROM bots WHERE id = 'junk'") as cursor:
                row = await cursor.fetchone()
            assert row is not None
            assert row["scope"] == "not json"
        finally:
            await conn.close()

        # A database that predates the bots table at all must still migrate.
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 70)
            applied = await run_migrations(conn)
            assert applied == LATEST_SCHEMA_VERSION - 70
        finally:
            await conn.close()
