import json
import logging

import aiosqlite

logger = logging.getLogger(__name__)

# The #bot / #bots hashtag keys — see app/bot_scope.py. Inlined rather than
# imported so this migration keeps describing the schema change it made even if
# the default later moves on.
DEFAULT_SCOPE = {
    "channels": {"only": ["EB50A1BCB3E4E5D7BF69A57C9DADA211", "0D24F5830B449668B8C221759B6C50D2"]}
}


async def migrate(conn: aiosqlite.Connection) -> None:
    """Retarget never-enabled bots from "all channels" to the #bot/#bots default.

    Bots used to default to every channel, so enabling one made the node answer
    commands on Public and on every other channel it carries. New bots now start
    scoped to ``#bot`` / ``#bots`` plus DMs.

    Only rows that are **still at the old default and still disabled** are
    rewritten: a disabled bot has never been put in service, so its scope is a
    default rather than a decision. Anything the operator enabled — or scoped by
    hand to ``only`` / ``except`` / ``none`` — is left exactly as it is.
    """
    table_check = await conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='bots'"
    )
    if not await table_check.fetchone():
        return

    cursor = await conn.execute("SELECT id, scope FROM bots WHERE enabled = 0")
    rows = await cursor.fetchall()

    retargeted = 0
    for row in rows:
        try:
            scope = json.loads(row[1]) if row[1] else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(scope, dict) or scope.get("channels") != "all":
            continue
        await conn.execute(
            "UPDATE bots SET scope = ? WHERE id = ?",
            (json.dumps(DEFAULT_SCOPE), row[0]),
        )
        retargeted += 1

    if retargeted:
        logger.info("Scoped %d disabled bot(s) to the #bot/#bots default", retargeted)

    await conn.commit()
