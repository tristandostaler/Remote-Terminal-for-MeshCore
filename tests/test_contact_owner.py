"""Owner info: saved firmware owner.info, operator notes, the sweep, and outreach."""

import time
from contextlib import asynccontextmanager

import pytest

from app.models import ContactUpsert, RepeaterLoginResponse
from app.repository import AppSettingsRepository, ContactRepository
from app.repository.contact_owner import (
    OUTREACH_LOOKBACK_SECONDS,
    ContactOwnerRepository,
    extract_contact_hints,
)
from app.repository.contacts import ContactClockDriftRepository
from app.repository.room_poll import RoomPollRepository
from app.services import owner_info_sweep

NOW = 1_800_000_000
HOUR = 3600
KEY_A = "aa" * 32
KEY_B = "bb" * 32
KEY_C = "cc" * 32


async def _add(key: str, *, type: int = 2, name: str = "Node", last_seen: int = NOW) -> None:
    await ContactRepository.upsert(
        ContactUpsert(
            public_key=key,
            name=name,
            type=type,
            last_seen=last_seen,
            last_advert=last_seen,
            first_seen=last_seen,
        )
    )


async def _drift(key: str, offsets: list[int], *, now: int = NOW) -> None:
    """One reading per hour, newest first, each ``offset`` seconds off."""
    for hours_ago, offset in enumerate(offsets):
        observed = now - hours_ago * HOUR
        await ContactClockDriftRepository.record(
            key, advert_timestamp=observed + offset, observed_at=observed
        )


class TestHints:
    def test_finds_callsigns_emails_handles_and_urls(self):
        hints = extract_contact_hints(
            name="VE2XYZ Rooftop",
            owner_info="Bob|bob@example.org|discord: bobby#12 https://mesh.example.org",
            notes="also @bob_on_mastodon",
        )
        found = {(h.kind, h.value, h.source) for h in hints}
        assert ("callsign", "VE2XYZ", "name") in found
        assert ("email", "bob@example.org", "owner_info") in found
        assert ("handle", "discord: bobby#12", "owner_info") in found
        assert ("url", "https://mesh.example.org", "owner_info") in found
        assert ("handle", "@bob_on_mastodon", "notes") in found

    def test_is_conservative_about_callsigns(self):
        # Lower case and ordinary words are not callsigns; an email's @ is not a handle.
        hints = extract_contact_hints(name="ve2xyz Repeater 5", owner_info="mail me: a@b.com")
        assert [h.kind for h in hints] == ["email"]

    def test_dedupes_across_sources(self):
        hints = extract_contact_hints(name="K1ABC", owner_info="K1ABC", notes="k1abc")
        assert len([h for h in hints if h.kind == "callsign"]) == 1


class TestRepository:
    @pytest.mark.asyncio
    async def test_unknown_node_is_an_empty_record_with_name_hints(self, test_db):
        info = await ContactOwnerRepository.get(KEY_A, name="W1AW-rpt")
        assert info.notes == ""
        assert info.fetched_at is None
        assert [h.value for h in info.hints] == ["W1AW"]

    @pytest.mark.asyncio
    async def test_a_miss_keeps_what_the_node_said_last_time(self, test_db):
        await ContactOwnerRepository.record_fetch(
            KEY_A, status="ok", owner_info="Bob", firmware_version="v1.9", now=NOW
        )
        await ContactOwnerRepository.record_fetch(KEY_A, status="no_reply", now=NOW + 100)

        info = await ContactOwnerRepository.get(KEY_A)
        assert info.firmware_owner_info == "Bob"
        assert info.firmware_version == "v1.9"
        assert info.fetched_at == NOW
        assert info.attempted_at == NOW + 100
        assert info.attempt_status == "no_reply"

    @pytest.mark.asyncio
    async def test_an_empty_answer_clears_the_saved_note(self, test_db):
        await ContactOwnerRepository.record_fetch(KEY_A, status="ok", owner_info="Bob", now=NOW)
        await ContactOwnerRepository.record_fetch(KEY_A, status="ok", owner_info=None, now=NOW + 1)
        assert (await ContactOwnerRepository.get(KEY_A)).firmware_owner_info is None

    @pytest.mark.asyncio
    async def test_fetches_never_touch_the_operators_notes(self, test_db):
        await ContactOwnerRepository.update(KEY_A, notes="  met at the club  ", now=NOW)
        await ContactOwnerRepository.record_fetch(KEY_A, status="ok", owner_info="Bob", now=NOW)

        info = await ContactOwnerRepository.get(KEY_A)
        assert info.notes == "met at the club"
        assert info.notes_updated_at == NOW
        assert info.firmware_owner_info == "Bob"

    @pytest.mark.asyncio
    async def test_notified_mark_sets_and_clears(self, test_db):
        await ContactOwnerRepository.update(KEY_A, notified=True, now=NOW)
        assert (await ContactOwnerRepository.get(KEY_A)).notified_at == NOW
        await ContactOwnerRepository.update(KEY_A, notified=False, now=NOW)
        assert (await ContactOwnerRepository.get(KEY_A)).notified_at is None


