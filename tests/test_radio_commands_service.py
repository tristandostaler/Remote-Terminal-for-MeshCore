from unittest.mock import AsyncMock, MagicMock

import pytest
from meshcore import EventType

from app.routers.radio import RadioConfigUpdate, RadioSettings
from app.services.radio_commands import (
    KeystoreRefreshError,
    PathHashModeUnsupportedError,
    RadioCommandRejectedError,
    RepeatFrequencyNotAllowedError,
    RepeatModeUnsupportedError,
    apply_radio_config_update,
    import_private_key_and_refresh_keystore,
)
from app.services.repeat_mode import RepeatFreqRange


def _radio_result(event_type=EventType.OK, payload=None):
    result = MagicMock()
    result.type = event_type
    result.payload = payload or {}
    return result


def _mock_meshcore_with_info():
    mc = MagicMock()
    mc.self_info = {
        "adv_lat": 10.0,
        "adv_lon": 20.0,
        "radio_freq": 869.525,
        "radio_bw": 250.0,
        "radio_sf": 11,
        "radio_cr": 5,
    }
    mc.commands = MagicMock()
    mc.commands.set_name = AsyncMock()
    mc.commands.set_coords = AsyncMock()
    mc.commands.set_tx_power = AsyncMock()
    mc.commands.set_radio = AsyncMock()
    mc.commands.set_path_hash_mode = AsyncMock(return_value=_radio_result())
    mc.commands.set_advert_loc_policy = AsyncMock(return_value=_radio_result())
    mc.commands.set_multi_acks = AsyncMock(return_value=_radio_result())
    mc.commands.send_appstart = AsyncMock()
    mc.commands.import_private_key = AsyncMock(return_value=_radio_result())
    return mc


class TestApplyRadioConfigUpdate:
    @pytest.mark.asyncio
    async def test_updates_requested_fields_and_refreshes_info(self):
        mc = _mock_meshcore_with_info()
        sync_radio_time_fn = AsyncMock()
        set_path_hash_mode = MagicMock()
        update = RadioConfigUpdate(
            name="NodeUpdated",
            lat=1.23,
            tx_power=17,
            radio=RadioSettings(freq=910.525, bw=62.5, sf=7, cr=5),
            path_hash_mode=1,
        )

        await apply_radio_config_update(
            mc,
            update,
            path_hash_mode_supported=True,
            set_path_hash_mode=set_path_hash_mode,
            sync_radio_time_fn=sync_radio_time_fn,
        )

        mc.commands.set_name.assert_awaited_once_with("NodeUpdated")
        mc.commands.set_coords.assert_awaited_once_with(lat=1.23, lon=20.0)
        mc.commands.set_tx_power.assert_awaited_once_with(val=17)
        mc.commands.set_radio.assert_awaited_once_with(freq=910.525, bw=62.5, sf=7, cr=5)
        mc.commands.set_path_hash_mode.assert_awaited_once_with(1)
        set_path_hash_mode.assert_called_once_with(1)
        sync_radio_time_fn.assert_awaited_once_with(mc)
        mc.commands.send_appstart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_advert_location_source(self):
        mc = _mock_meshcore_with_info()

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(advert_location_source="current"),
            path_hash_mode_supported=True,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
        )

        mc.commands.set_advert_loc_policy.assert_awaited_once_with(1)
        mc.commands.send_appstart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_updates_multi_acks_enabled(self):
        mc = _mock_meshcore_with_info()

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(multi_acks_enabled=True),
            path_hash_mode_supported=True,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
        )

        mc.commands.set_multi_acks.assert_awaited_once_with(1)
        mc.commands.send_appstart.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_raises_when_radio_rejects_multi_acks(self):
        mc = _mock_meshcore_with_info()
        mc.commands.set_multi_acks = AsyncMock(
            return_value=_radio_result(EventType.ERROR, {"error": "nope"})
        )

        with pytest.raises(RadioCommandRejectedError):
            await apply_radio_config_update(
                mc,
                RadioConfigUpdate(multi_acks_enabled=False),
                path_hash_mode_supported=True,
                set_path_hash_mode=MagicMock(),
                sync_radio_time_fn=AsyncMock(),
            )

        mc.commands.send_appstart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_when_radio_rejects_advert_location_source(self):
        mc = _mock_meshcore_with_info()
        mc.commands.set_advert_loc_policy = AsyncMock(
            return_value=_radio_result(EventType.ERROR, {"error": "nope"})
        )

        with pytest.raises(RadioCommandRejectedError):
            await apply_radio_config_update(
                mc,
                RadioConfigUpdate(advert_location_source="off"),
                path_hash_mode_supported=True,
                set_path_hash_mode=MagicMock(),
                sync_radio_time_fn=AsyncMock(),
            )

        mc.commands.send_appstart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_unsupported_path_hash_mode(self):
        mc = _mock_meshcore_with_info()
        update = RadioConfigUpdate(path_hash_mode=1)

        with pytest.raises(PathHashModeUnsupportedError):
            await apply_radio_config_update(
                mc,
                update,
                path_hash_mode_supported=False,
                set_path_hash_mode=MagicMock(),
                sync_radio_time_fn=AsyncMock(),
            )

        mc.commands.set_path_hash_mode.assert_not_awaited()
        mc.commands.send_appstart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_raises_when_radio_rejects_path_hash_mode(self):
        mc = _mock_meshcore_with_info()
        mc.commands.set_path_hash_mode = AsyncMock(
            return_value=_radio_result(EventType.ERROR, {"error": "nope"})
        )
        update = RadioConfigUpdate(path_hash_mode=1)
        set_path_hash_mode = MagicMock()

        with pytest.raises(RadioCommandRejectedError):
            await apply_radio_config_update(
                mc,
                update,
                path_hash_mode_supported=True,
                set_path_hash_mode=set_path_hash_mode,
                sync_radio_time_fn=AsyncMock(),
            )

        set_path_hash_mode.assert_not_called()
        mc.commands.send_appstart.assert_not_awaited()


