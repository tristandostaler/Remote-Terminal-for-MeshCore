import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Add the ``respond_to_rooms`` column to bots.

    Room-server posts arrive as direct messages from the room's own contact, so
    until now they passed the ``respond_to_dms`` gate and the bot answered by
    DMing the poster privately — the room never saw the reply. Rooms are now
    their own conversation kind: the gate is separate and ``ctx.reply`` posts
    back into the room.

    Existing rows inherit ``respond_to_dms`` so no bot loses (or gains) reach
    over the upgrade — only where its answer lands changes. Turning a bot off in
    rooms is a checkbox on its Settings tab.
    """
    table_check = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bots'"
    )
    if not await table_check.fetchone():
        return

    cursor = await conn.execute("PRAGMA table_info(bots)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "respond_to_rooms" not in columns:
        await conn.execute("ALTER TABLE bots ADD COLUMN respond_to_rooms INTEGER DEFAULT 1")
        # A database old enough to predate the DM gate has nothing to inherit
        # from; the column default already says yes.
        if "respond_to_dms" in columns:
            await conn.execute("UPDATE bots SET respond_to_rooms = COALESCE(respond_to_dms, 1)")

    await conn.commit()
