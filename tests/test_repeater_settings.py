"""Tests for the repeater settings catalog (validation, parsing, schema endpoint).

These cover the pure logic only -- no radio, no database. The CLI round trips
for reading and writing live in ``test_repeater_routes.py``.
"""

import pytest

from app.routers.repeaters import repeater_settings_schema
from app.services.repeater_settings import (
    REPEATER_SETTINGS,
    SETTING_GROUPS,
    SettingValueError,
    classify_set_reply,
    format_value,
    get_setting,
    parse_get_reply,
)


class TestCatalog:
    def test_every_setting_belongs_to_a_declared_group(self):
        group_keys = {group.key for group in SETTING_GROUPS}
        for setting in REPEATER_SETTINGS:
            assert setting.group in group_keys, setting.key

    def test_keys_and_cli_keys_are_unique(self):
        keys = [setting.key for setting in REPEATER_SETTINGS]
        cli_keys = [setting.cli_key for setting in REPEATER_SETTINGS]
        assert len(set(keys)) == len(keys)
        assert len(set(cli_keys)) == len(cli_keys)

    def test_commands_are_plain_firmware_syntax(self):
        setting = get_setting("flood_max")
        assert setting.get_command == "get flood.max"
        assert setting.set_command("3") == "set flood.max 3"

    def test_timing_keys_match_the_firmware_spelling(self):
        # The firmware's CLI keys are not consistent about dots: it is
        # `direct.txdelay`, not `direct.tx.delay`. A wrong spelling here is
        # answered "??" and locks the field as unsupported on every firmware.
        assert get_setting("rx_delay").get_command == "get rxdelay"
        assert get_setting("tx_delay").get_command == "get txdelay"
        assert get_setting("direct_tx_delay").set_command("2.5") == "set direct.txdelay 2.5"

    def test_gain_keys_use_the_firmware_spelling(self):
        # `radio.rxgain` must be asked for before `radio` would match it; the
        # firmware orders its branches for that, we only have to spell it right.
        assert get_setting("rx_boosted_gain").set_command("on") == "set radio.rxgain on"
        assert get_setting("fem_rx_gain").get_command == "get radio.fem.rxgain"
        assert get_setting("fem_tx_gain").get_command == "get radio.fem.txgain"

    def test_the_admin_password_is_a_bare_command(self):
        # There is no `set password` in the firmware: the new password is the
        # tail of a top-level `password` command, and `set password` is
        # answered "unknown config", which is how this field was broken.
        password = get_setting("password")
        assert password.set_command("hunter2") == "password hunter2"
        assert password.command_hint == "password"
        assert get_setting("flood_max").command_hint == "set flood.max"
        assert get_setting("role").command_hint == "get role"

    def test_every_catalog_key_is_one_the_firmware_handles(self):
        # Mirrors handleGetCmd/handleSetCmd in the firmware's CommonCLI.cpp
        # (plus the repeater-only telemetry modes). A key missing here is a
        # field that locks itself on every repeater.
        firmware_keys = {
            "name", "lat", "lon", "owner.info",
            "radio", "tx", "af", "dutycycle", "repeat", "cad",
            "radio.rxgain", "radio.fem.rxgain", "radio.fem.txgain",
            "flood.max", "flood.max.advert", "flood.max.unscoped",
            "advert.interval", "flood.advert.interval",
            "password", "guest.password", "allow.read.only",
            "telemetry.mode.base", "telemetry.mode.loc", "telemetry.mode.env",
            "bridge.type", "bridge.enabled", "bridge.source", "bridge.delay",
            "bridge.baud", "bridge.channel", "bridge.secret",
            "rxdelay", "txdelay", "direct.txdelay", "int.thresh",
            "agc.reset.interval", "multi.acks", "path.hash.mode", "loop.detect",
            "adc.multiplier", "extra.sf",
            "role", "public.key", "bootloader.ver",
            "pwrmgt.source", "pwrmgt.bootreason", "pwrmgt.bootmv",
        }  # fmt: skip
        assert {setting.cli_key for setting in REPEATER_SETTINGS} == firmware_keys

    def test_read_only_facts_cannot_be_written(self):
        for key in ("role", "public_key", "bridge_type", "boot_reason"):
            setting = get_setting(key)
            assert setting.writable is False
            with pytest.raises(SettingValueError):
                format_value(setting, "anything")

    def test_unknown_key_is_rejected(self):
        with pytest.raises(SettingValueError):
            get_setting("not_a_setting")

    @pytest.mark.asyncio
    async def test_schema_endpoint_describes_the_whole_catalog(self):
        schema = await repeater_settings_schema()
        assert len(schema.settings) == len(REPEATER_SETTINGS)
        assert {group.key for group in schema.groups} == {g.key for g in SETTING_GROUPS}

        password = next(s for s in schema.settings if s.key == "password")
        # The firmware cannot read an admin password back, and the UI needs to
        # know that before it offers a field that would always look empty.
        assert password.readable is False
        assert password.sensitive is True
        # The UI shows the command beside the field; for this one it is not
        # `set password`, so the hint has to come from the catalog.
        assert password.command_hint == "password"


