import json
import logging

import aiosqlite

from app.bot_scope import no_rooms

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Spell out every bot's ``rooms`` selection: no rooms, until one is picked.

    Room-server posts arrive as direct messages from the room's own contact, so
    until now they passed the ``respond_to_dms`` gate and the bot answered by
    DMing the poster privately — the room never saw the reply. Rooms are now
    their own conversation kind, picked from a list the same way channels are:
    ``scope.rooms`` is ``"all"``, ``"none"``, or ``{"only"|"except": [keys]}``.

    Rooms are opt-in, because the answer is now public to everyone logged in
    rather than a DM to one person, so existing rows get the same empty pick
    list a new bot starts with and the operator names the rooms each bot may
    speak in. That is what the engine already reads a missing key as, so this
    only makes the stored scope say out loud what it means; a scope that already
    names rooms is left alone.
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

    cursor = await conn.execute("SELECT id, scope FROM bots")
    rows = await cursor.fetchall()

    updated = 0
    for bot_id, raw_scope in rows:
        try:
            scope = json.loads(raw_scope) if raw_scope else {}
        except (json.JSONDecodeError, TypeError):
            # A scope we cannot read is not one to rewrite: migration 071 met the
            # same case and left it for the operator.
            logger.warning("Bot %s has an unreadable scope; leaving it alone", bot_id)
            continue
        if not isinstance(scope, dict) or "rooms" in scope:
            continue
        scope["rooms"] = no_rooms()
        await conn.execute("UPDATE bots SET scope = ? WHERE id = ?", (json.dumps(scope), bot_id))
        updated += 1

    await conn.commit()
    if updated:
        logger.info("Gave %d bot(s) an empty room scope to pick from", updated)
