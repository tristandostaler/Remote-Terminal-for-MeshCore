"""Companion CLI over the host link: frame 66 out, frame 29 back."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore import EventType

from app.services import radio_cli
from app.services.radio_cli import (
    CMD_RUN_CLI_COMMAND,
    RESP_CODE_CLI_REPLY,
    CliCommandError,
    CliUnsupportedError,
    firmware_supports_cli,
    install_cli_reply_adapter,
    run_cli_command,
)


def _fake_mc(*, reply: bytes | None = None, error: bool = False):
    """A MeshCore stub whose send() makes the reader 'receive' the scripted answer."""
    mc = MagicMock()
    original_rx = AsyncMock()
    mc._reader = MagicMock()
    mc._reader.handle_rx = original_rx
    mc._reader._remoteterm_cli_reply = False
    error_handlers: list = []

    def _subscribe(event_type, handler):
        assert event_type == EventType.ERROR
        error_handlers.append(handler)
        return MagicMock()

    mc.subscribe = MagicMock(side_effect=_subscribe)

    async def _send(frame: bytes, *args, **kwargs):
        mc.sent = frame
        if reply is not None:
            await mc._reader.handle_rx(bytearray([RESP_CODE_CLI_REPLY]) + bytearray(reply))
        if error:
            for handler in error_handlers:
                handler(MagicMock(payload={"error": "unsupported"}))
        return MagicMock(type=EventType.OK)

    mc.commands.send = AsyncMock(side_effect=_send)
    mc._original_rx = original_rx
    return mc


class TestFirmwareGate:
    def test_needs_protocol_14(self):
        assert firmware_supports_cli(14) is True
        assert firmware_supports_cli(20) is True
        assert firmware_supports_cli(13) is False
        assert firmware_supports_cli(None) is False


class TestAdapter:
    @pytest.mark.asyncio
    async def test_other_frames_pass_through_and_install_is_idempotent(self):
        mc = _fake_mc()
        install_cli_reply_adapter(mc)
        wrapped = mc._reader.handle_rx
        install_cli_reply_adapter(mc)
        assert mc._reader.handle_rx is wrapped

        await mc._reader.handle_rx(bytearray([0x05, 0x01]))
        mc._original_rx.assert_awaited_once_with(bytearray([0x05, 0x01]))

    @pytest.mark.asyncio
    async def test_unsolicited_reply_is_dropped_quietly(self):
        mc = _fake_mc()
        install_cli_reply_adapter(mc)
        await mc._reader.handle_rx(bytearray([RESP_CODE_CLI_REPLY]) + b"stray")
        mc._original_rx.assert_not_awaited()


class TestRunCliCommand:
    @pytest.mark.asyncio
    async def test_sends_frame_66_and_returns_reply_text(self):
        mc = _fake_mc(reply=b"OK - freq set\x00")

        reply = await run_cli_command(mc, "  set freq 910.525 ", firmware_ver_code=14)

        assert reply == "OK - freq set"
        assert mc.sent == bytes([CMD_RUN_CLI_COMMAND]) + b"set freq 910.525"
        assert radio_cli._pending_reply is None

    @pytest.mark.asyncio
    async def test_old_firmware_is_refused_before_sending(self):
        mc = _fake_mc(reply=b"never")
        with pytest.raises(CliUnsupportedError):
            await run_cli_command(mc, "ver", firmware_ver_code=13)
        mc.commands.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_error_frame_raises(self):
        mc = _fake_mc(error=True)
        with pytest.raises(CliCommandError):
            await run_cli_command(mc, "ver", firmware_ver_code=14)

    @pytest.mark.asyncio
    async def test_silence_times_out(self):
        mc = _fake_mc()
        with pytest.raises(asyncio.TimeoutError):
            await run_cli_command(mc, "ver", firmware_ver_code=14, timeout=0.01)
        assert radio_cli._pending_reply is None

    @pytest.mark.asyncio
    async def test_rejects_empty_and_oversized_commands(self):
        mc = _fake_mc()
        with pytest.raises(ValueError):
            await run_cli_command(mc, "   ", firmware_ver_code=14)
        with pytest.raises(ValueError):
            await run_cli_command(mc, "x" * 161, firmware_ver_code=14)
        mc.commands.send.assert_not_awaited()