class TestFormatValue:
    def test_int_range_is_enforced(self):
        assert format_value(get_setting("flood_max"), 3) == "3"
        assert format_value(get_setting("flood_max"), "3") == "3"
        with pytest.raises(SettingValueError):
            format_value(get_setting("flood_max"), 999)
        with pytest.raises(SettingValueError):
            format_value(get_setting("flood_max"), 1.5)

    def test_float_keeps_precision_but_drops_a_trailing_zero(self):
        assert format_value(get_setting("lat"), 45.5) == "45.5"
        assert format_value(get_setting("lat"), -0.0) == "0"
        with pytest.raises(SettingValueError):
            format_value(get_setting("lat"), 91)

    def test_bool_accepts_the_words_a_ui_might_send(self):
        repeat = get_setting("repeat")
        assert format_value(repeat, True) == "on"
        assert format_value(repeat, "On") == "on"
        assert format_value(repeat, "0") == "off"
        with pytest.raises(SettingValueError):
            format_value(repeat, "maybe")

    def test_multi_acks_is_written_as_a_digit(self):
        # The firmware parses `multi.acks` with atoi(), so "on" would read as
        # zero and switch the feature off whichever way the operator set it.
        multi = get_setting("multi_acks")
        assert format_value(multi, True) == "1"
        assert format_value(multi, "on") == "1"
        assert format_value(multi, "off") == "0"
        assert multi.set_command(format_value(multi, True)) == "set multi.acks 1"

    def test_enum_is_limited_to_its_options(self):
        mode = get_setting("telemetry_mode_base")
        assert format_value(mode, "Always") == "always"
        with pytest.raises(SettingValueError):
            format_value(mode, "sometimes")
        assert format_value(get_setting("loop_detect"), "Strict") == "strict"
        assert format_value(get_setting("path_hash_mode"), 2) == "2"
        with pytest.raises(SettingValueError):
            format_value(get_setting("path_hash_mode"), 3)

    def test_radio_tuple_is_normalised_and_bounded(self):
        radio = get_setting("radio")
        assert format_value(radio, "869.525, 250, 11, 5") == "869.525,250,11,5"
        # LR2021 boards run in the 2.4 GHz band, which the firmware accepts.
        assert format_value(radio, "2450,500,7,5") == "2450,500,7,5"
        with pytest.raises(SettingValueError):
            format_value(radio, "869.525,250,11")  # too few parts
        with pytest.raises(SettingValueError):
            format_value(radio, "869.525,250,99,5")  # spreading factor out of range
        with pytest.raises(SettingValueError):
            format_value(radio, "5800,250,11,5")  # not a LoRa frequency this firmware runs
        with pytest.raises(SettingValueError):
            format_value(radio, "869.525,1000,11,5")  # bandwidth the firmware refuses

    def test_firmware_ranges_are_mirrored(self):
        # These are the bounds the firmware itself enforces; anything wider here
        # would be accepted by the form and then refused over the air.
        assert format_value(get_setting("tx_delay"), 2) == "2"
        with pytest.raises(SettingValueError):
            format_value(get_setting("tx_delay"), 2.5)
        assert format_value(get_setting("rx_delay"), 20) == "20"
        with pytest.raises(SettingValueError):
            format_value(get_setting("rx_delay"), 21)
        assert format_value(get_setting("flood_advert_interval"), 168) == "168"
        with pytest.raises(SettingValueError):
            format_value(get_setting("flood_advert_interval"), 169)
        with pytest.raises(SettingValueError):
            format_value(get_setting("duty_cycle"), 0.5)
        assert format_value(get_setting("bridge_channel"), 14) == "14"
        with pytest.raises(SettingValueError):
            format_value(get_setting("bridge_channel"), 15)

    def test_extra_spreading_factors_are_a_bounded_list(self):
        extra = get_setting("extra_sf")
        assert format_value(extra, "7, 9,11") == "7,9,11"
        assert format_value(extra, [8]) == "8"
        with pytest.raises(SettingValueError):
            format_value(extra, "7,8,9,10")  # firmware takes at most three
        with pytest.raises(SettingValueError):
            format_value(extra, "7,13")  # not a spreading factor
        with pytest.raises(SettingValueError):
            format_value(extra, "7,x")
        with pytest.raises(SettingValueError):
            format_value(extra, " , ")

    def test_name_rejects_the_characters_the_firmware_refuses(self):
        # isValidName() in the firmware answers "Error, bad chars" for these;
        # refusing them here keeps the batch from half-applying.
        name = get_setting("name")
        assert format_value(name, "Hilltop-2 (north)") == "Hilltop-2 (north)"
        for bad in ("Hill[top]", "a:b", "a,b", "why?", "star*", "back\\slash"):
            with pytest.raises(SettingValueError):
                format_value(name, bad)

    def test_a_value_can_never_carry_a_second_command(self):
        # Everything is sent as the tail of one CLI line, so a newline in a
        # name would be a way to smuggle another command onto the repeater.
        with pytest.raises(SettingValueError):
            format_value(get_setting("name"), "Repeater\nreboot")
        with pytest.raises(SettingValueError):
            format_value(get_setting("name"), "Repeater\x00")

    def test_string_length_and_emptiness_are_checked(self):
        assert format_value(get_setting("name"), "  Hilltop  ") == "Hilltop"
        with pytest.raises(SettingValueError):
            format_value(get_setting("name"), "   ")
        with pytest.raises(SettingValueError):
            format_value(get_setting("name"), "x" * 33)


