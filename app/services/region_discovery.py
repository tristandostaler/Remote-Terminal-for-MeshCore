"""Periodic region discovery.

Sweeps nearby repeaters with the guest anon regions request (the same primitive
behind Settings › Discover Regions) and merges any newly reported region names
into ``app_settings.known_regions``. New names trigger the usual message region
backfill so already-stored traffic gets labelled.
"""

import asyncio
import logging
import time

from app.radio import RadioOperationBusyError
from app.region_scope import normalize_region_scope
from app.repository import AppSettingsRepository, ContactRepository
from app.services.radio_runtime import radio_runtime as radio_manager

logger = logging.getLogger(__name__)

# Selectable sweep intervals in hours; 0 disables the feature.
AUTO_DISCOVER_REGIONS_OPTIONS_HOURS = (0, 6, 12, 24, 72, 168)
# How often the loop wakes to see whether a sweep is due.
CHECK_INTERVAL_SECONDS = 300
# Repeaters asked per sweep (most recently heard first).
SWEEP_MAX_REPEATERS = 8

_task: asyncio.Task | None = None
_last_run_monotonic: float | None = None


def merge_new_regions(existing: list[str], discovered: list[str]) -> list[str]:
    """Return ``discovered`` names not already in ``existing``.

    Names are compared case-insensitively without a leading ``#`` and returned in
    the stored form (no ``#``). The ``*`` wildcard and blanks are dropped.
    """
    seen = {normalize_region_scope(name).lower() for name in existing}
    added: list[str] = []
    for raw in discovered:
        name = (raw or "").strip()
        if name.startswith("#"):
            name = name[1:].strip()
        if not name or name == "*":
            continue
        key = normalize_region_scope(name).lower()
        if key in seen:
            continue
        seen.add(key)
        added.append(name)
    return added


async def run_region_discovery_sweep() -> list[str]:
    """Sweep repeaters once and persist new regions. Returns the names added."""
    from app.routers.repeaters import request_anon_region_names
    from app.services.messages import backfill_message_regions

    targets = await ContactRepository.get_repeaters_by_recent(limit=SWEEP_MAX_REPEATERS)
    if not targets:
        return []

    discovered: list[str] = []
    async with radio_manager.radio_operation(
        "auto_discover_regions",
        pause_polling=True,
        suspend_auto_fetch=True,
        blocking=False,
    ) as mc:
        for contact in targets:
            discovered.extend(await request_anon_region_names(mc, contact) or [])

    settings = await AppSettingsRepository.get()
    added = merge_new_regions(settings.known_regions, discovered)
    if not added:
        return []

    updated = await AppSettingsRepository.update(known_regions=[*settings.known_regions, *added])
    logger.info("Auto-discovered %d new region(s): %s", len(added), ", ".join(added))
    asyncio.create_task(backfill_message_regions(updated.known_regions))
    return added


async def _discovery_loop() -> None:
    global _last_run_monotonic
    while True:
        try:
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            if not radio_manager.is_connected:
                continue
            hours = (await AppSettingsRepository.get()).auto_discover_regions_hours
            if hours <= 0:
                continue
            now = time.monotonic()
            # First sweep waits one full interval after start, so a restart loop
            # cannot hammer the mesh.
            if _last_run_monotonic is None:
                _last_run_monotonic = now
                continue
            if now - _last_run_monotonic < hours * 3600:
                continue
            _last_run_monotonic = now
            await run_region_discovery_sweep()
        except asyncio.CancelledError:
            logger.info("Region auto-discovery task cancelled")
            break
        except RadioOperationBusyError:
            logger.debug("Skipping region auto-discovery: radio busy")
        except Exception as exc:
            logger.error("Error in region auto-discovery loop: %s", exc, exc_info=True)


def start_region_auto_discovery() -> None:
    global _task, _last_run_monotonic
    if _task is None or _task.done():
        _last_run_monotonic = None
        _task = asyncio.create_task(_discovery_loop())
        logger.info("Started region auto-discovery task")


async def stop_region_auto_discovery() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
