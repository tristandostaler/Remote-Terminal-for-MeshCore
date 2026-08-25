"""Tests for database migration(s)."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration073:
    """Test migration 073: persist the noise-floor series."""

    @pytest.mark.asyncio
    async def test_creates_noise_floor_table(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 72)
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 72
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='noise_floor_samples'"
            )
            assert await cursor.fetchone() is not None

            cursor = await conn.execute("PRAGMA table_info(noise_floor_samples)")
            columns = {row["name"]: row for row in await cursor.fetchall()}
            assert set(columns) == {"timestamp", "noise_floor_dbm"}
            # timestamp is the rowid alias, so window scans stay index-ordered
            assert columns["timestamp"]["pk"] == 1
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent_when_table_already_exists(self):
        """Re-running against a database that already has the table is a no-op."""
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 72)
            await conn.execute(
                """
                CREATE TABLE noise_floor_samples (
                    timestamp INTEGER PRIMARY KEY,
                    noise_floor_dbm INTEGER NOT NULL
                )
                """
            )
            await conn.execute(
                "INSERT INTO noise_floor_samples (timestamp, noise_floor_dbm) VALUES (?, ?)",
                (1_700_000_000, -118),
            )
            await conn.commit()

            await run_migrations(conn)

            cursor = await conn.execute("SELECT COUNT(*) AS cnt FROM noise_floor_samples")
            row = await cursor.fetchone()
            assert row is not None and row["cnt"] == 1
        finally:
            await conn.close()