class TestRepeatMode:
    ALLOWED = [RepeatFreqRange(869.0, 869.0)]

    @pytest.mark.asyncio
    async def test_repeat_only_update_resends_current_radio_params(self):
        mc = _mock_meshcore_with_info()
        mc.self_info["radio_freq"] = 869.0
        set_repeat_enabled = MagicMock()

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(repeat_enabled=True),
            path_hash_mode_supported=False,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
            repeat_supported=True,
            repeat_enabled=False,
            allowed_repeat_freqs=self.ALLOWED,
            set_repeat_enabled=set_repeat_enabled,
        )

        mc.commands.set_radio.assert_awaited_once_with(freq=869.0, bw=250.0, sf=11, cr=5, repeat=1)
        set_repeat_enabled.assert_called_once_with(True)

    @pytest.mark.asyncio
    async def test_radio_params_update_carries_current_repeat_flag(self):
        mc = _mock_meshcore_with_info()

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(radio=RadioSettings(freq=433.0, bw=250.0, sf=11, cr=5)),
            path_hash_mode_supported=False,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
            repeat_supported=True,
            repeat_enabled=True,
            allowed_repeat_freqs=[RepeatFreqRange(433.0, 433.0)],
            set_repeat_enabled=MagicMock(),
        )

        mc.commands.set_radio.assert_awaited_once_with(freq=433.0, bw=250.0, sf=11, cr=5, repeat=1)

    @pytest.mark.asyncio
    async def test_omits_repeat_byte_when_unsupported(self):
        mc = _mock_meshcore_with_info()

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(radio=RadioSettings(freq=910.525, bw=62.5, sf=7, cr=5)),
            path_hash_mode_supported=False,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
            repeat_supported=False,
        )

        mc.commands.set_radio.assert_awaited_once_with(freq=910.525, bw=62.5, sf=7, cr=5)

    @pytest.mark.asyncio
    async def test_rejects_repeat_update_when_unsupported(self):
        mc = _mock_meshcore_with_info()

        with pytest.raises(RepeatModeUnsupportedError):
            await apply_radio_config_update(
                mc,
                RadioConfigUpdate(repeat_enabled=True),
                path_hash_mode_supported=False,
                set_path_hash_mode=MagicMock(),
                sync_radio_time_fn=AsyncMock(),
                repeat_supported=False,
            )

        mc.commands.set_radio.assert_not_awaited()
        mc.commands.send_appstart.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rejects_repeat_on_disallowed_frequency(self):
        mc = _mock_meshcore_with_info()

        with pytest.raises(RepeatFrequencyNotAllowedError) as exc_info:
            await apply_radio_config_update(
                mc,
                RadioConfigUpdate(
                    radio=RadioSettings(freq=910.525, bw=62.5, sf=7, cr=5),
                    repeat_enabled=True,
                ),
                path_hash_mode_supported=False,
                set_path_hash_mode=MagicMock(),
                sync_radio_time_fn=AsyncMock(),
                repeat_supported=True,
                allowed_repeat_freqs=self.ALLOWED,
                set_repeat_enabled=MagicMock(),
            )

        assert "869 MHz" in str(exc_info.value)
        mc.commands.set_radio.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_allows_turning_repeat_off_on_any_frequency(self):
        mc = _mock_meshcore_with_info()
        mc.self_info["radio_freq"] = 910.525
        set_repeat_enabled = MagicMock()

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(repeat_enabled=False),
            path_hash_mode_supported=False,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
            repeat_supported=True,
            repeat_enabled=True,
            allowed_repeat_freqs=self.ALLOWED,
            set_repeat_enabled=set_repeat_enabled,
        )

        mc.commands.set_radio.assert_awaited_once_with(
            freq=910.525, bw=250.0, sf=11, cr=5, repeat=0
        )
        set_repeat_enabled.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_uses_radio_reported_flag_after_toggle(self):
        mc = _mock_meshcore_with_info()
        mc.self_info["radio_freq"] = 869.0
        set_repeat_enabled = MagicMock()
        device_info = MagicMock()
        device_info.payload = {"fw ver": 9, "repeat": False}
        mc.commands.send_device_query = AsyncMock(return_value=device_info)

        await apply_radio_config_update(
            mc,
            RadioConfigUpdate(repeat_enabled=True),
            path_hash_mode_supported=False,
            set_path_hash_mode=MagicMock(),
            sync_radio_time_fn=AsyncMock(),
            repeat_supported=True,
            repeat_enabled=False,
            allowed_repeat_freqs=self.ALLOWED,
            set_repeat_enabled=set_repeat_enabled,
        )

        # The radio disagreed with the request, so the reported value wins.
        set_repeat_enabled.assert_called_once_with(False)

    @pytest.mark.asyncio
    async def test_raises_when_radio_rejects_radio_params(self):
        mc = _mock_meshcore_with_info()
        mc.commands.set_radio = AsyncMock(
            return_value=_radio_result(EventType.ERROR, {"error": "nope"})
        )

        with pytest.raises(RadioCommandRejectedError):
            await apply_radio_config_update(
                mc,
                RadioConfigUpdate(radio=RadioSettings(freq=869.0, bw=250.0, sf=11, cr=5)),
                path_hash_mode_supported=False,
                set_path_hash_mode=MagicMock(),
                sync_radio_time_fn=AsyncMock(),
            )

        mc.commands.send_appstart.assert_not_awaited()