class TestParseReplies:
    def test_plain_value(self):
        assert parse_get_reply(get_setting("flood_max"), "3") == ("3", "ok")

    def test_key_prefix_is_stripped(self):
        assert parse_get_reply(get_setting("flood_max"), "flood.max: 3") == ("3", "ok")

    def test_a_unit_the_firmware_appends_is_stripped_from_a_number(self):
        # `get dutycycle` answers "100.0%". The editor puts a float in a numeric
        # input, which a browser renders blank for anything but a bare number,
        # so the field looked empty although the value was there in the HTML.
        assert parse_get_reply(get_setting("duty_cycle"), "100.0%") == ("100.0", "ok")
        assert parse_get_reply(get_setting("duty_cycle"), "12.5 %") == ("12.5", "ok")
        assert parse_get_reply(get_setting("tx_power"), "22 dBm") == ("22", "ok")
        assert parse_get_reply(get_setting("tx_power"), "-9") == ("-9", "ok")
        # Anything that is not a number with an optional unit is left alone, so
        # a reply this parser does not understand is still shown rather than lost.
        assert parse_get_reply(get_setting("tx_power"), "22 dBm (max)") == ("22 dBm (max)", "ok")
        # A read-only string keeps its unit: it is displayed, never written back.
        assert parse_get_reply(get_setting("boot_voltage"), "4123 mV") == ("4123 mV", "ok")

    def test_unknown_key_reads_as_unsupported(self):
        # Older firmware answers an unknown config key with this sentinel rather
        # than silence, which is how the UI knows to lock the field.
        assert parse_get_reply(get_setting("duty_cycle"), "??: dutycycle") == (None, "unsupported")

    def test_a_board_that_cannot_do_it_reads_as_unsupported(self):
        # The build knows the key but this hardware has no front-end module /
        # no nRF52 power management; no value will ever take, so lock the field
        # rather than show it as a transient error.
        assert parse_get_reply(get_setting("fem_rx_gain"), "Error: unsupported") == (
            None,
            "unsupported",
        )
        assert parse_get_reply(
            get_setting("power_source"), "ERROR: Power management not supported"
        ) == (None, "unsupported")

    def test_firmware_error_reads_as_error(self):
        assert parse_get_reply(get_setting("flood_max"), "ERR: nope") == (None, "error")

    def test_reply_aliases_normalise_what_set_wants(self):
        # `bridge.source` reads back "logRx"/"logTx" but is written "rx"/"tx".
        assert parse_get_reply(get_setting("bridge_source"), "logRx") == ("rx", "ok")
        assert parse_get_reply(get_setting("bridge_source"), "logTx") == ("tx", "ok")
        # An empty extra-SF list is answered with a sentence, not an empty line.
        assert parse_get_reply(get_setting("extra_sf"), "No extra SF configured") == ("", "ok")
        assert parse_get_reply(get_setting("extra_sf"), "7,9") == ("7,9", "ok")

    def test_read_only_facts_are_passed_through_verbatim(self):
        assert parse_get_reply(get_setting("role"), "repeater") == ("repeater", "ok")
        assert parse_get_reply(get_setting("boot_reason"), "Reset: power-on; Shutdown: none") == (
            "Reset: power-on; Shutdown: none",
            "ok",
        )

    def test_silence_reads_as_no_reply(self):
        assert parse_get_reply(get_setting("flood_max"), None) == (None, "no_reply")

    def test_an_empty_answer_is_a_value_not_silence(self):
        # An unset guest password answers empty; that is "no password", which is
        # not the same as the repeater never answering.
        assert parse_get_reply(get_setting("guest_password"), "") == ("", "ok")

    def test_bool_and_enum_replies_are_normalised(self):
        assert parse_get_reply(get_setting("repeat"), "1") == ("on", "ok")
        assert parse_get_reply(get_setting("repeat"), "Off") == ("off", "ok")
        assert parse_get_reply(get_setting("telemetry_mode_base"), "ALWAYS") == ("always", "ok")

    def test_an_unexpected_enum_reply_is_passed_through(self):
        # A firmware that answers with something we don't model still has to be
        # displayed, rather than reported as an error.
        assert parse_get_reply(get_setting("telemetry_mode_base"), "2") == ("2", "ok")

    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("OK", "ok"),
            ("OK - repeat is now ON", "ok"),
            ("", "ok"),
            ("??: multi.acks", "unsupported"),
            # What `set` answers for a key it has never heard of.
            ("unknown config: password", "unsupported"),
            # What a known key answers on a board that cannot honour it.
            ("Error: unsupported", "unsupported"),
            ("ERR: out of range", "error"),
            ("Error, must be 0-2", "error"),
            (None, "no_reply"),
        ],
    )
    def test_set_replies_are_classified(self, reply, expected):
        assert classify_set_reply(reply) == expected
