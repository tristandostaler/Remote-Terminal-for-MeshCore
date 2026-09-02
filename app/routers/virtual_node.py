"""Operator view of the virtual companion node: who is connected, who is known.

The node itself lives in ``app/virtual_node``; this router only exposes its
state and two operator actions. The admin-commands switch is an app setting
(``PATCH /settings`` with ``virtual_node_allow_admin_commands``) and is
reported here so the UI has one payload to render.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.repository import AppSettingsRepository, ChannelRepository, VirtualNodeClientRepository
from app.virtual_node import virtual_node

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/virtual-node", tags=["virtual-node"])


class ConnectedVirtualNodeClient(BaseModel):
    peer: str = Field(description="Remote address:port of the TCP connection")
    client_id: str | None = Field(
        default=None, description="'<app name>@<host>' once the app has sent APP_START"
    )
    app_name: str = ""
    connected_at: int
    commands: int = Field(description="Commands served on this connection")
    queued_messages: int = Field(description="Frames waiting in this client's inbox")
    replayed_messages: int = Field(description="History frames queued for it on connect")


class KnownVirtualNodeClient(BaseModel):
    client_id: str
    app_name: str
    peer_host: str
    last_message_id: int = Field(description="Newest message id this client has pulled")
    first_seen: int
    last_seen: int
    connections: int
    connected: bool = Field(description="Whether a connection with this identity is open now")


class VirtualNodeCommand(BaseModel):
    """One command an app sent and what it was answered with."""

    at: int
    peer: str
    app_name: str = ""
    command: str = Field(description="Host command name, e.g. SEND_CHANNEL_TXT_MSG")
    result: str = Field(description="Response frame name, or the ERR_CODE_* returned")
    failed: bool = False
    detail: str = ""
    duration_ms: int = 0


class VirtualNodeChannelSlot(BaseModel):
    """Which channel an app finds at a given slot index. Slot 0 is always Public."""

    index: int
    key: str
    name: str | None = None


class VirtualNodeOverview(BaseModel):
    enabled: bool
    listening: bool
    host: str | None = None
    port: int | None = None
    read_only: bool
    replay_limit: int
    admin_commands_allowed: bool = Field(
        description="Apps may change radio settings (app setting, default off)"
    )
    client_count: int
    local_commands: int
    cached_commands: int
    forwarded_commands: int
    connected: list[ConnectedVirtualNodeClient]
    known_clients: list[KnownVirtualNodeClient]
    channel_slots: list[VirtualNodeChannelSlot] = Field(default_factory=list)
    recent_commands: list[VirtualNodeCommand] = Field(default_factory=list)


@router.get("", response_model=VirtualNodeOverview)
async def get_virtual_node_overview() -> VirtualNodeOverview:
    """Listener state, connected apps, remembered apps and the admin switch."""
    status = virtual_node.status()
    settings = await AppSettingsRepository.get()
    connected = [ConnectedVirtualNodeClient(**client) for client in status["clients"]]
    connected_ids = {c.client_id for c in connected if c.client_id}
    known = [
        KnownVirtualNodeClient(
            client_id=k.client_id,
            app_name=k.app_name,
            peer_host=k.peer_host,
            last_message_id=k.last_message_id,
            first_seen=k.first_seen,
            last_seen=k.last_seen,
            connections=k.connections,
            connected=k.client_id in connected_ids,
        )
        for k in await VirtualNodeClientRepository.list_all()
    ]
    channel_names = {c.key.upper(): c.name for c in await ChannelRepository.get_all()}
    channel_slots = [
        VirtualNodeChannelSlot(
            index=slot["index"], key=slot["key"], name=channel_names.get(slot["key"])
        )
        for slot in status["channel_slots"]
    ]
    return VirtualNodeOverview(
        enabled=status["enabled"],
        listening=status["listening"],
        host=status["host"],
        port=status["port"],
        read_only=status["read_only"],
        replay_limit=status["replay_limit"],
        admin_commands_allowed=settings.virtual_node_allow_admin_commands,
        client_count=status["client_count"],
        local_commands=status["local_commands"],
        cached_commands=status["cached_commands"],
        forwarded_commands=status["forwarded_commands"],
        connected=connected,
        known_clients=known,
        channel_slots=channel_slots,
        recent_commands=[VirtualNodeCommand(**entry) for entry in status["recent_commands"]],
    )


@router.delete("/clients/{client_id}")
async def forget_virtual_node_client(client_id: str) -> dict:
    """Forget a remembered app: its next connection starts at the present again."""
    if await VirtualNodeClientRepository.get(client_id) is None:
        raise HTTPException(status_code=404, detail="Unknown virtual node client")
    await VirtualNodeClientRepository.delete(client_id)
    logger.info("Forgot virtual node client %s", client_id)
    return {"status": "ok", "client_id": client_id}


@router.post("/connections/{peer}/disconnect")
async def disconnect_virtual_node_client(peer: str) -> dict:
    """Close one connected app's socket."""
    if not virtual_node.disconnect_peer(peer):
        raise HTTPException(status_code=404, detail="No such connection")
    return {"status": "ok", "peer": peer}
