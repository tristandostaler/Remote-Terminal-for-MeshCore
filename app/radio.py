import asyncio
import glob
import logging
import platform
import re
import socket
import time
from collections import OrderedDict
from contextlib import asynccontextmanager, nullcontext
from pathlib import Path

from meshcore import MeshCore
from serial.serialutil import SerialException

from app.config import settings
from app.keystore import clear_keys
from app.services.repeat_mode import RepeatFreqRange

logger = logging.getLogger(__name__)
MAX_FRONTEND_RECONNECT_ERROR_BROADCASTS = 3
_SERIAL_PORT_ERROR_RE = re.compile(r"could not open port (?P<port>.+?):")


class RadioOperationError(RuntimeError):
    """Base class for shared radio operation lock errors."""


class RadioOperationBusyError(RadioOperationError):
    """Raised when a non-blocking radio operation cannot acquire the lock."""


class RadioDisconnectedError(RadioOperationError):
    """Raised when the radio disconnects between pre-check and lock acquisition."""


def detect_serial_devices() -> list[str]:
    """Detect available serial devices based on platform."""
    devices: list[str] = []
    system = platform.system()

    if system == "Darwin":
        # macOS: Use /dev/cu.* devices (callout devices, preferred over tty.*)
        patterns = [
            "/dev/cu.usb*",
            "/dev/cu.wchusbserial*",
            "/dev/cu.SLAB_USBtoUART*",
        ]
        for pattern in patterns:
            devices.extend(glob.glob(pattern))
        devices.sort()
    else:
        # Linux: Prefer /dev/serial/by-id/ for persistent naming
        by_id_path = Path("/dev/serial/by-id")
        if by_id_path.is_dir():
            devices.extend(str(p) for p in by_id_path.iterdir())

        # Also check /dev/ttyACM* and /dev/ttyUSB* as fallback
        resolved_paths = set()
        for dev in devices:
            try:
                resolved_paths.add(str(Path(dev).resolve()))
            except OSError:
                pass

        for pattern in ["/dev/ttyACM*", "/dev/ttyUSB*"]:
            for dev in glob.glob(pattern):
                try:
                    if str(Path(dev).resolve()) not in resolved_paths:
                        devices.append(dev)
                except OSError:
                    devices.append(dev)

        devices.sort()

    return devices


def _extract_serial_port_from_error(exc: Exception) -> str | None:
    """Best-effort extraction of a serial port path from a pyserial error."""
    message = str(exc)
    match = _SERIAL_PORT_ERROR_RE.search(message)
    if match:
        return match.group("port")
    return None


def _format_reconnect_failure(exc: Exception) -> tuple[str, str, bool]:
    """Return log message, frontend detail, and whether to log a traceback."""
    if settings.connection_type == "serial":
        if isinstance(exc, RuntimeError) and str(exc).startswith("No MeshCore radio found"):
            message = (
                "Could not find a MeshCore radio on any serial port. "
                "Did the radio get disconnected or change serial ports?"
            )
            return (message, message, False)

        if isinstance(exc, SerialException):
            port = settings.serial_port or _extract_serial_port_from_error(exc) or "the serial port"
            message = (
                f"Could not connect to serial port {port}. "
                "Did the radio get disconnected or change serial ports?"
            )
            return (message, message, False)

    return (f"Reconnection failed: {exc}", str(exc), True)


async def test_serial_device(port: str, baudrate: int, timeout: float = 3.0) -> bool:
    """Test if a MeshCore radio responds on the given serial port."""
    mc = None
    try:
        logger.debug("Testing serial device %s", port)
        mc = await asyncio.wait_for(
            MeshCore.create_serial(port=port, baudrate=baudrate),
            timeout=timeout,
        )

        # Check if we got valid self_info (indicates successful communication)
        if mc.is_connected and mc.self_info:
            logger.debug("Device %s responded with valid self_info", port)
            return True

        return False
    except TimeoutError:
        logger.debug("Device %s timed out", port)
        return False
    except Exception as e:
        logger.debug("Device %s failed: %s", port, e)
        return False
    finally:
        if mc is not None:
            try:
                await mc.disconnect()
            except Exception:
                pass


