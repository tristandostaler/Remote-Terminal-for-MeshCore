"""Virtual MeshCore companion node.

A TCP server that speaks the companion wire protocol so that MeshCore apps
(the mobile app over WiFi, meshcore-cli, meshcore.js, Home Assistant, ...) can
connect to RemoteTerm as if it were the radio. RemoteTerm keeps the single
physical connection and answers most requests from its own mirrored state,
forwarding only what genuinely has to reach the radio.

See ``AGENTS_virtual_node.md`` in this package for the design.
"""

from app.virtual_node.server import VirtualNodeServer, virtual_node

__all__ = ["VirtualNodeServer", "virtual_node"]
