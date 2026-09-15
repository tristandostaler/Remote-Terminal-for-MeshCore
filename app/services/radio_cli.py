"""Run companion-firmware CLI commands over the host link.

Companion firmware with protocol version 14 or newer accepts
``CMD_RUN_CLI_COMMAND`` (66): the frame is the command byte followed by the
command text, and the radio answers with ``RESP_CODE_CLI_REPLY`` (29) carrying
the reply text. The pinned meshcore-py (2.3.7) predates both codes: its reader
logs frame 29 as "Unhandled packet type" and dispatches nothing, and its
``CommandType`` has no member for 66. So the frame is built by hand and the
reply is caught with the same ``reader.handle_rx`` wrap the GRP_DATA adapter
uses, resolved into a plain future rather than an event the library has no
type for.

One command at a time: the reply frame carries no correlation id, so a second
command in flight would take the first one's answer. Callers hold the radio
operation lock, which already serializes them.
"""

from __future__ import annotations

import asyncio
import logging

from meshcore import EventType

logger = logging.getLogger(__name__)

CMD_RUN_CLI_COMMAND = 66
RESP_CODE_CLI_REPLY = 29
# Companion protocol version (DEVICE_INFO "fw ver") that introduced the command,
# the same gate meshcore-cli uses before offering its ``cli`` command.
MIN_FIRMWARE_VER_CODE = 14
DEFAULT_TIMEOUT_SECONDS = 8.0
MAX_COMMAND_BYTES = 160

_pending_reply: asyncio.Future[str] | None = None


class CliUnsupportedError(RuntimeError):
    """The connected firmware is too old for CMD_RUN_CLI_COMMAND."""


class CliCommandError(RuntimeError):
    """The radio answered with an error frame instead of a CLI reply."""


def firmware_supports_cli(firmware_ver_code: int | None) -> bool:
    return firmware_ver_code is not None and firmware_ver_code >= MIN_FIRMWARE_VER_CODE


def _deliver_reply(text: str) -> None:
    future = _pending_reply
    if future is not None and not future.done():
        future.set_result(text)
    else:
        logger.debug("Unsolicited CLI reply from the radio: %r", text[:80])


def install_cli_reply_adapter(meshcore) -> None:
    """Catch RESP_CODE_CLI_REPLY frames before the library drops them.

    Idempotent per reader, like the other adapters, so reconnects do not stack.
    """
    reader = getattr(meshcore, "_reader", None)
    if reader is None or getattr(reader, "_remoteterm_cli_reply", False):
        return
    original = reader.handle_rx

    async def handle_rx(data: bytearray) -> None:
        if data and data[0] == RESP_CODE_CLI_REPLY:
            _deliver_reply(bytes(data[1:]).decode("utf-8", "ignore").rstrip("\x00"))
            return
        await original(data)

    reader.handle_rx = handle_rx
    reader._remoteterm_cli_reply = True


async def run_cli_command(
    mc,
    command: str,
    *,
    firmware_ver_code: int | None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Send one CLI command to the companion and return its reply text.

    Raises CliUnsupportedError when the firmware predates the command,
    CliCommandError when the radio answers with an error frame, and
    asyncio.TimeoutError when nothing comes back in time.
    """
    global _pending_reply

    if not firmware_supports_cli(firmware_ver_code):
        raise CliUnsupportedError(
            "This firmware does not accept CLI commands over the companion link "
            f"(protocol version {firmware_ver_code}, needs {MIN_FIRMWARE_VER_CODE} or newer)."
        )
    text = command.strip()
    if not text:
        raise ValueError("Empty command")
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_COMMAND_BYTES:
        raise ValueError(f"Command longer than {MAX_COMMAND_BYTES} bytes")

    install_cli_reply_adapter(mc)

    loop = asyncio.get_running_loop()
    reply: asyncio.Future[str] = loop.create_future()
    _pending_reply = reply

    def _on_error(event) -> None:
        if not reply.done():
            reply.set_exception(CliCommandError(str(getattr(event, "payload", {}) or "error")))

    error_sub = mc.subscribe(EventType.ERROR, _on_error)
    try:
        # Fire-and-forget at the library level: the reply is not an event it
        # knows, so the adapter above resolves the future instead.
        await mc.commands.send(bytes([CMD_RUN_CLI_COMMAND]) + encoded)
        return await asyncio.wait_for(reply, timeout)
    finally:
        try:
            error_sub.unsubscribe()
        except Exception:
            pass
        if _pending_reply is reply:
            _pending_reply = None
