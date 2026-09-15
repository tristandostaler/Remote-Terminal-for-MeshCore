"""RX silence watchdog: tell a quiet mesh from a radio that stopped pushing frames."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from meshcore import EventType

from app.radio import RadioManager
from app.services.rx_watchdog import REBOOT_COOLDOWN_SECONDS, RxSilenceWatchdog


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _stats(recv: int | None):
    event = MagicMock()
    if recv is None:
        event.type = EventType.ERROR
        event.payload = {"error": "unsupported"}
    else:
        event.type = EventType.STATS_PACKETS
        event.payload = {"recv": recv}
    return event


def _fake_manager(clock: FakeClock, recv_samples: list[int | None]):
    """A radio manager stub with a real RX clock and a scripted packet counter."""
    rm = MagicMock()
    rm.is_connected = True
    rm.is_setup_complete = True
    tracker = RadioManager()
    tracker.mark_rx_baseline(clock())
    rm.note_rx_log_frame = tracker.note_rx_log_frame
    rm.mark_rx_baseline = tracker.mark_rx_baseline
    rm.seconds_since_last_rx_log_frame = tracker.seconds_since_last_rx_log_frame
    type(rm).last_rx_log_frame_at = property(lambda self: tracker.last_rx_log_frame_at)

    mc = MagicMock()
    mc.commands.get_stats_packets = AsyncMock(side_effect=[_stats(v) for v in recv_samples])
    mc.commands.reboot = AsyncMock(return_value=MagicMock(type=EventType.OK))

    @asynccontextmanager
    async def _op(name, **kwargs):
        yield mc

    rm.radio_operation = _op
    rm.reconnect_and_prepare = AsyncMock(return_value=True)
    rm.disconnect = AsyncMock()
    rm._mc = mc
    rm._tracker = tracker
    return rm


@pytest.fixture(autouse=True)
def _timeout_300():
    with (
        patch("app.services.rx_watchdog.settings.rx_silence_timeout_seconds", 300),
        patch("app.websocket.broadcast_error"),
        patch("app.websocket.broadcast_success"),
    ):
        yield


class TestSilenceClock:
    def test_measures_from_setup_until_first_frame(self):
        rm = RadioManager()
        assert rm.seconds_since_last_rx_log_frame(10.0) is None
        rm.mark_rx_baseline(10.0)
        assert rm.seconds_since_last_rx_log_frame(70.0) == 60.0
        rm.note_rx_log_frame(65.0)
        assert rm.seconds_since_last_rx_log_frame(70.0) == 5.0

    def test_new_connection_restarts_the_clock(self):
        rm = RadioManager()
        rm.note_rx_log_frame(10.0)
        rm.mark_rx_baseline(100.0)
        assert rm.seconds_since_last_rx_log_frame(130.0) == 30.0


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_no_probe_while_frames_flow(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [])
        wd = RxSilenceWatchdog(rm, clock=clock)

        clock.advance(299)
        await wd.check()

        rm._mc.commands.get_stats_packets.assert_not_awaited()
        assert wd.state.incident_open is False

    @pytest.mark.asyncio
    async def test_quiet_mesh_probes_but_never_escalates(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 500, 500])
        wd = RxSilenceWatchdog(rm, clock=clock)

        for _ in range(3):
            clock.advance(300)
            await wd.check()

        assert rm._mc.commands.get_stats_packets.await_count == 3
        rm.reconnect_and_prepare.assert_not_awaited()
        rm._mc.commands.reboot.assert_not_awaited()
        assert wd.state.escalation == 0

    @pytest.mark.asyncio
    async def test_probe_waits_a_full_timeout_between_samples(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 500])
        wd = RxSilenceWatchdog(rm, clock=clock)

        clock.advance(300)
        await wd.check()
        clock.advance(30)
        await wd.check()

        assert rm._mc.commands.get_stats_packets.await_count == 1

    @pytest.mark.asyncio
    async def test_radio_heard_packets_we_never_saw_triggers_reconnect_then_reboot(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 512, 600, 640])
        wd = RxSilenceWatchdog(rm, clock=clock)

        clock.advance(300)
        await wd.check()  # first sample: recv=500
        clock.advance(300)
        await wd.check()  # recv=512 while we saw nothing -> reconnect
        rm.reconnect_and_prepare.assert_awaited_once()
        rm._mc.commands.reboot.assert_not_awaited()
        assert wd.state.escalation == 1

        # The reconnect restarted the silence clock; the incident stays open.
        rm._tracker.mark_rx_baseline(clock())
        await wd.check()
        assert wd.state.incident_open is True

        clock.advance(300)
        await wd.check()  # fresh baseline sample after reconnect: recv=600
        clock.advance(300)
        await wd.check()  # recv=640, still nothing pushed -> reboot
        rm._mc.commands.reboot.assert_awaited_once()
        # Once before the reconnect (the link looked connected), once after the reboot.
        assert rm.disconnect.await_count == 2
        assert wd.state.escalation == 2
        assert wd.state.last_reboot_at == clock()

    @pytest.mark.asyncio
    async def test_reboot_cooldown_falls_back_to_reconnect(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [1, 2, 3, 4])
        wd = RxSilenceWatchdog(rm, clock=clock)
        wd.state.escalation = 1
        wd.state.incident_open = True
        wd.state.last_reboot_at = clock() - REBOOT_COOLDOWN_SECONDS / 2

        clock.advance(300)
        await wd.check()
        clock.advance(300)
        await wd.check()

        rm._mc.commands.reboot.assert_not_awaited()
        rm.reconnect_and_prepare.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_frame_arrival_closes_the_incident(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 500])
        wd = RxSilenceWatchdog(rm, clock=clock)

        clock.advance(300)
        await wd.check()
        assert wd.state.incident_open is True

        rm.note_rx_log_frame(clock())
        clock.advance(10)
        with (
            patch("app.websocket.broadcast_success") as success,
            patch("app.websocket.broadcast_error") as error,
        ):
            await wd.check()

        assert wd.state.incident_open is False
        # Lulls and recoveries are log-only; only a confirmed stall toasts.
        success.assert_not_called()
        error.assert_not_called()

    @pytest.mark.asyncio
    async def test_probe_on_a_quiet_mesh_does_not_toast(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 500])
        wd = RxSilenceWatchdog(rm, clock=clock)

        with patch("app.websocket.broadcast_error") as error:
            clock.advance(300)
            await wd.check()
            clock.advance(300)
            await wd.check()

        error.assert_not_called()
        assert wd.state.warned is True

    @pytest.mark.asyncio
    async def test_firmware_without_packet_stats_only_probes(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [None, None, None])
        wd = RxSilenceWatchdog(rm, clock=clock)

        for _ in range(3):
            clock.advance(300)
            await wd.check()

        assert rm._mc.commands.get_stats_packets.await_count == 3
        rm.reconnect_and_prepare.assert_not_awaited()
        assert wd.state.stats_unsupported_warned is True

    @pytest.mark.asyncio
    async def test_disabled_by_zero_timeout(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500])
        wd = RxSilenceWatchdog(rm, clock=clock)
        clock.advance(10_000)

        with patch("app.services.rx_watchdog.settings.rx_silence_timeout_seconds", 0):
            await wd.check()

        rm._mc.commands.get_stats_packets.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_disconnected_keeps_open_incident_but_clears_idle_state(self):
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 510])
        wd = RxSilenceWatchdog(rm, clock=clock)
        clock.advance(300)
        await wd.check()
        clock.advance(300)
        await wd.check()
        assert wd.state.escalation == 1

        rm.is_connected = False
        await wd.check()
        assert wd.state.escalation == 1
        assert wd.state.incident_open is True


class TestWatchdogAcrossLinkDrops:
    @pytest.mark.asyncio
    async def test_external_disconnect_drops_the_stale_counter_sample(self):
        """Packets heard while the link was down must not count as 'never pushed'."""
        clock = FakeClock()
        rm = _fake_manager(clock, [500, 503])
        wd = RxSilenceWatchdog(rm, clock=clock)

        clock.advance(300)
        await wd.check()  # quiet mesh, first sample recv=500
        assert wd.state.incident_open is True

        rm.is_connected = False  # WiFi blip: library or keepalive drops the link
        await wd.check()
        assert wd.state.last_probe is None

        rm.is_connected = True
        rm._tracker.mark_rx_baseline(clock())
        clock.advance(300)
        await wd.check()  # recv=503: three packets heard during the outage

        rm.reconnect_and_prepare.assert_not_awaited()
        assert wd.state.escalation == 0


class TestTcpKeepalive:
    def test_sets_keepalive_options_on_the_live_socket(self):
        rm = RadioManager()
        sock = MagicMock()
        transport = MagicMock()
        transport.get_extra_info.return_value = sock
        mc = MagicMock()
        mc.connection_manager.connection.transport = transport
        rm._meshcore = mc

        with patch("app.radio.settings.tcp_keepalive_idle_seconds", 30):
            assert rm.apply_tcp_keepalive() is True

        import socket

        calls = {(c.args[0], c.args[1]): c.args[2] for c in sock.setsockopt.call_args_list}
        assert calls[(socket.SOL_SOCKET, socket.SO_KEEPALIVE)] == 1
        if hasattr(socket, "TCP_KEEPIDLE"):
            assert calls[(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE)] == 30
            assert calls[(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL)] == 10
            assert calls[(socket.IPPROTO_TCP, socket.TCP_KEEPCNT)] == 3

    def test_no_transport_is_a_noop(self):
        rm = RadioManager()
        rm._meshcore = MagicMock()
        rm._meshcore.connection_manager.connection.transport = None
        assert rm.apply_tcp_keepalive() is False

    def test_disabled_by_zero_idle(self):
        rm = RadioManager()
        rm._meshcore = MagicMock()
        with patch("app.radio.settings.tcp_keepalive_idle_seconds", 0):
            assert rm.apply_tcp_keepalive() is False
