"""Tests for database migration(s)."""

import aiosqlite
import pytest

from app.migrations import get_version, run_migrations, set_version
from tests.test_migrations.conftest import LATEST_SCHEMA_VERSION


class TestMigration079:
    """Test migration 079: keeping media this build cannot decode.

    The tables exist so that adding a decoder later can read pictures already
    received. Nothing expires on a timer, so the cascade to the marker message is
    the only thing that ever releases the bytes -- which makes it the part worth
    pinning.
    """

    @staticmethod
    async def _db() -> aiosqlite.Connection:
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA foreign_keys = ON")
        await set_version(conn, 78)
        await conn.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT)"
        )
        await conn.commit()
        return conn

    @pytest.mark.asyncio
    async def test_creates_both_tables(self):
        conn = await self._db()
        try:
            await run_migrations(conn)
            assert await get_version(conn) == LATEST_SCHEMA_VERSION

            cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
            tables = {row["name"] for row in await cursor.fetchall()}
            assert "unsupported_media" in tables
            assert "unsupported_media_blobs" in tables
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_deleting_the_message_takes_the_payloads_with_it(self):
        """The entire retention rule. With no TTL anywhere, a cascade that did not
        fire would mean these bytes could never be reclaimed at all."""
        conn = await self._db()
        try:
            await run_migrations(conn)
            await conn.execute("INSERT INTO messages (id, text) VALUES (7, 'mediax:1')")
            await conn.execute(
                "INSERT INTO unsupported_media (id, message_id, conversation_key, data_type, "
                "codec_label, received_at, last_blob_at) VALUES (1, 7, 'ab', 65520, 'MCOimg', 1, 1)"
            )
            await conn.execute(
                "INSERT INTO unsupported_media_blobs (media_id, idx, payload) VALUES (1, 0, X'00')"
            )
            await conn.commit()

            await conn.execute("DELETE FROM messages WHERE id = 7")
            await conn.commit()

            for table in ("unsupported_media", "unsupported_media_blobs"):
                cursor = await conn.execute(f"SELECT COUNT(*) AS n FROM {table}")
                row = await cursor.fetchone()
                assert row is not None and row["n"] == 0, f"{table} outlived its message"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_a_blob_index_cannot_be_reused_within_one_arrival(self):
        """Order is the only structure kept for an unparseable format, so two
        payloads claiming the same position would corrupt a future decode."""
        conn = await self._db()
        try:
            await run_migrations(conn)
            await conn.execute(
                "INSERT INTO unsupported_media (id, message_id, conversation_key, data_type, "
                "codec_label, received_at, last_blob_at) VALUES (1, NULL, 'ab', 65520, 'x', 1, 1)"
            )
            await conn.execute(
                "INSERT INTO unsupported_media_blobs (media_id, idx, payload) VALUES (1, 0, X'00')"
            )
            await conn.commit()

            with pytest.raises(aiosqlite.IntegrityError):
                await conn.execute(
                    "INSERT INTO unsupported_media_blobs (media_id, idx, payload) "
                    "VALUES (1, 0, X'11')"
                )
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_survives_a_database_with_no_messages_table(self):
        conn = await aiosqlite.connect(":memory:")
        conn.row_factory = aiosqlite.Row
        try:
            await set_version(conn, 78)
            await conn.commit()

            await run_migrations(conn)

            assert await get_version(conn) == LATEST_SCHEMA_VERSION
        finally:
            await conn.close()
