"""RX silence watchdog: notice when the radio stops pushing raw frames.

Everything RemoteTerm shows for a channel comes from the raw RX-log frames the
companion pushes over the host link, and nothing in the link reports when that
push stops: commands still get answers, the socket stays open, the connection
monitor sees "connected". The only symptom is silence -- which is also what a
quiet mesh looks like.

The watchdog tells the two apart with the radio's own packet counter. After
``rx_silence_timeout_seconds`` without a raw frame it takes a ``STATS_PACKETS``
sample (which is itself a command exchange, the very thing that un-wedged the
link in the field). If a later sample shows the radio *heard* packets in a
window where we saw none, the push path is wedged and the watchdog escalates:
reconnect the transport first, and if the next confirmed window is still
silent, send the reboot command. A radio that heard nothing is a quiet mesh,
and nothing happens beyond the periodic probe.

Firmware without packet stats cannot confirm either way; the watchdog then
only probes and warns, never escalates.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from meshcore import EventType

from app.config import settings
from app.radio import RadioDisconnectedError, RadioOperationBusyError

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 30
# Do not reboot the radio more often than this, whatever the counters say.
REBOOT_COOLDOWN_SECONDS = 30 * 60


@dataclass
class ProbeSample:
    taken_at: float
    recv: int | None  # None: firmware did not answer with packet stats


@dataclass
class RxWatchdogState:
    """Per-incident state; reset whenever a raw frame arrives."""

    incident_open: bool = False
    warned: bool = False
    stats_unsupported_warned: bool = False
    last_probe: ProbeSample | None = None
    escalation: int = 0  # 0 = probing, 1 = reconnected once, 2 = reboot sent
    last_reboot_at: float | None = None
    log: list[str] = field(default_factory=list)


class RxSilenceWatchdog:
    def __init__(self, radio_manager, *, clock=time.monotonic):
        self._radio_manager = radio_manager
        self._clock = clock
        self.state = RxWatchdogState()

    # -- helpers -------------------------------------------------------------

    @property
    def timeout(self) -> int:
        try:
            return max(0, int(settings.rx_silence_timeout_seconds))
        except (TypeError, ValueError):
            return 0

    def _reset(self) -> None:
        last_reboot = self.state.last_reboot_at
        self.state = RxWatchdogState(last_reboot_at=last_reboot)

    async def _sample_packet_stats(self) -> ProbeSample | None:
        """One STATS_PACKETS exchange; None when the radio was busy or gone."""
        try:
            async with self._radio_manager.radio_operation(
                "rx_watchdog_probe", blocking=False
            ) as mc:
                event = await mc.commands.get_stats_packets()
        except (RadioOperationBusyError, RadioDisconnectedError):
            return None
        except Exception as exc:
            logger.debug("RX watchdog probe failed: %s", exc)
            return None
        recv: int | None = None
        if getattr(event, "type", None) == EventType.STATS_PACKETS:
            value = (event.payload or {}).get("recv")
            if isinstance(value, int):
                recv = value
        return ProbeSample(taken_at=self._clock(), recv=recv)

    # -- main step -----------------------------------------------------------

    async def check(self) -> None:
        """One watchdog tick. Safe to call as often as you like."""
        timeout = self.timeout
        if timeout <= 0:
            return
        rm = self._radio_manager
        if not rm.is_connected or not getattr(rm, "is_setup_complete", False):
            # Keep an open incident across the reconnect we may have caused
            # ourselves, or the escalation ladder would restart from the
            # bottom every time; a clean idle state has nothing to keep. The
            # last counter sample is dropped either way: packets the radio
            # heard while the link was down were never ours to receive, so
            # comparing across the gap would escalate on a healthy radio.
            self.state.last_probe = None
            if not self.state.incident_open:
                self._reset()
            return

        silence = rm.seconds_since_last_rx_log_frame(self._clock())
        if silence is None:
            return

        if silence < timeout:
            if not self.state.incident_open:
                return
            if rm.last_rx_log_frame_at is None:
                # A fresh connection restarted the silence clock; no frame has
                # actually arrived yet, so the incident is still open.
                return
            logger.info(
                "Radio RX-log frames resumed after %s",
                " -> ".join(self.state.log) or "a silent period",
            )
            self._reset()
            return

        # Silent past the threshold. Time for a probe?
        last = self.state.last_probe
        if last is not None and self._clock() - last.taken_at < timeout:
            return

        if not self.state.incident_open:
            self.state.incident_open = True
            self.state.log.append(f"silent {int(silence)}s")

        sample = await self._sample_packet_stats()
        if sample is None:
            return  # radio busy; try again next tick

        if not self.state.warned:
            self.state.warned = True
            logger.warning(
                "No raw RX-log frame from the radio for %ds; probing the link (packet stats: %s)",
                int(silence),
                "unsupported" if sample.recv is None else f"recv={sample.recv}",
            )

        previous = last
        self.state.last_probe = sample

        if sample.recv is None:
            if not self.state.stats_unsupported_warned:
                self.state.stats_unsupported_warned = True
                logger.warning(
                    "This firmware does not report packet stats; the RX watchdog can probe "
                    "the link but cannot tell a quiet mesh from a stalled push path, so it "
                    "will not reconnect or reboot on its own."
                )
            return

        if previous is None or previous.recv is None:
            return  # first confirmed sample of this incident: nothing to compare yet

        heard = sample.recv - previous.recv
        if heard <= 0:
            logger.info(
                "Radio heard no packets either in the last %ds (recv=%d); the mesh is quiet",
                int(sample.taken_at - previous.taken_at),
                sample.recv,
            )
            return

        # The radio heard packets and pushed none of them to us: the push path is wedged.
        await self._escalate(heard)

    async def _escalate(self, heard: int) -> None:
        """Reconnect, then reboot. Log-only: the watchdog never toasts.

        Everything it does is visible in the log and in the connection status
        the UI already shows (the health indicator flips while it reconnects),
        so a toast would only duplicate that.
        """
        now = self._clock()
        if self.state.escalation == 0:
            self.state.escalation = 1
            self.state.log.append(f"radio heard {heard}, reconnecting")
            logger.error(
                "Radio heard %d packet(s) but pushed no RX-log frame to RemoteTerm; "
                "the push path is stalled. Reconnecting the transport.",
                heard,
            )
            self.state.last_probe = None
            await self._reconnect()
            return

        if (
            self.state.last_reboot_at is not None
            and now - self.state.last_reboot_at < REBOOT_COOLDOWN_SECONDS
        ):
            logger.error(
                "Radio still heard %d packet(s) without forwarding any after a reconnect; "
                "reboot cooldown active, will retry the reconnect.",
                heard,
            )
            self.state.last_probe = None
            await self._reconnect()
            return

        self.state.escalation = 2
        self.state.last_reboot_at = now
        self.state.log.append(f"radio heard {heard}, rebooting")
        logger.error(
            "Radio still heard %d packet(s) without forwarding any after a reconnect; "
            "sending the reboot command.",
            heard,
        )
        self.state.last_probe = None
        await self._reboot()

    async def _reconnect(self) -> None:
        """Tear the transport down and bring it back up with full setup.

        The link still *looks* connected (that is the whole problem), and
        ``RadioManager.reconnect`` returns early on a connected radio, so the
        disconnect has to be explicit. Disconnecting leaves ``connection_desired``
        alone, so the connection monitor would also pick it up; we do not wait
        for it.
        """
        rm = self._radio_manager
        try:
            await rm.disconnect()
        except Exception as exc:
            logger.warning("RX watchdog disconnect failed: %s", exc)
        try:
            reconnect = getattr(rm, "reconnect_and_prepare", None)
            if reconnect is None:
                from app.services.radio_lifecycle import reconnect_and_prepare_radio

                await reconnect_and_prepare_radio(rm, broadcast_on_success=True)
            else:
                await reconnect(broadcast_on_success=True)
        except Exception as exc:
            logger.warning("RX watchdog reconnect failed: %s", exc)

    async def _reboot(self) -> None:
        rm = self._radio_manager
        try:
            async with rm.radio_operation("rx_watchdog_reboot", blocking=True) as mc:
                await mc.commands.reboot()
        except Exception as exc:
            logger.warning("RX watchdog reboot command failed: %s", exc)
        # The connection monitor notices the dropped link and reconnects; a
        # transport that survives the reboot (TCP to a WiFi node) is forced.
        try:
            await rm.disconnect()
        except Exception:
            logger.debug("Disconnect after reboot command failed", exc_info=True)


async def rx_watchdog_loop(radio_manager) -> None:
    """Background task: tick the watchdog every CHECK_INTERVAL_SECONDS."""
    watchdog = RxSilenceWatchdog(radio_manager)
    if watchdog.timeout <= 0:
        logger.info("RX silence watchdog disabled (MESHCORE_RX_SILENCE_TIMEOUT_SECONDS=0)")
        return
    logger.info("RX silence watchdog started (timeout: %ds)", watchdog.timeout)
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            await watchdog.check()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Error in RX silence watchdog: %s", exc, exc_info=True)
