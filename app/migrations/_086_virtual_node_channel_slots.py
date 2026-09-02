import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Durable channel-slot assignments for the virtual companion node.

    Apps address a channel by its slot index, and they cache that index. The
    virtual node used to assign slots in memory, so a restart re-derived them
    from the channel store: adding or removing a channel then shifted the
    others, and an app's cached "slot 3" silently became a different channel.
    The message still went out and was still repeated -- encrypted for a
    channel nobody in the conversation was listening to.

    Keeping the assignment here makes an index mean the same channel for good.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS virtual_node_channel_slots (
            slot INTEGER PRIMARY KEY,
            channel_key TEXT NOT NULL UNIQUE
        )
        """
    )
    await conn.commit()
