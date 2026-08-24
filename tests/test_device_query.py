"""Tests for the shared DEVICE_INFO query helper."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore.packets import PacketType

from app.services.device_query import query_device_info


def _mock_meshcore(frames: list[bytes] | None = None, payload: dict | None = None):
    mc = MagicMock()
    mc._reader = MagicMock()
    original_handle_rx = AsyncMock()
    mc._reader.handle_rx = original_handle_rx

    async def _send_device_query():
        for frame in frames or []:
            await mc._reader.handle_rx(bytearray(frame))
        return MagicMock(payload=payload if payload is not None else {})

    mc.commands.send_device_query = AsyncMock(side_effect=_send_device_query)
    return mc, original_handle_rx


class TestQueryDeviceInfo:
    @pytest.mark.asyncio
    async def test_returns_payload_and_captured_frame(self):
        frame = bytes([PacketType.DEVICE_INFO.value, 10]) + bytes(80)
        mc, original_handle_rx = _mock_meshcore([frame], {"fw ver": 10})

        result = await query_device_info(mc)

        assert result.payload == {"fw ver": 10}
        assert result.raw_frame == frame
        # The original handler still ran, and was restored afterwards.
        original_handle_rx.assert_awaited_once()
        assert mc._reader.handle_rx is original_handle_rx

    @pytest.mark.asyncio
    async def test_ignores_unrelated_frames(self):
        other = bytes([PacketType.BATTERY.value, 1, 2])
        mc, _ = _mock_meshcore([other], {"fw ver": 8})

        result = await query_device_info(mc)

        assert result.raw_frame is None

    @pytest.mark.asyncio
    async def test_restores_handler_when_query_fails(self):
        mc, original_handle_rx = _mock_meshcore()
        mc.commands.send_device_query = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await query_device_info(mc)

        assert mc._reader.handle_rx is original_handle_rx

    @pytest.mark.asyncio
    async def test_tolerates_missing_payload(self):
        mc, _ = _mock_meshcore()
        mc.commands.send_device_query = AsyncMock(return_value=None)

        result = await query_device_info(mc)

        assert result.payload == {}
        assert result.raw_frame is None
