"""Periodic owner-info refresh for repeaters and room servers.

Walks every recently heard repeater (and every room with a stored credential),
logs in, sends the guest-accessible owner-info binary request, and saves the
answer in ``contact_owner``. See ``app/repository/contact_owner.py``.

Paced on purpose: **one node per check**, a check every few minutes. Each
refresh is a login plus a request over LoRa, so a mesh of a few hundred
repeaters spreads across a day or two instead of costing an hour of airtime in
one go -- and a weekly interval still covers well over a thousand nodes.
"""

import asyncio
import logging
import time

from app.radio import RadioOperationBusyError
from app.repository import AppSettingsRepository, ContactRepository
from app.repository.contact_owner import ContactOwnerRepository
from app.services.radio_runtime import radio_runtime as radio_manager

logger = logging.getLogger(__name__)

# Selectable refresh intervals in days; 0 disables the sweep.
OWNER_INFO_REFRESH_OPTIONS_DAYS = (0, 1, 3, 7, 14, 30)
# How often the loop wakes, and so the pace: at most one node per wake.
CHECK_INTERVAL_SECONDS = 300

_task: asyncio.Task | None = None


async def refresh_owner_info(
    public_key: str, credential: str | None, *, blocking: bool = False
) -> str:
    """Log in to one node, ask for its owner info, and record the outcome.

    Returns the recorded status (``ok``, ``no_reply``, ``login_failed``,
    ``error``). Raises ``RadioOperationBusyError`` without recording anything,
    so a busy radio never counts as the node failing to answer. The sweep
    passes ``blocking=False`` and tries again later; a button press waits.
    """
    from app.routers.server_control import (
        prepare_authenticated_contact_connection,
        request_repeater_owner_info,
    )

    contact = await ContactRepository.get_by_key(public_key)
    if contact is None:
        return "error"

    status = "error"
    owner: dict[str, str | None] | None = None
    try:
        async with radio_manager.radio_operation(
            f"owner_info_sweep:{public_key[:12]}",
            blocking=blocking,
            pause_polling=True,
            suspend_auto_fetch=True,
        ) as mc:
            # "" is a guest login; a room's stored credential may be a real
            # password. Never gate on truthiness (see app/repository/room_poll.py).
            login = await prepare_authenticated_contact_connection(
                mc, contact, credential if credential is not None else ""
            )
            if not login.authenticated:
                status = "login_failed" if login.status == "rejected" else "no_reply"
            else:
                owner = await request_repeater_owner_info(mc, contact)
                status = "ok" if owner is not None else "no_reply"
    except RadioOperationBusyError:
        raise
    except Exception as exc:
        logger.warning("Owner-info refresh failed for %s: %s", public_key[:12], exc)
        status = "error"

    await ContactOwnerRepository.record_fetch(
        public_key,
        status=status,
        owner_info=(owner or {}).get("owner_info"),
        firmware_version=(owner or {}).get("firmware_version"),
    )
    logger.info(
        "Owner-info refresh for %s (%s): %s",
        contact.name or public_key[:12],
        public_key[:12],
        status,
    )
    return status


async def run_owner_info_sweep_step(*, now: int | None = None) -> str | None:
    """Refresh the single most overdue node, if any is due. Returns its key."""
    days = (await AppSettingsRepository.get()).owner_info_refresh_days
    if days <= 0:
        return None
    now = int(time.time()) if now is None else now
    interval = days * 86400
    target = await ContactOwnerRepository.next_sweep_target(
        stale_before=now - interval, heard_since=now - interval
    )
    if target is None:
        return None
    public_key, credential = target
    await refresh_owner_info(public_key, credential)
    return public_key


async def _sweep_loop() -> None:
    while True:
        try:
            # The first step waits a full check after start, so a restart loop
            # cannot turn into a burst of logins.
            await asyncio.sleep(CHECK_INTERVAL_SECONDS)
            if not radio_manager.is_connected:
                continue
            await run_owner_info_sweep_step()
        except asyncio.CancelledError:
            logger.info("Owner-info sweep task cancelled")
            break
        except RadioOperationBusyError:
            logger.debug("Skipping owner-info sweep step: radio busy")
        except Exception as exc:
            logger.error("Error in owner-info sweep loop: %s", exc, exc_info=True)


def start_owner_info_sweep() -> None:
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_sweep_loop())
        logger.info("Started owner-info sweep task")


async def stop_owner_info_sweep() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