class TestSweepTargets:
    @pytest.mark.asyncio
    async def test_rooms_need_a_stored_credential(self, test_db):
        await _add(KEY_A, type=3)
        assert (
            await ContactOwnerRepository.next_sweep_target(stale_before=NOW, heard_since=0)
        ) is None

        await RoomPollRepository.upsert(KEY_A, credential_action="set", credential="")
        assert await ContactOwnerRepository.next_sweep_target(stale_before=NOW, heard_since=0) == (
            KEY_A,
            "",
        )

    @pytest.mark.asyncio
    async def test_skips_quiet_and_fresh_nodes_and_clients(self, test_db):
        await _add(KEY_A, type=2, last_seen=NOW - 30 * 86400)  # gone quiet
        await _add(KEY_B, type=2)
        await _add(KEY_C, type=1)  # a chat node has no owner.info
        await ContactOwnerRepository.record_fetch(KEY_B, status="ok", now=NOW - 60)

        stale_before = NOW - 7 * 86400
        heard_since = NOW - 7 * 86400
        assert (
            await ContactOwnerRepository.next_sweep_target(
                stale_before=stale_before, heard_since=heard_since
            )
            is None
        )

        # A week later B is due again.
        assert await ContactOwnerRepository.next_sweep_target(
            stale_before=NOW + 1, heard_since=heard_since
        ) == (KEY_B, None)

    @pytest.mark.asyncio
    async def test_never_asked_goes_first(self, test_db):
        await _add(KEY_A, type=2)
        await _add(KEY_B, type=2)
        await ContactOwnerRepository.record_fetch(KEY_A, status="no_reply", now=NOW - 10 * 86400)
        target = await ContactOwnerRepository.next_sweep_target(stale_before=NOW, heard_since=0)
        assert target == (KEY_B, None)


class TestSweep:
    def _patch_radio(self, monkeypatch, *, login_status="ok", owner=None):
        @asynccontextmanager
        async def fake_op(*args, **kwargs):
            yield object()

        async def fake_login(mc, contact, password, **kwargs):
            fake_login.passwords.append(password)
            return RepeaterLoginResponse(
                status=login_status, authenticated=login_status == "ok", message=None
            )

        fake_login.passwords = []

        async def fake_request(mc, contact, **kwargs):
            return owner

        monkeypatch.setattr(owner_info_sweep.radio_manager, "radio_operation", fake_op)
        monkeypatch.setattr(
            "app.routers.server_control.prepare_authenticated_contact_connection", fake_login
        )
        monkeypatch.setattr("app.routers.server_control.request_repeater_owner_info", fake_request)
        return fake_login

    @pytest.mark.asyncio
    async def test_setting_defaults_to_weekly_and_round_trips(self, test_db):
        assert (await AppSettingsRepository.get()).owner_info_refresh_days == 7
        updated = await AppSettingsRepository.update(owner_info_refresh_days=0)
        assert updated.owner_info_refresh_days == 0

    @pytest.mark.asyncio
    async def test_disabled_does_nothing(self, test_db, monkeypatch):
        await _add(KEY_A, last_seen=int(time.time()))
        await AppSettingsRepository.update(owner_info_refresh_days=0)
        self._patch_radio(monkeypatch)
        assert await owner_info_sweep.run_owner_info_sweep_step() is None

    @pytest.mark.asyncio
    async def test_step_saves_the_answer(self, test_db, monkeypatch):
        await _add(KEY_A, last_seen=int(time.time()))
        login = self._patch_radio(
            monkeypatch, owner={"owner_info": "VE2XYZ", "firmware_version": "v1.10"}
        )

        assert await owner_info_sweep.run_owner_info_sweep_step() == KEY_A
        assert login.passwords == [""]  # guest login for a repeater
        info = await ContactOwnerRepository.get(KEY_A)
        assert info.attempt_status == "ok"
        assert info.firmware_owner_info == "VE2XYZ"

        # Refreshed just now, so nothing else is due.
        assert await owner_info_sweep.run_owner_info_sweep_step() is None

    @pytest.mark.asyncio
    async def test_rejected_login_is_recorded(self, test_db, monkeypatch):
        await _add(KEY_A, type=3, last_seen=int(time.time()))
        await RoomPollRepository.upsert(KEY_A, credential_action="set", credential="hunter2")
        login = self._patch_radio(monkeypatch, login_status="rejected")

        await owner_info_sweep.run_owner_info_sweep_step()
        assert login.passwords == ["hunter2"]
        assert (await ContactOwnerRepository.get(KEY_A)).attempt_status == "login_failed"


