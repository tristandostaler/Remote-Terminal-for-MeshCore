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

    def test_enum_is_limited_to_its_options(self):
        mode = get_setting("telemetry_mode_base")
        assert format_value(mode, "Always") == "always"
        with pytest.raises(SettingValueError):
            format_value(mode, "sometimes")

    def test_radio_tuple_is_normalised_and_bounded(self):
        radio = get_setting("radio")
        assert format_value(radio, "869.525, 250, 11, 5") == "869.525,250,11,5"
        with pytest.raises(SettingValueError):
            format_value(radio, "869.525,250,11")  # too few parts
        with pytest.raises(SettingValueError):
            format_value(radio, "869.525,250,99,5")  # spreading factor out of range
        with pytest.raises(SettingValueError):
            format_value(radio, "2400,250,11,5")  # not a LoRa frequency this firmware runs

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

    def test_unknown_key_reads_as_unsupported(self):
        # Older firmware answers an unknown config key with this sentinel rather
        # than silence, which is how the UI knows to lock the field.
        assert parse_get_reply(get_setting("duty_cycle"), "??: dutycycle") == (None, "unsupported")

    def test_firmware_error_reads_as_error(self):
        assert parse_get_reply(get_setting("flood_max"), "ERR: nope") == (None, "error")

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
            ("", "ok"),
            ("??: multi.acks", "unsupported"),
            ("ERR: out of range", "error"),
            (None, "no_reply"),
        ],
    )
    def test_set_replies_are_classified(self, reply, expected):
        assert classify_set_reply(reply) == expected
