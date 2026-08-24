"""Tests for companion repeat ("relay") mode helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.repeat_mode import (
    FALLBACK_ALLOWED_REPEAT_FREQS_MHZ,
    RepeatFreqRange,
    describe_allowed_repeat_freqs,
    extract_repeat_flag,
    freq_allowed_for_repeat,
    normalize_repeat_freq_mhz,
    parse_allowed_repeat_freqs,
    query_allowed_repeat_freqs,
)


def _device_frame(fw_ver: int, *, repeat: int | None = None, path_hash_mode: int | None = None):
    """Build a raw DEVICE_INFO frame (byte 0 = packet type)."""
    frame = bytearray(80)
    frame[0] = 4  # PacketType.DEVICE_INFO
    frame[1] = fw_ver
    if repeat is not None:
        frame.append(repeat)
    if path_hash_mode is not None:
        frame.append(path_hash_mode)
    return bytes(frame)


class TestExtractRepeatFlag:
    def test_reads_parsed_payload(self):
        assert extract_repeat_flag({"repeat": True}, None) is True
        assert extract_repeat_flag({"repeat": False}, None) is False

    def test_reads_integer_payload(self):
        assert extract_repeat_flag({"repeat": 1}, None) is True
        assert extract_repeat_flag({"repeat": 0}, None) is False

    def test_returns_none_when_firmware_does_not_report_it(self):
        assert extract_repeat_flag({"fw ver": 8}, _device_frame(8)) is None

    def test_falls_back_to_raw_frame(self):
        frame = _device_frame(10, repeat=1, path_hash_mode=2)
        assert extract_repeat_flag({"fw ver": 10}, frame) is True

    def test_raw_frame_fallback_ignores_older_firmware(self):
        # A short frame from fw_ver 8 has no repeat byte to read.
        assert extract_repeat_flag({}, _device_frame(8, repeat=1)) is None


class TestNormalizeRepeatFreq:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (869, 869.0),  # MHz
            (869_000, 869.0),  # kHz
            (869_000_000, 869.0),  # Hz
            (2_400_000, 2400.0),  # 2.4 GHz reported in kHz
            (2_400_000_000, 2400.0),  # 2.4 GHz reported in Hz
        ],
    )
    def test_normalizes_units(self, value, expected):
        assert normalize_repeat_freq_mhz(value) == expected

    def test_rejects_non_positive(self):
        assert normalize_repeat_freq_mhz(0) is None
        assert normalize_repeat_freq_mhz(-1) is None


class TestParseAllowedRepeatFreqs:
    def test_parses_ranges(self):
        payload = {"freqs": [{"min": 433_000, "max": 434_790}, {"min": 869_000, "max": 869_000}]}
        assert parse_allowed_repeat_freqs(payload) == [
            RepeatFreqRange(433.0, 434.79),
            RepeatFreqRange(869.0, 869.0),
        ]

    def test_skips_malformed_entries(self):
        payload = {"freqs": [{"min": "433"}, {"max": 869_000}, 7, {"min": 918_000, "max": 0}]}
        assert parse_allowed_repeat_freqs(payload) == []

    def test_handles_missing_payload(self):
        assert parse_allowed_repeat_freqs({}) == []


class TestQueryAllowedRepeatFreqs:
    @pytest.mark.asyncio
    async def test_uses_radio_reported_ranges(self):
        event = MagicMock()
        event.payload = {"freqs": [{"min": 918_000, "max": 918_000}]}
        mc = MagicMock()
        mc.commands.get_allowed_repeat_freq = AsyncMock(return_value=event)

        assert await query_allowed_repeat_freqs(mc) == [RepeatFreqRange(918.0, 918.0)]

    @pytest.mark.asyncio
    async def test_falls_back_when_command_unsupported(self):
        mc = MagicMock()
        mc.commands.get_allowed_repeat_freq = AsyncMock(side_effect=RuntimeError("boom"))

        assert await query_allowed_repeat_freqs(mc) == list(FALLBACK_ALLOWED_REPEAT_FREQS_MHZ)

    @pytest.mark.asyncio
    async def test_falls_back_on_empty_response(self):
        event = MagicMock()
        event.payload = {"freqs": []}
        mc = MagicMock()
        mc.commands.get_allowed_repeat_freq = AsyncMock(return_value=event)

        assert await query_allowed_repeat_freqs(mc) == list(FALLBACK_ALLOWED_REPEAT_FREQS_MHZ)


class TestFreqAllowedForRepeat:
    def test_matches_within_tolerance(self):
        allowed = [RepeatFreqRange(869.0, 869.0)]
        assert freq_allowed_for_repeat(869.0, allowed) is True
        assert freq_allowed_for_repeat(869.0004, allowed) is True
        assert freq_allowed_for_repeat(869.525, allowed) is False

    def test_matches_inside_range(self):
        allowed = [RepeatFreqRange(433.0, 434.79)]
        assert freq_allowed_for_repeat(434.0, allowed) is True
        assert freq_allowed_for_repeat(435.0, allowed) is False

    def test_unknown_allow_list_permits_everything(self):
        assert freq_allowed_for_repeat(910.525, []) is True


class TestDescribeAllowedRepeatFreqs:
    def test_formats_points_and_ranges(self):
        described = describe_allowed_repeat_freqs(
            [RepeatFreqRange(433.0, 434.79), RepeatFreqRange(869.0, 869.0)]
        )
        assert described == "433-434.79 MHz, 869 MHz"