class TestOutreach:
    @pytest.mark.asyncio
    async def test_flags_only_sustained_drift(self, test_db):
        await _add(KEY_A, name="Fast")
        await _add(KEY_B, name="Blip")
        await _add(KEY_C, name="Fine")
        await _drift(KEY_A, [2400, 2390, 2380])
        await _drift(KEY_B, [5, 2400, 3])  # one odd reading is not a problem
        await _drift(KEY_C, [2, 1, 0])
        await ContactOwnerRepository.update(KEY_A, notes="K1ABC", now=NOW)

        result = await ContactOwnerRepository.list_outreach(now=NOW)

        assert [item.public_key for item in result.items] == [KEY_A]
        item = result.items[0]
        assert item.issue == "clock_drift"
        assert item.drift_seconds == 2400
        assert item.severity == "major"
        assert item.readings == 3
        assert item.owner.notes == "K1ABC"
        assert result.nodes_measured == 3
        assert result.server_clock_suspect is False

    @pytest.mark.asyncio
    async def test_a_single_reading_is_not_enough(self, test_db):
        await _add(KEY_A)
        await _drift(KEY_A, [9000])
        assert (await ContactOwnerRepository.list_outreach(now=NOW)).items == []

    @pytest.mark.asyncio
    async def test_unset_clocks_are_their_own_issue_and_sort_last(self, test_db):
        await _add(KEY_A)
        await _add(KEY_B)
        await _drift(KEY_B, [900, 900])
        for hours_ago in range(2):
            observed = NOW - hours_ago * HOUR
            await ContactClockDriftRepository.record(
                KEY_A, advert_timestamp=1000 + hours_ago, observed_at=observed
            )

        result = await ContactOwnerRepository.list_outreach(now=NOW)
        assert [(i.public_key, i.issue) for i in result.items] == [
            (KEY_B, "clock_drift"),
            (KEY_A, "unset_clock"),
        ]

    @pytest.mark.asyncio
    async def test_recently_notified_sorts_after_the_rest(self, test_db):
        await _add(KEY_A)
        await _add(KEY_B)
        await _drift(KEY_A, [9000, 9000])
        await _drift(KEY_B, [600, 600])
        await ContactOwnerRepository.update(KEY_A, notified=True, now=NOW - HOUR)

        items = (await ContactOwnerRepository.list_outreach(now=NOW)).items
        assert [(i.public_key, i.recently_notified) for i in items] == [
            (KEY_B, False),
            (KEY_A, True),
        ]

        # A mark older than the lookback no longer holds the node back.
        await ContactOwnerRepository.update(
            KEY_A, notified=True, now=NOW - OUTREACH_LOOKBACK_SECONDS - 1
        )
        items = (await ContactOwnerRepository.list_outreach(now=NOW)).items
        assert items[0].public_key == KEY_A

    @pytest.mark.asyncio
    async def test_whole_mesh_off_points_at_our_clock(self, test_db):
        for key in (KEY_A, KEY_B, KEY_C):
            await _add(key)
            await _drift(key, [-1800, -1800])

        result = await ContactOwnerRepository.list_outreach(now=NOW)
        assert result.server_clock_suspect is True
        assert result.median_drift_seconds == -1800


