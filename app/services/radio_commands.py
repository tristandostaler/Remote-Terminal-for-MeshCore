import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from meshcore import EventType

from app.services.device_query import query_device_info
from app.services.repeat_mode import (
    RepeatFreqRange,
    describe_allowed_repeat_freqs,
    extract_repeat_flag,
    freq_allowed_for_repeat,
)

logger = logging.getLogger(__name__)


class RadioCommandServiceError(RuntimeError):
    """Base error for reusable radio command workflows."""


class PathHashModeUnsupportedError(RadioCommandServiceError):
    """Raised when firmware does not support path hash mode updates."""


class RepeatModeUnsupportedError(RadioCommandServiceError):
    """Raised when firmware does not support companion repeat mode."""


class RepeatFrequencyNotAllowedError(RadioCommandServiceError):
    """Raised when repeat is requested on a frequency the radio won't relay on."""


class RadioCommandRejectedError(RadioCommandServiceError):
    """Raised when the radio reports an error for a command."""


class KeystoreRefreshError(RadioCommandServiceError):
    """Raised when server-side keystore refresh fails after import."""


async def _read_back_repeat_flag(mc, *, requested: bool) -> bool:
    """Re-read the repeat flag from the radio, falling back to what was asked for."""
    try:
        device_query = await query_device_info(mc)
    except Exception as exc:
        logger.debug("Failed to read back repeat mode: %s", exc)
        return requested

    flag = extract_repeat_flag(device_query.payload, device_query.raw_frame)
    if flag is None:
        return requested
    if flag != requested:
        logger.warning("Radio reports repeat=%s after requesting repeat=%s", flag, requested)
    return flag


