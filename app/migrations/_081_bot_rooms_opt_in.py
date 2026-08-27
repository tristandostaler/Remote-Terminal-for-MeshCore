import json
import logging

import aiosqlite

from app.bot_scope import no_rooms

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Retarget bots left on "all rooms" onto the empty room pick list.

    Migration 080 originally had every bot inherit ``respond_to_dms`` as its room
    selection, so a node that upgraded while that version shipped has bots
    storing ``"rooms": "all"`` — and 080 will never revisit them, because the
    runner skips a migration whose number the database has already recorded.
    Rooms became opt-in right after, so those bots keep a public voice in every
    room the node is logged into while a bot created today has none.

    The stored value is the same ``"all"`` an operator gets by clicking "All
    rooms" themselves, so this cannot tell an inherited default from a decision
    and resets both. That is the trade the opt-in default asks for: the reach it
    takes away is public — a room answer goes to everyone logged in — and the
    picker is two clicks. Anything narrower (a named list, "all except", or the
    empty list already) is untouched, as is a scope that never named rooms:
    the engine reads that as no room either way.
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
            # A scope we cannot read is not one to rewrite: migrations 071 and
            # 080 met the same case and left it for the operator.
            logger.warning("Bot %s has an unreadable scope; leaving it alone", bot_id)
            continue
        if not isinstance(scope, dict) or scope.get("rooms") != "all":
            continue
        scope["rooms"] = no_rooms()
        await conn.execute("UPDATE bots SET scope = ? WHERE id = ?", (json.dumps(scope), bot_id))
        updated += 1

    await conn.commit()
    if updated:
        logger.info(
            "Took %d bot(s) off 'all rooms'; pick the rooms each one may answer in", updated
        )
