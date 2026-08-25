"""The configurable direct-message attempt cap."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.repository import AppSettingsRepository
from app.send_attempts import (
    DEFAULT_MAX_MESSAGE_RETRIES,
    MAX_MESSAGE_RETRIES,
    MIN_MESSAGE_RETRIES,
    clamp_message_retries,
)
from app.services.message_send import resolve_max_send_attempts


class TestClampMessageRetries:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, MIN_MESSAGE_RETRIES),
            (5, 5),
            (10, MAX_MESSAGE_RETRIES),
            # Out of range in either direction is clamped, not rejected: a stale
            # client must not be able to brick a settings save.
            (0, MIN_MESSAGE_RETRIES),
            (-3, MIN_MESSAGE_RETRIES),
            (999, MAX_MESSAGE_RETRIES),
        ],
    )
    def test_clamps_into_the_legal_range(self, value, expected):
        assert clamp_message_retries(value) == expected

    @pytest.mark.parametrize("value", [None, "not a number", object()])
    def test_falls_back_to_the_default_for_unusable_values(self, value):
        assert clamp_message_retries(value) == DEFAULT_MAX_MESSAGE_RETRIES


class TestResolveMaxSendAttempts:
    @pytest.mark.asyncio
    async def test_reads_the_stored_setting(self, test_db):
        await AppSettingsRepository.update(max_message_retries=6)
        assert await resolve_max_send_attempts() == 6

    @pytest.mark.asyncio
    async def test_clamps_an_out_of_range_stored_value(self, test_db):
        """A value written by a future version must not reach the retry loop raw."""
        repository = MagicMock()
        repository.get = AsyncMock(return_value=MagicMock(max_message_retries=99))

        assert await resolve_max_send_attempts(settings_repository=repository) == (
            MAX_MESSAGE_RETRIES
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_the_default_when_settings_cannot_be_read(self):
        """A send must not fail because of a settings hiccup."""
        repository = MagicMock()
        repository.get = AsyncMock(side_effect=RuntimeError("database gone"))

        assert await resolve_max_send_attempts(settings_repository=repository) == (
            DEFAULT_MAX_MESSAGE_RETRIES
        )


class TestSettingsRoundTrip:
    @pytest.mark.asyncio
    async def test_defaults_to_the_previously_hardcoded_value(self, test_db):
        settings = await AppSettingsRepository.get()
        assert settings.max_message_retries == DEFAULT_MAX_MESSAGE_RETRIES

    @pytest.mark.asyncio
    async def test_update_clamps_before_storing(self, test_db):
        await AppSettingsRepository.update(max_message_retries=50)
        settings = await AppSettingsRepository.get()
        assert settings.max_message_retries == MAX_MESSAGE_RETRIES
