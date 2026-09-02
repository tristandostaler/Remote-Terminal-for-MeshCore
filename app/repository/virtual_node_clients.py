"""History cursors for apps connected to the virtual companion node.

The companion protocol has no notion of a client identity: ``CMD_APP_START``
carries only the app's *name* ("MeshCore", "mccli", ...), so two phones
running the same app look alike on the wire. The best stable handle available
is that name combined with the address the connection came from, which is what
``client_id`` holds (``"<app_name>@<peer_host>"``). It is a heuristic and is
documented as such: a phone that changes IP address starts a fresh cursor.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.database import db


@dataclass(slots=True)
class VirtualNodeClient:
    client_id: str
    app_name: str
    peer_host: str
    last_message_id: int
    first_seen: int
    last_seen: int
    connections: int


def _row_to_client(row: Any) -> VirtualNodeClient:
    return VirtualNodeClient(
        client_id=row["client_id"],
        app_name=row["app_name"],
        peer_host=row["peer_host"],
        last_message_id=int(row["last_message_id"] or 0),
        first_seen=int(row["first_seen"]),
        last_seen=int(row["last_seen"]),
        connections=int(row["connections"] or 0),
    )


class VirtualNodeClientRepository:
    @staticmethod
    async def get(client_id: str) -> VirtualNodeClient | None:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM virtual_node_clients WHERE client_id = ?", (client_id,)
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_client(row) if row else None

    @staticmethod
    async def record_connection(
        client_id: str,
        *,
        app_name: str,
        peer_host: str,
        now: int,
        initial_message_id: int,
    ) -> VirtualNodeClient:
        """Create the client on first sight (cursor at ``initial_message_id``) or bump its stats."""
        async with db.tx() as conn:
            async with conn.execute(
                """
                INSERT INTO virtual_node_clients
                    (client_id, app_name, peer_host, last_message_id, first_seen, last_seen, connections)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(client_id) DO UPDATE SET
                    app_name = excluded.app_name,
                    peer_host = excluded.peer_host,
                    last_seen = excluded.last_seen,
                    connections = virtual_node_clients.connections + 1
                """,
                (client_id, app_name, peer_host, initial_message_id, now, now),
            ):
                pass
            async with conn.execute(
                "SELECT * FROM virtual_node_clients WHERE client_id = ?", (client_id,)
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        return _row_to_client(row)

    @staticmethod
    async def advance_cursor(client_id: str, last_message_id: int, *, now: int) -> None:
        """Move the cursor forward; never backwards."""
        async with db.tx() as conn:
            async with conn.execute(
                """
                UPDATE virtual_node_clients
                SET last_message_id = MAX(last_message_id, ?), last_seen = ?
                WHERE client_id = ?
                """,
                (last_message_id, now, client_id),
            ):
                pass

    @staticmethod
    async def list_all() -> list[VirtualNodeClient]:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM virtual_node_clients ORDER BY last_seen DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_client(row) for row in rows]

    @staticmethod
    async def delete(client_id: str) -> None:
        async with db.tx() as conn:
            async with conn.execute(
                "DELETE FROM virtual_node_clients WHERE client_id = ?", (client_id,)
            ):
                pass
