"""Durable channel-slot assignments for the virtual companion node.

A connected app addresses a channel by a one-byte slot index and caches that
index alongside the channel name it read once. The index therefore has to keep
meaning the same channel across restarts and across channels being added and
removed -- otherwise a send goes out encrypted for whatever channel now sits in
that slot, which looks exactly like a successful send and reaches nobody.
"""

from __future__ import annotations

from app.database import db


class VirtualNodeChannelSlotRepository:
    @staticmethod
    async def get_all() -> dict[int, str]:
        """Slot index -> upper-case channel key, for every assigned slot."""
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT slot, channel_key FROM virtual_node_channel_slots ORDER BY slot"
            ) as cursor:
                rows = await cursor.fetchall()
        return {int(row["slot"]): str(row["channel_key"]).upper() for row in rows}

    @staticmethod
    async def replace_all(slots: list[str | None]) -> None:
        """Persist the slot table as it now stands (blank slots are not stored)."""
        assignments = [(index, key.upper()) for index, key in enumerate(slots) if key is not None]
        async with db.tx() as conn:
            async with conn.execute("DELETE FROM virtual_node_channel_slots"):
                pass
            if assignments:
                await conn.executemany(
                    "INSERT INTO virtual_node_channel_slots (slot, channel_key) VALUES (?, ?)",
                    assignments,
                )