class TestImportPrivateKeyAndRefreshKeystore:
    @pytest.mark.asyncio
    async def test_rejects_radio_error(self):
        mc = _mock_meshcore_with_info()
        mc.commands.import_private_key = AsyncMock(
            return_value=_radio_result(EventType.ERROR, {"error": "failed"})
        )
        export_fn = AsyncMock(return_value=True)

        with pytest.raises(RadioCommandRejectedError):
            await import_private_key_and_refresh_keystore(
                mc,
                b"\xaa" * 64,
                export_and_store_private_key_fn=export_fn,
            )

        export_fn.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_keystore_refresh_once(self):
        mc = _mock_meshcore_with_info()
        export_fn = AsyncMock(side_effect=[False, True])

        await import_private_key_and_refresh_keystore(
            mc,
            b"\xaa" * 64,
            export_and_store_private_key_fn=export_fn,
        )

        mc.commands.import_private_key.assert_awaited_once_with(b"\xaa" * 64)
        assert export_fn.await_count == 2

    @pytest.mark.asyncio
    async def test_raises_when_keystore_refresh_fails_twice(self):
        mc = _mock_meshcore_with_info()
        export_fn = AsyncMock(return_value=False)

        with pytest.raises(KeystoreRefreshError):
            await import_private_key_and_refresh_keystore(
                mc,
                b"\xaa" * 64,
                export_and_store_private_key_fn=export_fn,
            )

        assert export_fn.await_count == 2
