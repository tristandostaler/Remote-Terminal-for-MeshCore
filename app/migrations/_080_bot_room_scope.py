import json
import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Give every bot a ``rooms`` selection inside its ``scope``.

    Room-server posts arrive as direct messages from the room's own contact, so
    until now they passed the ``respond_to_dms`` gate and the bot answered by
    DMing the poster privately — the room never saw the reply. Rooms are now
    their own conversation kind, picked from a list the same way channels are:
    ``scope.rooms`` is ``"all"``, ``"none"``, or ``{"only"|"except": [keys]}``.

    Existing rows inherit ``respond_to_dms`` — all rooms if the bot answered
    DMs, no rooms if it did not — so no bot gains or loses reach over the
    upgrade; only where its answer lands changes. A scope that already names
    rooms is left alone, and a missing key means every room, so nothing depends
    on this having run.
    """
    table_check = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bots'"
    )
    if not await table_check.fetchone():
        return

    cursor = await conn.execute("PRAGMA table_info(bots)")
    columns = {row[1] for row in await cursor.fetchall()}
    if "scope" not in columns:
        return
    # A database old enough to predate the DM gate has nothing to inherit from;
    # "all rooms" is the default either way.
    has_dm_gate = "respond_to_dms" in columns

    dm_gate = "respond_to_dms" if has_dm_gate else "1"
    cursor = await conn.execute(f"SELECT id, scope, {dm_gate} FROM bots")
    rows = await cursor.fetchall()

    updated = 0
    for bot_id, raw_scope, answers_dms in rows:
        try:
            scope = json.loads(raw_scope) if raw_scope else {}
        except (json.JSONDecodeError, TypeError):
            # A scope we cannot read is not one to rewrite: migration 071 met the
            # same case and left it for the operator.
            logger.warning("Bot %s has an unreadable scope; leaving it alone", bot_id)
            continue
        if not isinstance(scope, dict) or "rooms" in scope:
            continue
        scope["rooms"] = "all" if answers_dms else {"only": []}
        await conn.execute("UPDATE bots SET scope = ? WHERE id = ?", (json.dumps(scope), bot_id))
        updated += 1

    await conn.commit()
    if updated:
        logger.info("Gave %d bot(s) a room scope inherited from respond_to_dms", updated)
