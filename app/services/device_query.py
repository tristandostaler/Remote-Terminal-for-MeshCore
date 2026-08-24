"""Shared plumbing for the companion DEVICE_INFO query.

The MeshCore library parses the device-query response into a dict, but the
newest fields (``path_hash_mode``, ``repeat``) are only decoded by recent
library versions -- and a stale ``.pyc`` on WSL2 Windows mounts can silently
drop them from an otherwise healthy response.  Callers therefore keep the raw
frame alongside the parsed payload so those fields can be read positionally.

Raw DEVICE_INFO frame layout (byte 0 is the packet type):
    1       fw_ver
    2       max_contacts / 2
    3       max_channels
    4:8     ble_pin
    8:20    fw_build
    20:60   model
    60:80   ver
    80      repeat (fw_ver >= 9)
    81      path_hash_mode (fw_ver >= 10)
"""

from dataclasses import dataclass

DEVICE_INFO_REPEAT_OFFSET = 80
DEVICE_INFO_PATH_HASH_MODE_OFFSET = 81


@dataclass(slots=True)
class DeviceQueryResult:
    """Parsed payload plus the raw frame it was decoded from (when captured)."""

    payload: dict
    raw_frame: bytes | None


async def query_device_info(mc) -> DeviceQueryResult:
    """Send a device query, returning both the parsed payload and raw frame."""
    from meshcore.packets import PacketType

    reader = mc._reader
    original_handle_rx = reader.handle_rx
    captured: list[bytes] = []

    async def _capture_handle_rx(data: bytearray) -> None:
        if len(data) > 0 and data[0] == PacketType.DEVICE_INFO.value:
            captured.append(bytes(data))
        return await original_handle_rx(data)

    reader.handle_rx = _capture_handle_rx
    try:
        event = await mc.commands.send_device_query()
    finally:
        reader.handle_rx = original_handle_rx

    payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
    return DeviceQueryResult(payload=payload, raw_frame=captured[-1] if captured else None)
