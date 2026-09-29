import pytest

from app.repository import AppSettingsRepository
from app.services import region_discovery
from app.services.region_discovery import merge_new_regions


def test_merge_new_regions_dedupes_and_normalizes():
    added = merge_new_regions(["Alpha"], ["#alpha", "Beta", "#beta", "*", "", "  Gamma  "])
    assert added == ["Beta", "Gamma"]


@pytest.mark.asyncio
async def test_auto_discover_setting_round_trips(test_db):
    assert (await AppSettingsRepository.get()).auto_discover_regions_hours == 0
    updated = await AppSettingsRepository.update(auto_discover_regions_hours=24)
    assert updated.auto_discover_regions_hours == 24


@pytest.mark.asyncio
async def test_sweep_adds_new_regions_only(test_db, monkeypatch):
    from contextlib import asynccontextmanager

    from app.models import Contact

    await AppSettingsRepository.update(known_regions=["Alpha"])
    repeater = Contact(public_key="aa" * 32, name="R1", type=2)

    async def fake_recent(limit=8):
        return [repeater]

    async def fake_request(mc, contact):
        return ["*", "#alpha", "Beta"]

    @asynccontextmanager
    async def fake_op(*args, **kwargs):
        yield object()

    backfilled: list[list[str]] = []

    async def fake_backfill(regions):
        backfilled.append(list(regions))
        return {}

    monkeypatch.setattr(
        region_discovery.ContactRepository, "get_repeaters_by_recent", staticmethod(fake_recent)
    )
    monkeypatch.setattr("app.routers.repeaters.request_anon_region_names", fake_request)
    monkeypatch.setattr(region_discovery.radio_manager, "radio_operation", fake_op, raising=False)
    monkeypatch.setattr("app.services.messages.backfill_message_regions", fake_backfill)

    added = await region_discovery.run_region_discovery_sweep()

    assert added == ["Beta"]
    assert (await AppSettingsRepository.get()).known_regions == ["Alpha", "Beta"]
