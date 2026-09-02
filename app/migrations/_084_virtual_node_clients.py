import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Per-client history cursors for the virtual companion node.

    An app connecting to the virtual node is identified by the name it sends
    in ``CMD_APP_START`` plus the address it connects from. The row remembers
    the newest message id that client has pulled, so on its next connection
    the node can replay what it missed instead of starting from "now".
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS virtual_node_clients (
            client_id TEXT PRIMARY KEY,
            app_name TEXT NOT NULL DEFAULT '',
            peer_host TEXT NOT NULL DEFAULT '',
            last_message_id INTEGER NOT NULL DEFAULT 0,
            first_seen INTEGER NOT NULL,
            last_seen INTEGER NOT NULL,
            connections INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await conn.commit()