async def apply_radio_config_update(
    mc,
    update,
    *,
    path_hash_mode_supported: bool,
    set_path_hash_mode: Callable[[int], None],
    sync_radio_time_fn: Callable[[Any], Awaitable[Any]],
    repeat_supported: bool = False,
    repeat_enabled: bool = False,
    allowed_repeat_freqs: Sequence[RepeatFreqRange] = (),
    set_repeat_enabled: Callable[[bool], None] | None = None,
) -> None:
    """Apply a validated radio-config update to the connected radio."""
    if update.repeat_enabled is not None and not repeat_supported:
        raise RepeatModeUnsupportedError("Firmware does not support repeat mode")
    if update.advert_location_source is not None:
        advert_loc_policy = 0 if update.advert_location_source == "off" else 1
        logger.info(
            "Setting advert location policy to %s",
            update.advert_location_source,
        )
        result = await mc.commands.set_advert_loc_policy(advert_loc_policy)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(
                f"Failed to set advert location policy: {result.payload}"
            )

    if update.multi_acks_enabled is not None:
        multi_acks = 1 if update.multi_acks_enabled else 0
        logger.info("Setting multi ACKs to %d", multi_acks)
        result = await mc.commands.set_multi_acks(multi_acks)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(f"Failed to set multi ACKs: {result.payload}")

    if update.telemetry_mode_base is not None:
        logger.info("Setting telemetry_mode_base to %d", update.telemetry_mode_base)
        result = await mc.commands.set_telemetry_mode_base(update.telemetry_mode_base)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(
                f"Failed to set telemetry mode (base): {result.payload}"
            )

    if update.telemetry_mode_loc is not None:
        logger.info("Setting telemetry_mode_loc to %d", update.telemetry_mode_loc)
        result = await mc.commands.set_telemetry_mode_loc(update.telemetry_mode_loc)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(
                f"Failed to set telemetry mode (location): {result.payload}"
            )

    if update.telemetry_mode_env is not None:
        logger.info("Setting telemetry_mode_env to %d", update.telemetry_mode_env)
        result = await mc.commands.set_telemetry_mode_env(update.telemetry_mode_env)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(
                f"Failed to set telemetry mode (environment): {result.payload}"
            )

    if update.name is not None:
        logger.info("Setting radio name to %s", update.name)
        await mc.commands.set_name(update.name)

    if update.lat is not None or update.lon is not None:
        current_info = mc.self_info
        lat = update.lat if update.lat is not None else current_info.get("adv_lat", 0.0)
        lon = update.lon if update.lon is not None else current_info.get("adv_lon", 0.0)
        logger.info("Setting radio coordinates to %f, %f", lat, lon)
        await mc.commands.set_coords(lat=lat, lon=lon)

    if update.tx_power is not None:
        logger.info("Setting TX power to %d dBm", update.tx_power)
        await mc.commands.set_tx_power(val=update.tx_power)

    # Repeat mode rides along on SET_RADIO_PARAMS, so a repeat-only change still
    # re-sends the current radio parameters, and a radio-params change always
    # carries the current repeat flag so it is never cleared by omission.
    target_repeat = repeat_enabled if update.repeat_enabled is None else update.repeat_enabled
    if update.radio is not None or update.repeat_enabled is not None:
        current_info = mc.self_info or {}
        raw_freq = update.radio.freq if update.radio is not None else current_info.get("radio_freq")
        raw_bw = update.radio.bw if update.radio is not None else current_info.get("radio_bw")
        raw_sf = update.radio.sf if update.radio is not None else current_info.get("radio_sf")
        raw_cr = update.radio.cr if update.radio is not None else current_info.get("radio_cr")
        if raw_freq is None or raw_bw is None or raw_sf is None or raw_cr is None:
            raise RadioCommandRejectedError(
                "Radio parameters are not available; cannot update repeat mode"
            )
        freq = float(raw_freq)
        bw = float(raw_bw)
        sf = int(raw_sf)
        cr = int(raw_cr)

        allowed = list(allowed_repeat_freqs)
        if target_repeat and not freq_allowed_for_repeat(freq, allowed):
            raise RepeatFrequencyNotAllowedError(
                f"Repeat mode requires one of: {describe_allowed_repeat_freqs(allowed)} "
                f"(current frequency: {freq:g} MHz)"
            )

        repeat_byte = int(target_repeat) if repeat_supported else None
        logger.info(
            "Setting radio params: freq=%f MHz, bw=%f kHz, sf=%d, cr=%d, repeat=%s",
            freq,
            bw,
            sf,
            cr,
            repeat_byte,
        )
        radio_kwargs: dict[str, Any] = {"freq": freq, "bw": bw, "sf": sf, "cr": cr}
        if repeat_byte is not None:
            radio_kwargs["repeat"] = repeat_byte
        result = await mc.commands.set_radio(**radio_kwargs)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(f"Failed to set radio params: {result.payload}")
        if repeat_byte is not None and set_repeat_enabled is not None:
            confirmed = target_repeat
            if update.repeat_enabled is not None:
                confirmed = await _read_back_repeat_flag(mc, requested=target_repeat)
            set_repeat_enabled(confirmed)

    if update.path_hash_mode is not None:
        if not path_hash_mode_supported:
            raise PathHashModeUnsupportedError("Firmware does not support path hash mode setting")

        logger.info("Setting path hash mode to %d", update.path_hash_mode)
        result = await mc.commands.set_path_hash_mode(update.path_hash_mode)
        if result is not None and result.type == EventType.ERROR:
            raise RadioCommandRejectedError(f"Failed to set path hash mode: {result.payload}")
        set_path_hash_mode(update.path_hash_mode)

    await sync_radio_time_fn(mc)

    # Commands like set_name() write to flash but don't update cached self_info.
    # send_appstart() forces a fresh SELF_INFO so the response reflects changes.
    await mc.commands.send_appstart()


async def import_private_key_and_refresh_keystore(
    mc,
    key_bytes: bytes,
    *,
    export_and_store_private_key_fn: Callable[[Any], Awaitable[bool]],
) -> None:
    """Import a private key and refresh the in-memory keystore immediately."""
    result = await mc.commands.import_private_key(key_bytes)
    if result.type == EventType.ERROR:
        raise RadioCommandRejectedError(f"Failed to import private key: {result.payload}")

    keystore_refreshed = await export_and_store_private_key_fn(mc)
    if not keystore_refreshed:
        logger.warning("Keystore refresh failed after import, retrying once")
        keystore_refreshed = await export_and_store_private_key_fn(mc)

    if not keystore_refreshed:
        raise KeystoreRefreshError(
            "Private key imported on radio, but server-side keystore refresh failed. "
            "Reconnect to apply the new key for DM decryption."
        )
