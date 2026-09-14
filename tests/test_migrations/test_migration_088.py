"""Tests for migration 088: live feed comparison mirror + settings."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


async def _fresh_conn() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.execute("CREATE TABLE app_settings (id INTEGER PRIMARY KEY CHECK (id = 1))")
    await conn.execute("INSERT INTO app_settings (id) VALUES (1)")
    return conn


class TestMigration088:
    @pytest.mark.asyncio
    async def test_creates_table_and_settings_columns(self):
        conn = await _fresh_conn()
        try:
            await set_version(conn, 87)
            await conn.commit()

            applied = await run_migrations(conn)

            assert applied == LATEST_SCHEMA_VERSION - 87
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            cursor = await conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='live_feed_messages'"
            )
            assert await cursor.fetchone() is not None

            cursor = await conn.execute("PRAGMA table_info(live_feed_messages)")
            columns = {row["name"] for row in await cursor.fetchall()}
            assert {
                "packet_hash",
                "channel_name",
                "channel_key",
                "sender",
                "text",
                "sender_timestamp",
                "first_seen",
                "last_seen",
                "repeats",
                "observers",
                "hops",
                "snr",
                "scope_name",
                "fetched_at",
            } <= columns

            cursor = await conn.execute("PRAGMA table_info(app_settings)")
            settings_columns = {row["name"] for row in await cursor.fetchall()}
            assert {
                "live_feed_enabled",
                "live_feed_url",
                "live_feed_region",
                "live_feed_channels",
                "live_feed_poll_interval",
            } <= settings_columns

            cursor = await conn.execute(
                "SELECT live_feed_enabled, live_feed_url, live_feed_channels, "
                "live_feed_poll_interval FROM app_settings WHERE id = 1"
            )
            row = await cursor.fetchone()
            assert row["live_feed_enabled"] == 0
            assert row["live_feed_url"] == "https://live.meshcore.ca"
            assert row["live_feed_channels"] == '["*"]'
            assert row["live_feed_poll_interval"] == 300
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_is_idempotent(self):
        conn = await _fresh_conn()
        try:
            await set_version(conn, 87)
            await conn.commit()
            await run_migrations(conn)

            from app.migrations._088_live_feed import migrate

            await migrate(conn)  # must not raise on the already-migrated schema

            cursor = await conn.execute("PRAGMA table_info(app_settings)")
            names = [row["name"] for row in await cursor.fetchall()]
            assert names.count("live_feed_url") == 1
        finally:
            await conn.close()
