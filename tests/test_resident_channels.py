"""Resident channels: keep channels loaded in radio slots and consume the
firmware's queued copies as a fallback for the raw RX-log route."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from meshcore import EventType
from meshcore.events import Event

import app.radio_sync as radio_sync
from app.channel_constants import PUBLIC_CHANNEL_KEY, PUBLIC_CHANNEL_NAME
from app.event_handlers import on_channel_message
from app.models import Channel
from app.radio import RadioManager, radio_manager
from app.radio_sync import (
    add_resident_channel_to_radio,
    remove_resident_channel_from_radio,
    select_resident_channels,
    sync_and_offload_channels,
)
from app.repository import ChannelRepository, MessageRepository


@pytest.fixture(autouse=True)
def _reset_radio_manager_state():
    prev_mc = radio_manager._meshcore
    prev_max_channels = radio_manager.max_channels
    prev_connection_info = radio_manager._connection_info
    radio_manager.reset_channel_send_cache()
    radio_manager.clear_pending_message_channel_slots()
    radio_manager.clear_resident_channels()
    yield
    radio_manager._meshcore = prev_mc
    radio_manager.max_channels = prev_max_channels
    radio_manager._connection_info = prev_connection_info
    radio_manager.reset_channel_send_cache()
    radio_manager.clear_pending_message_channel_slots()
    radio_manager.clear_resident_channels()


def _channel(key: str, name: str, favorite: bool = False) -> Channel:
    return Channel(key=key.upper(), name=name, favorite=favorite)


def _slot(name: str, key_hex: str) -> MagicMock:
    result = MagicMock()
    result.type = EventType.CHANNEL_INFO
    result.payload = {"channel_name": name, "channel_secret": bytes.fromhex(key_hex)}
    return result


def _empty() -> MagicMock:
    result = MagicMock()
    result.type = EventType.ERROR
    result.payload = {"error": "empty"}
    return result


def _ok() -> MagicMock:
    result = MagicMock()
    result.type = EventType.OK
    result.payload = {}
    return result


class TestSelectResidentChannels:
    def test_public_first_then_favorites_then_activity_then_name(self):
        public = _channel(PUBLIC_CHANNEL_KEY, PUBLIC_CHANNEL_NAME)
        fav = _channel("11" * 16, "#zzz-fav", favorite=True)
        busy = _channel("22" * 16, "#busy")
        idle = _channel("33" * 16, "#idle")
        never = _channel("44" * 16, "#aaa-never")
        activity = {busy.key: 200, idle.key: 100, public.key: 50}

        chosen = select_resident_channels([never, idle, busy, fav, public], activity, 40)

        assert [c.name for c in chosen] == ["Public", "#zzz-fav", "#busy", "#idle", "#aaa-never"]

    def test_leaves_one_scratch_slot(self):
        channels = [_channel(f"{i:02x}" * 16, f"#c{i}") for i in range(1, 6)]

        assert len(select_resident_channels(channels, {}, 3)) == 2
        assert select_resident_channels(channels, {}, 1) == []
        assert select_resident_channels(channels, {}, 0) == []


class TestSyncKeepsResidentChannels:
    @pytest.mark.asyncio
    async def test_loads_public_and_active_channels_and_clears_the_rest(self, test_db):
        """The radio ends up holding the resident set in slot order; other slots are cleared."""
        await ChannelRepository.upsert(key="11" * 16, name="#quiet")
        radio_manager.max_channels = 4

        # Radio starts with a stale channel in slot 3 and Public in slot 1.
        mock_mc = MagicMock()
        mock_mc.commands.get_channel = AsyncMock(
            side_effect=[
                _empty(),
                _slot(PUBLIC_CHANNEL_NAME, PUBLIC_CHANNEL_KEY),
                _empty(),
                _slot("#stale", "99" * 16),
            ]
        )
        mock_mc.commands.set_channel = AsyncMock(return_value=_ok())

        result = await sync_and_offload_channels(mock_mc)

        # #stale was synced into the DB (synced=2 with Public). Four channels
        # now exist (the test DB seeds #remoteterm) but only three fit with one
        # scratch slot kept free, so the last by name (#stale) is not resident.
        assert result["synced"] == 2
        assert result["resident"] == 3
        writes = {
            call.kwargs["channel_idx"]: call.kwargs["channel_name"]
            for call in mock_mc.commands.set_channel.await_args_list
        }
        # Slot 0 gets Public, slots 1-2 the hashtag channels (no activity: by name),
        # slot 3 (previously #stale) is cleared as the scratch slot.
        assert writes[0] == PUBLIC_CHANNEL_NAME
        assert {writes[1], writes[2]} == {"#quiet", "#remoteterm"}
        assert writes[3] == ""
        assert result["cleared"] == 1
        assert radio_manager.get_resident_channel_slot("99" * 16) is None

        assert radio_manager.get_resident_channel_slot(PUBLIC_CHANNEL_KEY) == 0
        assert radio_manager.resident_channel_count() == 3
        assert radio_manager.channel_key_for_slot(0) == PUBLIC_CHANNEL_KEY
        public = await ChannelRepository.get_by_key(PUBLIC_CHANNEL_KEY)
        assert public is not None and public.on_radio is True

    @pytest.mark.asyncio
    async def test_slot_already_holding_the_right_channel_is_not_rewritten(self, test_db):
        radio_manager.max_channels = 2
        mock_mc = MagicMock()
        mock_mc.commands.get_channel = AsyncMock(
            side_effect=[_slot(PUBLIC_CHANNEL_NAME, PUBLIC_CHANNEL_KEY), _empty()]
        )
        mock_mc.commands.set_channel = AsyncMock(return_value=_ok())

        result = await sync_and_offload_channels(mock_mc)

        mock_mc.commands.set_channel.assert_not_awaited()
        assert result["resident"] == 1
        assert radio_manager.get_resident_channel_slot(PUBLIC_CHANNEL_KEY) == 0

    @pytest.mark.asyncio
    async def test_opt_out_clears_everything(self, test_db):
        radio_manager.max_channels = 2
        mock_mc = MagicMock()
        mock_mc.commands.get_channel = AsyncMock(
            side_effect=[_slot(PUBLIC_CHANNEL_NAME, PUBLIC_CHANNEL_KEY), _empty()]
        )
        mock_mc.commands.set_channel = AsyncMock(return_value=_ok())

        with patch("app.radio_sync.settings.resident_channels_enabled", False):
            result = await sync_and_offload_channels(mock_mc)

        assert result["cleared"] == 1
        assert result["resident"] == 0
        assert mock_mc.commands.set_channel.await_args.kwargs["channel_name"] == ""
        assert radio_manager.resident_channel_count() == 0


class TestSendSlotPlanningWithResidentChannels:
    def test_resident_channel_sends_from_its_pinned_slot_without_reconfigure(self):
        rm = RadioManager()
        rm.max_channels = 4
        rm._connection_info = "Serial: /dev/ttyUSB0"
        rm.set_resident_channels({PUBLIC_CHANNEL_KEY: 0, "11" * 16: 1})

        assert rm.plan_channel_send_slot(PUBLIC_CHANNEL_KEY) == (0, False, None)

    def test_resident_channel_on_tcp_rewrites_its_own_slot(self):
        rm = RadioManager()
        rm.max_channels = 4
        rm._connection_info = "TCP: 10.0.0.5:5000"
        rm.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})

        slot, needs_configure, evicted = rm.plan_channel_send_slot("22" * 16, preferred_slot=0)
        assert (slot, needs_configure, evicted) == (1, True, None)
        assert rm.plan_channel_send_slot(PUBLIC_CHANNEL_KEY, preferred_slot=0) == (0, True, None)

    def test_non_resident_sends_never_touch_resident_slots(self):
        rm = RadioManager()
        rm.max_channels = 3
        rm._connection_info = "Serial: /dev/ttyUSB0"
        rm.set_resident_channels({PUBLIC_CHANNEL_KEY: 0, "11" * 16: 1})

        slot_a, configure_a, _ = rm.plan_channel_send_slot("aa" * 16, preferred_slot=0)
        assert (slot_a, configure_a) == (2, True)
        rm.note_channel_slot_loaded("aa" * 16, slot_a)

        # Only one scratch slot: the next channel evicts the cached one, not a resident.
        slot_b, configure_b, evicted = rm.plan_channel_send_slot("bb" * 16, preferred_slot=0)
        assert (slot_b, configure_b, evicted) == (2, True, ("aa" * 16).upper())
        assert rm.get_resident_channel_slot(PUBLIC_CHANNEL_KEY) == 0
        assert rm.get_resident_channel_slot("11" * 16) == 1

    def test_first_free_resident_slot_keeps_one_scratch_slot(self):
        rm = RadioManager()
        rm.max_channels = 3
        rm.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})
        assert rm.first_free_resident_slot() == 1
        rm.add_resident_channel("11" * 16, 1)
        assert rm.first_free_resident_slot() is None


class TestResidentSlotUpdates:
    @pytest.mark.asyncio
    async def test_add_pins_into_free_slot_and_flags_on_radio(self, test_db):
        radio_manager.max_channels = 4
        radio_manager.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})
        await ChannelRepository.upsert(key="11" * 16, name="#new")
        channel = await ChannelRepository.get_by_key("11" * 16)
        mock_mc = MagicMock()
        mock_mc.commands.set_channel = AsyncMock(return_value=_ok())

        assert await add_resident_channel_to_radio(mock_mc, channel) is True

        mock_mc.commands.set_channel.assert_awaited_once()
        assert mock_mc.commands.set_channel.await_args.kwargs["channel_idx"] == 1
        assert radio_manager.get_resident_channel_slot("11" * 16) == 1
        stored = await ChannelRepository.get_by_key("11" * 16)
        assert stored is not None and stored.on_radio is True

    @pytest.mark.asyncio
    async def test_remove_clears_the_slot(self, test_db):
        radio_manager.max_channels = 4
        radio_manager.set_resident_channels({PUBLIC_CHANNEL_KEY: 0, "11" * 16: 1})
        mock_mc = MagicMock()
        mock_mc.commands.set_channel = AsyncMock(return_value=_ok())

        assert await remove_resident_channel_from_radio(mock_mc, "11" * 16) is True

        kwargs = mock_mc.commands.set_channel.await_args.kwargs
        assert kwargs["channel_idx"] == 1 and kwargs["channel_name"] == ""
        assert radio_manager.get_resident_channel_slot("11" * 16) is None
        assert radio_manager.get_resident_channel_slot(PUBLIC_CHANNEL_KEY) == 0


class TestQueuedChannelMessageFallback:
    @pytest.mark.asyncio
    async def test_handler_stores_queued_message_for_resident_slot(self, test_db):
        """A CHANNEL_MSG_RECV the firmware queued lands as an ordinary channel message."""
        await ChannelRepository.upsert(key=PUBLIC_CHANNEL_KEY, name=PUBLIC_CHANNEL_NAME)
        radio_manager.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})
        mock_mc = MagicMock()
        mock_mc.commands.get_channel = AsyncMock(side_effect=AssertionError("no radio query"))
        radio_manager._meshcore = mock_mc

        event = Event(
            EventType.CHANNEL_MSG_RECV,
            {
                "channel_idx": 0,
                "text": "Bob: heard through the queue",
                "sender_timestamp": 1700000000,
                "txt_type": 0,
                "path_len": 1,
            },
        )
        with patch("app.radio_sync.broadcast_event"):
            await on_channel_message(event)

        stored = await MessageRepository.get_all(
            msg_type="CHAN", conversation_key=PUBLIC_CHANNEL_KEY
        )
        assert len(stored) == 1
        assert stored[0].text == "Bob: heard through the queue"
        assert stored[0].sender_name == "Bob"
        mock_mc.commands.get_channel.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_scratch_slot_is_resolved_by_asking_the_radio(self, test_db):
        """A non-resident slot may hold whatever the last send loaded: ask the radio."""
        radio_manager.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})
        radio_manager.remember_pending_message_channel_slot("99" * 16, 3)  # stale snapshot
        mock_mc = MagicMock()
        mock_mc.commands.get_channel = AsyncMock(return_value=_slot("#fresh", "11" * 16))
        radio_manager._meshcore = mock_mc

        event = Event(
            EventType.CHANNEL_MSG_RECV,
            {"channel_idx": 3, "text": "Cara: via scratch slot", "sender_timestamp": 1700000000},
        )
        with patch("app.radio_sync.broadcast_event"):
            await on_channel_message(event)

        mock_mc.commands.get_channel.assert_awaited_once_with(3)
        stored = await MessageRepository.get_all(msg_type="CHAN", conversation_key="11" * 16)
        assert len(stored) == 1
        assert await MessageRepository.get_all(msg_type="CHAN", conversation_key="99" * 16) == []

    @pytest.mark.asyncio
    async def test_queued_copy_collapses_onto_raw_copy(self, test_db):
        """When the raw frame got there first, the pulled copy is a dedup no-op."""
        await ChannelRepository.upsert(key=PUBLIC_CHANNEL_KEY, name=PUBLIC_CHANNEL_NAME)
        radio_manager.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})
        radio_manager._meshcore = MagicMock()
        await MessageRepository.create(
            msg_type="CHAN",
            text="Bob: same message",
            conversation_key=PUBLIC_CHANNEL_KEY,
            sender_timestamp=1700000000,
            received_at=1700000001,
            sender_name="Bob",
        )

        event = Event(
            EventType.CHANNEL_MSG_RECV,
            {"channel_idx": 0, "text": "Bob: same message", "sender_timestamp": 1700000000},
        )
        with patch("app.radio_sync.broadcast_event"):
            await on_channel_message(event)

        stored = await MessageRepository.get_all(
            msg_type="CHAN", conversation_key=PUBLIC_CHANNEL_KEY
        )
        assert len(stored) == 1

    @pytest.mark.asyncio
    async def test_handler_stands_down_while_a_drain_pulls_inline(self, test_db):
        await ChannelRepository.upsert(key=PUBLIC_CHANNEL_KEY, name=PUBLIC_CHANNEL_NAME)
        radio_manager.set_resident_channels({PUBLIC_CHANNEL_KEY: 0})
        radio_manager._meshcore = MagicMock()
        event = Event(
            EventType.CHANNEL_MSG_RECV,
            {"channel_idx": 0, "text": "Bob: pulled by drain", "sender_timestamp": 1700000000},
        )

        async with radio_sync.inline_message_pull():
            await on_channel_message(event)

        stored = await MessageRepository.get_all(
            msg_type="CHAN", conversation_key=PUBLIC_CHANNEL_KEY
        )
        assert stored == []

    @pytest.mark.asyncio
    async def test_handler_ignores_grp_data_placeholder(self, test_db):
        from app.imaging.aeic.channel_data import grp_data_placeholder_payload

        radio_manager._meshcore = MagicMock()
        await on_channel_message(Event(EventType.CHANNEL_MSG_RECV, grp_data_placeholder_payload()))
        assert await MessageRepository.get_all(msg_type="CHAN") == []
