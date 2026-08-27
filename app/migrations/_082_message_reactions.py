import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """MeshCore Open Advanced compatible emoji reactions.

    Adds to ``messages``:

    - ``reactions`` (TEXT, JSON ``{emoji: count}``) -- reactions other nodes (or
      we) attached to this message. NULL means none.
    - ``is_reaction`` (INTEGER, default 0) -- the row itself is a reaction
      payload (``r:HHHH:II``, with the usual ``"Name: "`` prefix on channel
      rows). Such rows stay stored so flood-echo dedup keeps working, but every
      conversation surface hides them; their effect lives in the target
      message's ``reactions``.

    Existing stored reaction payloads (received before this feature and shown
    as raw ``r:...`` bubbles until now) are backfilled to ``is_reaction = 1`` so
    they disappear from conversations. They are not retroactively applied to
    their targets -- matching history against a moving window would guess, and
    MCO Advanced also drops reactions it could not match live.
    """
    tables_cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in await tables_cursor.fetchall()}
    if "messages" not in existing_tables:
        await conn.commit()
        return

    col_cursor = await conn.execute("PRAGMA table_info(messages)")
    message_columns = {row[1] for row in await col_cursor.fetchall()}
    if "reactions" not in message_columns:
        await conn.execute("ALTER TABLE messages ADD COLUMN reactions TEXT")
    if "is_reaction" not in message_columns:
        await conn.execute("ALTER TABLE messages ADD COLUMN is_reaction INTEGER NOT NULL DEFAULT 0")

    # Backfill: hide historical reaction payloads. GLOB is case-sensitive and
    # supports character classes, which matches the strict lowercase wire form.
    # Guarded on the real schema: some migration tests replay this over minimal
    # legacy tables that predate the type/text columns.
    if {"type", "text"} <= message_columns:
        reaction_glob = "r:[0-9a-f][0-9a-f][0-9a-f][0-9a-f]:[0-9a-f][0-9a-f]"
        await conn.execute(
            "UPDATE messages SET is_reaction = 1 WHERE type = 'PRIV' AND text GLOB ?",
            (reaction_glob,),
        )
        # Channel rows carry the wire "Name: body" form. The sender split that
        # the runtime applies accepts both "Name: r:..." and "Name:r:...".
        await conn.execute(
            "UPDATE messages SET is_reaction = 1 WHERE type = 'CHAN' "
            "AND (text GLOB ? OR text GLOB ?)",
            (f"*: {reaction_glob}", f"*:{reaction_glob}"),
        )
    await conn.commit()
