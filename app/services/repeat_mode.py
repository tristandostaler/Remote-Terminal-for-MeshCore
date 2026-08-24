"""Companion repeat ("relay") mode helpers.

MeshCore companion firmware (protocol version 9 and up) can relay mesh packets
for other nodes, the same way a dedicated repeater does.  The flag lives in the
DEVICE_INFO frame and is written through the ``SET_RADIO_PARAMS`` command's
trailing repeat byte, so toggling it always re-sends the full radio parameter
set.

Firmware only relays on the shared off-grid frequencies.  Devices that
implement ``GET_ALLOWED_REPEAT_FREQ`` report the permitted ranges themselves;
older ones get the frequencies the official apps hardcode.
"""

import logging
from typing import NamedTuple

from app.services.device_query import DEVICE_INFO_REPEAT_OFFSET

logger = logging.getLogger(__name__)

# Companion protocol version that added the repeat flag to DEVICE_INFO.
FIRMWARE_VER_REPEAT = 9

# Frequency comparisons are done in MHz; firmware resolution is 1 kHz.
FREQ_MATCH_TOLERANCE_MHZ = 0.0005


class RepeatFreqRange(NamedTuple):
    """An inclusive frequency range (MHz) where repeating is permitted."""

    min_mhz: float
    max_mhz: float


# Used when the radio does not implement GET_ALLOWED_REPEAT_FREQ. Mirrors the
# off-grid frequencies the official MeshCore apps allow repeat on.
FALLBACK_ALLOWED_REPEAT_FREQS_MHZ: tuple[RepeatFreqRange, ...] = (
    RepeatFreqRange(433.0, 433.0),
    RepeatFreqRange(869.0, 869.0),
    RepeatFreqRange(918.0, 918.0),
)


def extract_repeat_flag(payload: dict, raw_frame: bytes | None) -> bool | None:
    """Read the repeat flag from a device query, or ``None`` when unsupported."""
    value = payload.get("repeat")
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0

    # Raw-frame fallback: older meshcore libraries (and stale .pyc files) don't
    # decode the repeat byte even when the firmware reports it.
    if raw_frame is None or len(raw_frame) <= DEVICE_INFO_REPEAT_OFFSET:
        return None
    fw_ver = raw_frame[1] if len(raw_frame) > 1 else 0
    if fw_ver < FIRMWARE_VER_REPEAT:
        return None
    return raw_frame[DEVICE_INFO_REPEAT_OFFSET] != 0


def normalize_repeat_freq_mhz(value: int) -> float | None:
    """Convert a firmware-reported frequency to MHz.

    Firmware units have varied between Hz, kHz, and MHz across builds, so the
    magnitude decides: sub-GHz repeat frequencies are unambiguous under these
    thresholds, and 2.4 GHz LoRa stays correct in every unit as well.
    """
    if value <= 0:
        return None
    if value >= 100_000_000:
        return value / 1_000_000
    if value >= 100_000:
        return value / 1_000
    return float(value)


def parse_allowed_repeat_freqs(payload: dict) -> list[RepeatFreqRange]:
    """Normalize an ``ALLOWED_REPEAT_FREQ`` payload into MHz ranges."""
    raw_freqs = payload.get("freqs")
    if not isinstance(raw_freqs, list):
        return []

    ranges: list[RepeatFreqRange] = []
    for entry in raw_freqs:
        if not isinstance(entry, dict):
            continue
        low = entry.get("min")
        high = entry.get("max")
        if not isinstance(low, int) or not isinstance(high, int):
            continue
        min_mhz = normalize_repeat_freq_mhz(low)
        max_mhz = normalize_repeat_freq_mhz(high)
        if min_mhz is None or max_mhz is None:
            continue
        ranges.append(RepeatFreqRange(min(min_mhz, max_mhz), max(min_mhz, max_mhz)))
    return ranges


async def query_allowed_repeat_freqs(mc) -> list[RepeatFreqRange]:
    """Ask the radio which frequencies it will repeat on (best effort).

    Returns the fallback frequency set when the command is unsupported or the
    radio reports nothing usable.
    """
    try:
        event = await mc.commands.get_allowed_repeat_freq()
    except Exception as exc:
        logger.debug("Failed to query allowed repeat frequencies: %s", exc)
        return list(FALLBACK_ALLOWED_REPEAT_FREQS_MHZ)

    payload = event.payload if event is not None and isinstance(event.payload, dict) else {}
    ranges = parse_allowed_repeat_freqs(payload)
    if not ranges:
        logger.debug("Radio reported no allowed repeat frequencies; using defaults")
        return list(FALLBACK_ALLOWED_REPEAT_FREQS_MHZ)
    return ranges


def freq_allowed_for_repeat(freq_mhz: float, allowed: list[RepeatFreqRange]) -> bool:
    """Whether ``freq_mhz`` falls inside one of the permitted repeat ranges."""
    if not allowed:
        return True
    return any(
        (freq_range.min_mhz - FREQ_MATCH_TOLERANCE_MHZ)
        <= freq_mhz
        <= (freq_range.max_mhz + FREQ_MATCH_TOLERANCE_MHZ)
        for freq_range in allowed
    )


def describe_allowed_repeat_freqs(allowed: list[RepeatFreqRange]) -> str:
    """Human-readable frequency list for error messages."""
    parts = [
        f"{freq_range.min_mhz:g} MHz"
        if freq_range.min_mhz == freq_range.max_mhz
        else f"{freq_range.min_mhz:g}-{freq_range.max_mhz:g} MHz"
        for freq_range in allowed
    ]
    return ", ".join(parts)
