"""Local-radio stats sampling.

A single 60s loop fetches core, radio, and packet stats from the connected
radio in one radio-lock acquisition.  Each noise-floor reading is persisted
as a side effect (``noise_floor_samples``), so the statistics chart survives
a restart and can be asked for a window wider than a day.

After each sample the loop:
1. Broadcasts a WS ``health`` frame so frontend dashboards refresh.
2. Dispatches a ``broadcast_health_fanout`` event carrying the full stats
   snapshot plus radio identity, so fanout modules (e.g. HA MQTT) can
   publish sensor state without a second radio poll.

Consumers:
- GET /api/health      → get_latest_radio_stats()  (battery, uptime, etc.)
- GET /api/statistics  → get_noise_floor_history()  (windowed noise-floor chart)
- Fanout on_health     → _build_fanout_payload()    (identity + stats)
"""

import asyncio
import logging
import time
from typing import Any

from meshcore import EventType

from app.radio import RadioDisconnectedError, RadioOperationBusyError
from app.repository.noise_floor import NoiseFloorRepository
from app.services.radio_runtime import radio_runtime as radio_manager

logger = logging.getLogger(__name__)

STATS_SAMPLE_INTERVAL_SECONDS = 60

# Old samples are dropped on a slow cadence rather than every minute; the
# retention horizon is a year, so being an hour late costs nothing.
NOISE_FLOOR_PRUNE_INTERVAL_SECONDS = 6 * 3600

_stats_task: asyncio.Task | None = None
_latest_stats: dict[str, Any] = {}
_last_noise_floor_prune: int = 0


async def _record_noise_floor(now: int, noise_floor: int) -> None:
    """Persist one reading, occasionally trimming the retention tail.

    A failed write must never take down the sampling loop — the loop also
    drives the health broadcast and the fanout dispatch, and a missing chart
    point is the cheapest thing here to lose.
    """
    global _last_noise_floor_prune

    try:
        await NoiseFloorRepository.record(now, noise_floor)
    except Exception:
        logger.debug("Failed to persist noise floor sample", exc_info=True)
        return

    if now - _last_noise_floor_prune < NOISE_FLOOR_PRUNE_INTERVAL_SECONDS:
        return
    _last_noise_floor_prune = now
    try:
        deleted = await NoiseFloorRepository.prune()
        if deleted:
            logger.info("Pruned %d noise floor samples past the retention horizon", deleted)
    except Exception:
        logger.debug("Failed to prune noise floor samples", exc_info=True)


async def _sample_all_stats() -> dict[str, Any]:
    """Fetch core, radio, and packet stats in one radio operation.

    Returns the snapshot dict (may be empty if the radio is disconnected or
    all commands errored).
    """
    if not radio_manager.is_connected:
        return {}

    try:
        async with radio_manager.radio_operation("radio_stats_sample", blocking=False) as mc:
            core_event = await mc.commands.get_stats_core()
            radio_event = await mc.commands.get_stats_radio()
            packet_event = await mc.commands.get_stats_packets()
    except (RadioDisconnectedError, RadioOperationBusyError):
        return {}
    except Exception as exc:
        logger.debug("Radio stats sampling failed: %s", exc)
        return {}

    now = int(time.time())
    snapshot: dict[str, Any] = {"timestamp": now}

    if getattr(core_event, "type", None) == EventType.STATS_CORE:
        snapshot.update(core_event.payload)

    if getattr(radio_event, "type", None) == EventType.STATS_RADIO:
        snapshot.update(radio_event.payload)
        noise_floor = radio_event.payload.get("noise_floor")
        if isinstance(noise_floor, int):
            await _record_noise_floor(now, noise_floor)

    if getattr(packet_event, "type", None) == EventType.STATS_PACKETS:
        snapshot["packets"] = packet_event.payload

    has_any_data = len(snapshot) > 1
    return snapshot if has_any_data else {}


def _build_fanout_payload(stats: dict[str, Any]) -> dict:
    """Build the health fanout payload from a stats snapshot + radio identity.

    Includes radio identity (public_key, name), connection state, and the
    full stats snapshot so fanout modules can publish rich sensor data
    without a second radio poll.
    """
    mc = radio_manager.meshcore
    self_info = mc.self_info if mc else None

    payload: dict = {
        "connected": radio_manager.is_connected,
        "connection_info": radio_manager.connection_info,
        "public_key": (self_info.get("public_key") or None) if self_info else None,
        "name": (self_info.get("name") or None) if self_info else None,
    }

    if stats:
        payload["noise_floor_dbm"] = stats.get("noise_floor")
        payload["battery_mv"] = stats.get("battery_mv")
        payload["uptime_secs"] = stats.get("uptime_secs")
        payload["last_rssi"] = stats.get("last_rssi")
        payload["last_snr"] = stats.get("last_snr")
        payload["tx_air_secs"] = stats.get("tx_air_secs")
        payload["rx_air_secs"] = stats.get("rx_air_secs")
        packets = stats.get("packets") or {}
        payload["packets_recv"] = packets.get("recv")
        payload["packets_sent"] = packets.get("sent")
        payload["flood_tx"] = packets.get("flood_tx")
        payload["direct_tx"] = packets.get("direct_tx")
        payload["flood_rx"] = packets.get("flood_rx")
        payload["direct_rx"] = packets.get("direct_rx")

    return payload


async def _stats_sampling_loop() -> None:
    global _latest_stats
    while True:
        try:
            snapshot = await _sample_all_stats()
            if snapshot:
                _latest_stats = snapshot
            elif not radio_manager.is_connected:
                _latest_stats = {}
            from app.websocket import broadcast_health

            broadcast_health(radio_manager.is_connected, radio_manager.connection_info)

            # Dispatch enriched health snapshot to fanout modules
            from app.fanout.manager import fanout_manager

            await fanout_manager.broadcast_health_fanout(_build_fanout_payload(snapshot))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Radio stats sampling loop error")

        try:
            await asyncio.sleep(STATS_SAMPLE_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise


# ── Public API ────────────────────────────────────────────────────────────


async def start_radio_stats_sampling() -> None:
    """Start the periodic radio stats background task."""
    global _stats_task
    if _stats_task is not None and not _stats_task.done():
        return
    _stats_task = asyncio.create_task(_stats_sampling_loop())


async def stop_radio_stats_sampling() -> None:
    """Stop the periodic radio stats background task."""
    global _stats_task
    if _stats_task is None:
        return
    if not _stats_task.done():
        _stats_task.cancel()
        try:
            await _stats_task
        except asyncio.CancelledError:
            pass
    _stats_task = None


async def get_noise_floor_history(cutoff: int | None) -> dict:
    """Return the stored noise-floor series since ``cutoff`` (``None`` = all).

    Bucketing happens in SQL (see ``NoiseFloorRepository.history``) so a year
    of minute-resolution samples still arrives as a chart-sized series.
    """
    history = await NoiseFloorRepository.history(cutoff)
    history["sample_interval_seconds"] = STATS_SAMPLE_INTERVAL_SECONDS
    return history


def get_latest_radio_stats() -> dict[str, Any]:
    """Return the most recent radio stats snapshot (for health endpoint)."""
    return dict(_latest_stats)