async def find_radio_port(baudrate: int) -> str | None:
    """Find the first serial port with a responding MeshCore radio."""
    devices = detect_serial_devices()

    if not devices:
        logger.warning("No serial devices found")
        return None

    logger.info("Found %d serial device(s), testing for MeshCore radio...", len(devices))

    for device in devices:
        if await test_serial_device(device, baudrate):
            logger.info("Found MeshCore radio at %s", device)
            return device

    logger.warning("No MeshCore radio found on any serial device")
    return None


class RadioManager:
    """Manages the MeshCore radio connection."""

    def __init__(self):
        self._meshcore: MeshCore | None = None
        self._connection_info: str | None = None
        self._connection_desired: bool = True
        self._reconnect_task: asyncio.Task | None = None
        self._last_connected: bool = False
        self._reconnect_lock: asyncio.Lock | None = None
        self._operation_lock: asyncio.Lock | None = None
        self._setup_lock: asyncio.Lock | None = None
        self._setup_in_progress: bool = False
        self._setup_complete: bool = False
        self._frontend_reconnect_error_broadcasts: int = 0
        self.device_info_loaded: bool = False
        self.max_contacts: int | None = None
        self.device_model: str | None = None
        self.firmware_build: str | None = None
        self.firmware_version: str | None = None
        # Companion protocol version (FIRMWARE_VER_CODE). Gates version-dependent
        # host commands such as the mode-1 unscoped flood-scope frame (ver 12+).
        self.firmware_ver_code: int | None = None
        self.max_channels: int = 40
        self.path_hash_mode: int = 0
        self.path_hash_mode_supported: bool = False
        # Set once this node answers ERR_CODE_UNSUPPORTED_CMD to CMD_SEND_RAW_DATA,
        # so the image and voice transports stop retrying a command this firmware
        # does not have and go straight to their text fallback. Per connection:
        # cleared below, because the node on the other end of the cable can change.
        self.raw_data_unsupported: bool = False
        # Companion repeat ("relay") mode: whether the firmware reports the flag
        # at all, its current value, and the frequencies it will repeat on.
        self.repeat_supported: bool = False
        self.repeat_enabled: bool = False
        self.allowed_repeat_freqs: list[RepeatFreqRange] = []
        self._channel_slot_by_key: OrderedDict[str, int] = OrderedDict()
        self._channel_key_by_slot: dict[int, str] = {}
        self._pending_message_channel_key_by_slot: dict[int, str] = {}
        # Channels pinned in radio slots for the whole session (see
        # ``radio_sync.load_resident_channels``). Unlike the send cache above,
        # a resident slot is never evicted by a send to another channel: the
        # firmware decrypts and queues messages for it, which is the fallback
        # that keeps channel traffic flowing when the raw RX-log push stalls.
        self._resident_channel_slot_by_key: OrderedDict[str, int] = OrderedDict()
        self._resident_channel_key_by_slot: dict[int, str] = {}
        # Monotonic time of the last raw RX-log frame the radio pushed to us,
        # and of the moment the current connection finished setup. The RX
        # silence watchdog compares both against the radio's own packet counters.
        self._last_rx_log_frame_at: float | None = None
        self._rx_baseline_at: float | None = None
        self._rx_watchdog_task: asyncio.Task | None = None

    async def _acquire_operation_lock(
        self,
        name: str,
        *,
        blocking: bool,
    ) -> None:
        """Acquire the shared radio operation lock."""

        if self._operation_lock is None:
            self._operation_lock = asyncio.Lock()

        if not blocking:
            if self._operation_lock.locked():
                raise RadioOperationBusyError(f"Radio is busy (operation: {name})")
            # In single-threaded asyncio the lock cannot be acquired between the
            # check above and the await below (no other coroutine runs until we
            # yield). The await returns immediately for an uncontested lock.
            await self._operation_lock.acquire()
        else:
            await self._operation_lock.acquire()

        logger.debug("Acquired radio operation lock (%s)", name)

    def _release_operation_lock(self, name: str) -> None:
        """Release the shared radio operation lock."""
        if self._operation_lock and self._operation_lock.locked():
            self._operation_lock.release()
            logger.debug("Released radio operation lock (%s)", name)
        else:
            logger.error("Attempted to release unlocked radio operation lock (%s)", name)

    def _reset_connected_runtime_state(self) -> None:
        """Clear cached runtime state after a transport teardown completes."""
        self._setup_complete = False
        self.device_info_loaded = False
        self.max_contacts = None
        self.device_model = None
        self.firmware_build = None
        self.firmware_version = None
        self.firmware_ver_code = None
        self.max_channels = 40
        self.path_hash_mode = 0
        self.path_hash_mode_supported = False
        self.raw_data_unsupported = False
        self.repeat_supported = False
        self.repeat_enabled = False
        self.allowed_repeat_freqs = []
        self.reset_channel_send_cache()
        self.clear_pending_message_channel_slots()
        self.clear_resident_channels()
        self._last_rx_log_frame_at = None
        self._rx_baseline_at = None

    # -- RX-log frame tracking (fed by event_handlers.on_rx_log_data) --------

    def note_rx_log_frame(self, now: float | None = None) -> None:
        """Record that the radio just pushed a raw RX-log frame."""
        self._last_rx_log_frame_at = time.monotonic() if now is None else now

    def mark_rx_baseline(self, now: float | None = None) -> None:
        """Start the silence clock now (called when a connection finishes setup)."""
        self._rx_baseline_at = time.monotonic() if now is None else now

    @property
    def last_rx_log_frame_at(self) -> float | None:
        return self._last_rx_log_frame_at

    def seconds_since_last_rx_log_frame(self, now: float | None = None) -> float | None:
        """Seconds since the last raw frame, or since setup if none arrived yet.

        Returns None when neither has happened on this connection.
        """
        reference = self._last_rx_log_frame_at
        if reference is None or (
            self._rx_baseline_at is not None and self._rx_baseline_at > reference
        ):
            reference = self._rx_baseline_at
        if reference is None:
            return None
        current = time.monotonic() if now is None else now
        return max(0.0, current - reference)

    # -- Transport keepalive -------------------------------------------------

    def apply_tcp_keepalive(self) -> bool:
        """Enable TCP keepalive on the live socket to a WiFi companion.

        meshcore-py opens a plain asyncio connection, and a WiFi companion that
        drops off the network mid-session leaves that socket half-open: no data
        means no error, so the link reports connected forever. Keepalive turns a
        dead peer into a connection_lost within ``idle + 3 * interval`` seconds,
        which is what lets the connection monitor reconnect. Re-applied on every
        post-connect setup because the library replaces the transport when it
        reconnects on its own. Returns True when the options were set.
        """
        idle = int(settings.tcp_keepalive_idle_seconds or 0)
        if idle <= 0:
            return False
        mc = self._meshcore
        if mc is None:
            return False
        connection = getattr(getattr(mc, "connection_manager", None), "connection", None)
        transport = getattr(connection, "transport", None)
        if transport is None:
            return False
        try:
            sock = transport.get_extra_info("socket")
        except Exception:
            return False
        if sock is None:
            return False
        interval = max(1, idle // 3)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            for option_name, value in (
                ("TCP_KEEPIDLE", idle),  # Linux
                ("TCP_KEEPALIVE", idle),  # macOS spelling of the idle time
                ("TCP_KEEPINTVL", interval),
                ("TCP_KEEPCNT", 3),
            ):
                option = getattr(socket, option_name, None)
                if option is not None:
                    sock.setsockopt(socket.IPPROTO_TCP, option, value)
        except OSError as exc:
            logger.debug("Could not enable TCP keepalive: %s", exc)
            return False
        logger.debug("TCP keepalive enabled (idle=%ds, interval=%ds)", idle, interval)
        return True

    @asynccontextmanager
    async def radio_operation(
        self,
        name: str,
        *,
        pause_polling: bool = False,
        suspend_auto_fetch: bool = False,
        blocking: bool = True,
    ):
        """Acquire shared radio lock and optionally pause polling / auto-fetch.

        After acquiring the lock, resolves the current MeshCore instance and
        yields it.  Callers get a fresh reference via ``async with ... as mc:``,
        avoiding stale-reference bugs when a reconnect swaps ``_meshcore``
        between the pre-check and the lock acquisition.

        Args:
            name: Human-readable operation name for logs/errors.
            pause_polling: Pause fallback message polling while held.
            suspend_auto_fetch: Stop MeshCore auto message fetching while held.
            blocking: If False, fail immediately when lock is held.

        Raises:
            RadioDisconnectedError: If the radio disconnected before the lock
                was acquired (``_meshcore`` is ``None``).
        """
        await self._acquire_operation_lock(name, blocking=blocking)

        mc = self._meshcore
        if mc is None:
            self._release_operation_lock(name)
            raise RadioDisconnectedError("Radio disconnected")

        poll_context = nullcontext()
        if pause_polling:
            from app.radio_sync import pause_polling as pause_polling_context

            poll_context = pause_polling_context()

        auto_fetch_paused = False

        try:
            async with poll_context:
                if suspend_auto_fetch:
                    await mc.stop_auto_message_fetching()
                    auto_fetch_paused = True
                yield mc
        finally:
            try:
                if auto_fetch_paused:
                    try:
                        await mc.start_auto_message_fetching()
                    except Exception as e:
                        logger.warning("Failed to restart auto message fetching (%s): %s", name, e)
            finally:
                self._release_operation_lock(name)

    async def post_connect_setup(self) -> None:
        """Run shared post-connection orchestration after transport setup succeeds."""
        from app.services.radio_lifecycle import run_post_connect_setup

        await run_post_connect_setup(self)

    def reset_channel_send_cache(self) -> None:
        """Forget any session-local channel-slot reuse state."""
        self._channel_slot_by_key.clear()
        self._channel_key_by_slot.clear()

    def remember_pending_message_channel_slot(self, channel_key: str, slot: int) -> None:
        """Remember a channel key for later queued-message recovery."""
        self._pending_message_channel_key_by_slot[slot] = channel_key.upper()

    def get_pending_message_channel_key(self, slot: int) -> str | None:
        """Return the last remembered channel key for a radio slot."""
        return self._pending_message_channel_key_by_slot.get(slot)

    def clear_pending_message_channel_slots(self) -> None:
        """Drop any queued-message recovery slot metadata."""
        self._pending_message_channel_key_by_slot.clear()

    # -- Resident channels ---------------------------------------------------

    def set_resident_channels(self, slots_by_key: dict[str, int]) -> None:
        """Replace the resident channel set (key -> radio slot), in priority order."""
        self._resident_channel_slot_by_key = OrderedDict(
            (key.upper(), int(slot)) for key, slot in slots_by_key.items()
        )
        self._resident_channel_key_by_slot = {
            slot: key for key, slot in self._resident_channel_slot_by_key.items()
        }
        # A slot that just became resident can no longer be a send scratch slot.
        for slot in list(self._resident_channel_key_by_slot):
            cached_key = self._channel_key_by_slot.pop(slot, None)
            if cached_key is not None:
                self._channel_slot_by_key.pop(cached_key, None)
        for key in list(self._resident_channel_slot_by_key):
            self.invalidate_cached_channel_slot(key)

    def add_resident_channel(self, channel_key: str, slot: int) -> None:
        """Pin one more channel in a radio slot."""
        normalized_key = channel_key.upper()
        previous_slot = self._resident_channel_slot_by_key.pop(normalized_key, None)
        if previous_slot is not None:
            self._resident_channel_key_by_slot.pop(previous_slot, None)
        displaced = self._resident_channel_key_by_slot.get(slot)
        if displaced is not None:
            self._resident_channel_slot_by_key.pop(displaced, None)
        self._resident_channel_slot_by_key[normalized_key] = slot
        self._resident_channel_key_by_slot[slot] = normalized_key
        cached_key = self._channel_key_by_slot.pop(slot, None)
        if cached_key is not None:
            self._channel_slot_by_key.pop(cached_key, None)
        self.invalidate_cached_channel_slot(normalized_key)

    def remove_resident_channel(self, channel_key: str) -> int | None:
        """Unpin a channel; returns the slot it occupied, if any."""
        slot = self._resident_channel_slot_by_key.pop(channel_key.upper(), None)
        if slot is not None:
            self._resident_channel_key_by_slot.pop(slot, None)
        return slot

    def clear_resident_channels(self) -> None:
        self._resident_channel_slot_by_key.clear()
        self._resident_channel_key_by_slot.clear()

    def get_resident_channel_slot(self, channel_key: str) -> int | None:
        return self._resident_channel_slot_by_key.get(channel_key.upper())

    def resident_channel_key_for_slot(self, slot: int) -> str | None:
        return self._resident_channel_key_by_slot.get(slot)

    def get_resident_channels_snapshot(self) -> list[tuple[str, int]]:
        """Resident channels as (key, slot) pairs in priority order."""
        return list(self._resident_channel_slot_by_key.items())

    def resident_channel_count(self) -> int:
        return len(self._resident_channel_slot_by_key)

    def first_free_resident_slot(self) -> int | None:
        """Lowest slot not held by a resident channel, leaving one scratch slot free."""
        capacity = self.get_channel_send_cache_capacity()
        if capacity <= 1 or len(self._resident_channel_slot_by_key) >= capacity - 1:
            return None
        for slot in range(capacity):
            if slot not in self._resident_channel_key_by_slot:
                return slot
        return None

    def channel_slot_reuse_enabled(self) -> bool:
        """Return whether this transport can safely reuse cached channel slots."""
        if settings.force_channel_slot_reconfigure:
            return False
        if self._connection_info:
            return not self._connection_info.startswith("TCP:")
        return settings.connection_type != "tcp"

    def get_channel_send_cache_capacity(self) -> int:
        """Return the app-managed channel cache capacity for the current session."""
        try:
            return max(1, int(self.max_channels))
        except (TypeError, ValueError):
            return 1

    def get_cached_channel_slot(self, channel_key: str) -> int | None:
        """Return the radio slot a channel is known to be loaded in, if any.

        A resident channel counts as loaded on transports that reuse slots; on
        the others every send reconfigures anyway (see plan_channel_send_slot).
        """
        normalized_key = channel_key.upper()
        if self.channel_slot_reuse_enabled():
            resident_slot = self._resident_channel_slot_by_key.get(normalized_key)
            if resident_slot is not None:
                return resident_slot
        return self._channel_slot_by_key.get(normalized_key)

    def channel_key_for_slot(self, slot: int) -> str | None:
        """Reverse of :meth:`get_cached_channel_slot`.

        Inbound GRP_DATA frames identify their channel by radio SLOT, not by
        key -- ``RESP_CODE_CHANNEL_DATA_RECV`` carries a one-byte channel index
        and nothing else -- so receiving one means mapping that index back to a
        channel we know.

        Consults ``_channel_key_by_slot`` first (the maintained reverse index,
        rather than a linear scan of its forward twin) and then the queued-message
        recovery map, exactly the way ``_resolve_channel_for_pending_message``
        does for inbound channel TEXT.

        Both are consulted because the first is gated on
        :meth:`channel_slot_reuse_enabled`: on a TCP connection, or when
        ``force_channel_slot_reconfigure`` is set, ``note_channel_slot_loaded``
        returns early and the reuse maps stay EMPTY for the whole session. An
        earlier version of this method read only the forward reuse map, so on
        those transports every inbound image was dropped as "no channel loaded"
        while sending kept working -- sends do not need the cache, they just
        reconfigure a slot each time.

        Returns None only when nothing in this process has ever associated the
        slot with a channel; the caller then asks the radio directly.
        """
        resident = self._resident_channel_key_by_slot.get(slot)
        if resident is not None:
            return resident
        cached = self._channel_key_by_slot.get(slot)
        if cached is not None:
            return cached
        return self._pending_message_channel_key_by_slot.get(slot)

    def plan_channel_send_slot(
        self,
        channel_key: str,
        *,
        preferred_slot: int = 0,
    ) -> tuple[int, bool, str | None]:
        """Choose a radio slot for a channel send.

        Returns `(slot, needs_configure, evicted_channel_key)`.

        A resident channel always sends from its own pinned slot. On transports
        that reuse slots it is already loaded, so no configure is needed; on the
        others (TCP, forced reconfigure) the same key is rewritten into the same
        slot, which is harmless and keeps the "always reconfigure" guarantee.
        Non-resident channels never touch a resident slot: they share the
        remaining scratch slots under the existing LRU policy.
        """
        normalized_key = channel_key.upper()
        resident_slot = self._resident_channel_slot_by_key.get(normalized_key)
        if resident_slot is not None:
            return resident_slot, not self.channel_slot_reuse_enabled(), None

        capacity = self.get_channel_send_cache_capacity()

        if not self.channel_slot_reuse_enabled():
            if preferred_slot in self._resident_channel_key_by_slot:
                preferred_slot = self._find_first_free_channel_slot(capacity, preferred_slot)
            return preferred_slot, True, None

        cached_slot = self._channel_slot_by_key.get(normalized_key)
        if cached_slot is not None:
            return cached_slot, False, None

        scratch_capacity = capacity - len(self._resident_channel_key_by_slot)
        if scratch_capacity <= 0:
            # Every slot is pinned (should not happen: the resident set leaves
            # one scratch slot free). Borrow the lowest-priority resident slot.
            evicted_key, slot = next(reversed(self._resident_channel_slot_by_key.items()))
            self.remove_resident_channel(evicted_key)
            return slot, True, evicted_key

        if len(self._channel_slot_by_key) < scratch_capacity:
            slot = self._find_first_free_channel_slot(capacity, preferred_slot)
            return slot, True, None

        evicted_key, slot = next(iter(self._channel_slot_by_key.items()))
        return slot, True, evicted_key

    def note_channel_slot_loaded(self, channel_key: str, slot: int) -> None:
        """Record that a channel is now resident in the given radio slot."""
        if not self.channel_slot_reuse_enabled():
            return

        normalized_key = channel_key.upper()
        resident_key = self._resident_channel_key_by_slot.get(slot)
        if resident_key is not None:
            if resident_key == normalized_key:
                return  # Already pinned there; nothing to cache.
            # Something overwrote a pinned slot; it is not resident any more.
            self.remove_resident_channel(resident_key)
        previous_slot = self._channel_slot_by_key.pop(normalized_key, None)
        if previous_slot is not None and previous_slot != slot:
            self._channel_key_by_slot.pop(previous_slot, None)

        displaced_key = self._channel_key_by_slot.get(slot)
        if displaced_key is not None and displaced_key != normalized_key:
            self._channel_slot_by_key.pop(displaced_key, None)

        self._channel_key_by_slot[slot] = normalized_key
        self._channel_slot_by_key[normalized_key] = slot

    def note_channel_slot_used(self, channel_key: str) -> None:
        """Refresh LRU order for a previously loaded channel slot."""
        if not self.channel_slot_reuse_enabled():
            return

        normalized_key = channel_key.upper()
        slot = self._channel_slot_by_key.get(normalized_key)
        if slot is None:
            return
        self._channel_slot_by_key.move_to_end(normalized_key)
        self._channel_key_by_slot[slot] = normalized_key

    def invalidate_cached_channel_slot(self, channel_key: str) -> None:
        """Drop any cached slot assignment for a channel key."""
        normalized_key = channel_key.upper()
        slot = self._channel_slot_by_key.pop(normalized_key, None)
        if slot is None:
            return
        if self._channel_key_by_slot.get(slot) == normalized_key:
            self._channel_key_by_slot.pop(slot, None)

    def get_channel_send_cache_snapshot(self) -> list[tuple[str, int]]:
        """Return the current channel send cache contents in LRU order."""
        return list(self._channel_slot_by_key.items())

    def _find_first_free_channel_slot(self, capacity: int, preferred_slot: int) -> int:
        """Pick the first unclaimed app-managed slot, preferring the requested slot."""
        taken = self._channel_key_by_slot.keys() | self._resident_channel_key_by_slot.keys()
        if preferred_slot < capacity and preferred_slot not in taken:
            return preferred_slot

        for slot in range(capacity):
            if slot not in taken:
                return slot

        return preferred_slot

    @property
    def meshcore(self) -> MeshCore | None:
        return self._meshcore

    @property
    def connection_info(self) -> str | None:
        return self._connection_info

    @property
    def is_connected(self) -> bool:
        return self._meshcore is not None and self._meshcore.is_connected

    @property
    def is_reconnecting(self) -> bool:
        return self._reconnect_lock is not None and self._reconnect_lock.locked()

    @property
    def is_setup_in_progress(self) -> bool:
        return self._setup_in_progress

    @property
    def is_setup_complete(self) -> bool:
        return self._setup_complete

    @property
    def connection_desired(self) -> bool:
        return self._connection_desired

    def resume_connection(self) -> None:
        """Allow connection monitor and manual reconnects to establish transport again."""
        self._connection_desired = True

    async def pause_connection(self) -> None:
        """Stop automatic reconnect attempts and tear down any current transport."""
        self._connection_desired = False
        self._last_connected = False
        await self.disconnect()

    def _reset_reconnect_error_broadcasts(self) -> None:
        self._frontend_reconnect_error_broadcasts = 0

    def _broadcast_reconnect_error_if_needed(self, details: str) -> None:
        from app.websocket import broadcast_error

        self._frontend_reconnect_error_broadcasts += 1
        if self._frontend_reconnect_error_broadcasts > MAX_FRONTEND_RECONNECT_ERROR_BROADCASTS:
            return

        if self._frontend_reconnect_error_broadcasts == MAX_FRONTEND_RECONNECT_ERROR_BROADCASTS:
            details = f"{details} Further reconnect failures will be logged only until a connection succeeds."

        broadcast_error("Reconnection failed", details)

    async def _disable_meshcore_auto_reconnect(self, mc: MeshCore) -> None:
        """Disable library-managed reconnects so manual teardown fully releases transport."""
        connection_manager = getattr(mc, "connection_manager", None)
        if connection_manager is None:
            return

        if hasattr(connection_manager, "auto_reconnect"):
            connection_manager.auto_reconnect = False

        reconnect_task = getattr(connection_manager, "_reconnect_task", None)
        if reconnect_task is None or not isinstance(reconnect_task, asyncio.Task | asyncio.Future):
            return

        reconnect_task.cancel()
        try:
            await reconnect_task
        except asyncio.CancelledError:
            pass
        finally:
            connection_manager._reconnect_task = None

    async def connect(self) -> None:
        """Connect to the radio using the configured transport."""
        if self._meshcore is not None:
            await self.disconnect()

        connection_type = settings.connection_type
        if connection_type == "tcp":
            await self._connect_tcp()
        elif connection_type == "ble":
            await self._connect_ble()
        else:
            await self._connect_serial()

    async def _connect_serial(self) -> None:
        """Connect to the radio over serial."""
        port = settings.serial_port

        # Auto-detect if no port specified
        if not port:
            logger.info("No serial port specified, auto-detecting...")
            port = await find_radio_port(settings.serial_baudrate)
            if not port:
                raise RuntimeError("No MeshCore radio found. Please specify MESHCORE_SERIAL_PORT.")

        logger.debug(
            "Connecting to radio at %s (baud %d)",
            port,
            settings.serial_baudrate,
        )
        self._meshcore = await MeshCore.create_serial(
            port=port,
            baudrate=settings.serial_baudrate,
            auto_reconnect=True,
            max_reconnect_attempts=10,
        )
        self._connection_info = f"Serial: {port}"
        self._last_connected = True
        self._setup_complete = False
        logger.debug("Serial connection established")

    async def _connect_tcp(self) -> None:
        """Connect to the radio over TCP."""
        host = settings.tcp_host
        port = settings.tcp_port

        logger.debug("Connecting to radio at %s:%d (TCP)", host, port)
        self._meshcore = await MeshCore.create_tcp(
            host=host,
            port=port,
            auto_reconnect=True,
            max_reconnect_attempts=10,
        )
        self._connection_info = f"TCP: {host}:{port}"
        self._last_connected = True
        self._setup_complete = False
        self.apply_tcp_keepalive()
        logger.debug("TCP connection established")

    async def _connect_ble(self) -> None:
        """Connect to the radio over BLE."""
        address = settings.ble_address
        pin = settings.ble_pin

        logger.debug("Connecting to radio at %s (BLE)", address)
        self._meshcore = await MeshCore.create_ble(
            address=address,
            pin=pin,
            auto_reconnect=True,
            max_reconnect_attempts=15,
        )
        self._connection_info = f"BLE: {address}"
        self._last_connected = True
        self._setup_complete = False
        logger.debug("BLE connection established")

    async def disconnect(self) -> None:
        """Disconnect from the radio."""
        from app.radio_sync import stop_background_contact_reconciliation

        clear_keys()
        self._reset_reconnect_error_broadcasts()
        if self._meshcore is None:
            return

        await stop_background_contact_reconciliation()
        await self._acquire_operation_lock("disconnect", blocking=True)
        try:
            mc = self._meshcore
            if mc is None:
                return

            logger.debug("Disconnecting from radio")
            await self._disable_meshcore_auto_reconnect(mc)
            try:
                await mc.disconnect()
            finally:
                await self._disable_meshcore_auto_reconnect(mc)

            if self._meshcore is mc:
                self._meshcore = None
            self._reset_connected_runtime_state()
            logger.debug("Radio disconnected")
        finally:
            self._release_operation_lock("disconnect")

    async def reconnect(self, *, broadcast_on_success: bool = True) -> bool:
        """Attempt to reconnect to the radio.

        Returns True if reconnection was successful, False otherwise.
        Uses a lock to prevent concurrent reconnection attempts.
        """
        from app.websocket import broadcast_health

        # Lazily initialize lock (can't create in __init__ before event loop exists)
        if self._reconnect_lock is None:
            self._reconnect_lock = asyncio.Lock()

        async with self._reconnect_lock:
            if not self._connection_desired:
                logger.info("Reconnect skipped because connection is paused by operator")
                return False

            # If we became connected while waiting for the lock (another
            # reconnect succeeded ahead of us), skip the redundant attempt.
            if self.is_connected:
                logger.debug("Already connected after acquiring lock, skipping reconnect")
                return True

            logger.info("Attempting to reconnect to radio...")

            try:
                # Disconnect if we have a stale connection
                if self._meshcore is not None:
                    try:
                        await self.disconnect()
                    except Exception:
                        pass

                # Try to connect (will auto-detect if no port specified)
                await self.connect()

                if not self._connection_desired:
                    logger.info("Reconnect completed after pause request; disconnecting transport")
                    await self.disconnect()
                    return False

                if self.is_connected:
                    logger.info("Radio reconnected successfully at %s", self._connection_info)
                    self._reset_reconnect_error_broadcasts()
                    if broadcast_on_success:
                        broadcast_health(True, self._connection_info)
                    return True
                else:
                    logger.warning("Reconnection failed: not connected after connect()")
                    return False

            except Exception as e:
                log_message, frontend_detail, include_traceback = _format_reconnect_failure(e)
                logger.warning(log_message, exc_info=include_traceback)
                self._broadcast_reconnect_error_if_needed(frontend_detail)
                return False

    async def start_connection_monitor(self) -> None:
        """Start background task to monitor connection and auto-reconnect."""
        from app.services.radio_lifecycle import connection_monitor_loop

        if self._reconnect_task is not None:
            return

        self._reconnect_task = asyncio.create_task(connection_monitor_loop(self))
        logger.info("Radio connection monitor started")

        from app.services.rx_watchdog import rx_watchdog_loop

        if self._rx_watchdog_task is None:
            self._rx_watchdog_task = asyncio.create_task(rx_watchdog_loop(self))

    async def stop_connection_monitor(self) -> None:
        """Stop the connection monitor task."""
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except asyncio.CancelledError:
                pass
            self._reconnect_task = None
            logger.info("Radio connection monitor stopped")
        if self._rx_watchdog_task is not None:
            self._rx_watchdog_task.cancel()
            try:
                await self._rx_watchdog_task
            except asyncio.CancelledError:
                pass
            self._rx_watchdog_task = None


radio_manager = RadioManager()