class TestApi:
    @pytest.mark.asyncio
    async def test_edit_and_read_back_owner(self, test_db, client):
        await _add(KEY_A, name="Hill")

        empty = await client.get(f"/api/contacts/{KEY_A}/owner")
        assert empty.status_code == 200
        assert empty.json()["notes"] == ""

        patched = await client.patch(
            f"/api/contacts/{KEY_A}/owner", json={"notes": "VE2ABC on the forum", "notified": True}
        )
        assert patched.status_code == 200
        data = patched.json()
        assert data["notes"] == "VE2ABC on the forum"
        assert data["notified_at"] is not None
        assert {"kind": "callsign", "value": "VE2ABC", "source": "notes"} in data["hints"]

        stats = await client.get(f"/api/contacts/{KEY_A}/stats")
        assert stats.json()["owner"]["notes"] == "VE2ABC on the forum"

    @pytest.mark.asyncio
    async def test_notes_are_length_limited(self, test_db, client):
        await _add(KEY_A)
        response = await client.patch(f"/api/contacts/{KEY_A}/owner", json={"notes": "x" * 2001})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_contact_is_404(self, test_db, client):
        response = await client.patch(f"/api/contacts/{KEY_A}/owner", json={"notes": "x"})
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_outreach_endpoint(self, test_db, client):
        now = int(time.time())
        await _add(KEY_A, last_seen=now)
        await _drift(KEY_A, [3000, 3000], now=now)

        response = await client.get("/api/contacts/owner-outreach")
        assert response.status_code == 200
        assert [item["public_key"] for item in response.json()["items"]] == [KEY_A]

    @pytest.mark.asyncio
    async def test_refresh_setting_is_validated(self, test_db, client):
        bad = await client.patch("/api/settings", json={"owner_info_refresh_days": 5})
        assert bad.status_code == 400
        good = await client.patch("/api/settings", json={"owner_info_refresh_days": 14})
        assert good.status_code == 200
        assert good.json()["owner_info_refresh_days"] == 14


class TestRefreshEndpoint:
    def _patch(self, monkeypatch):
        calls: list[tuple] = []

        async def fake_refresh(public_key, credential, *, blocking=False):
            calls.append((public_key, credential, blocking))
            await ContactOwnerRepository.record_fetch(
                public_key, status="ok", owner_info="VE2XYZ", firmware_version="v1.10"
            )
            return "ok"

        monkeypatch.setattr("app.routers.contacts.radio_manager.require_connected", lambda: None)
        monkeypatch.setattr("app.services.owner_info_sweep.refresh_owner_info", fake_refresh)
        return calls

    @pytest.mark.asyncio
    async def test_repeater_is_fetched_now_with_a_guest_login(self, test_db, client, monkeypatch):
        await _add(KEY_A, type=2)
        calls = self._patch(monkeypatch)

        response = await client.post(f"/api/contacts/{KEY_A}/owner/refresh")

        assert response.status_code == 200
        assert response.json()["firmware_owner_info"] == "VE2XYZ"
        assert response.json()["attempt_status"] == "ok"
        # Waits for the radio rather than giving up like the sweep does.
        assert calls == [(KEY_A, None, True)]

    @pytest.mark.asyncio
    async def test_room_needs_a_stored_credential(self, test_db, client, monkeypatch):
        await _add(KEY_A, type=3)
        calls = self._patch(monkeypatch)

        refused = await client.post(f"/api/contacts/{KEY_A}/owner/refresh")
        assert refused.status_code == 409
        assert calls == []

        await RoomPollRepository.upsert(KEY_A, credential_action="set", credential="pw")
        ok = await client.post(f"/api/contacts/{KEY_A}/owner/refresh")
        assert ok.status_code == 200
        assert calls == [(KEY_A, "pw", True)]

    @pytest.mark.asyncio
    async def test_chat_nodes_have_no_owner_info(self, test_db, client, monkeypatch):
        await _add(KEY_A, type=1)
        self._patch(monkeypatch)

        response = await client.post(f"/api/contacts/{KEY_A}/owner/refresh")
        assert response.status_code == 400
