"""Per-message retry, cancel and delete, plus the metadata the UI renders from."""

import asyncio
import contextlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from meshcore import EventType

import app.services.message_send as message_send_service
from app.compression.mcmp import try_decode_incoming
from app.models import SendChannelMessageRequest, SendDirectMessageRequest
from app.radio import radio_manager
from app.repository import ChannelRepository, ContactRepository, MessageRepository
from app.routers.messages import (
    cancel_message,
    delete_message,
    retry_message,
    send_channel_message,
    send_direct_message,
)
from app.services import dm_ack_tracker, send_tracker

# Prose-like and long enough that v2's "only if smaller" gate actually fires.
LONG_TEXT = (
    "Hey, are you at the repeater site today? I can hear you but the path looks "
    "long and the noise floor has been climbing all afternoon."
)


@pytest.fixture(autouse=True)
def _reset_radio_state():
    prev = radio_manager._meshcore
    prev_lock = radio_manager._operation_lock
    prev_slot_by_key = radio_manager._channel_slot_by_key.copy()
    prev_key_by_slot = radio_manager._channel_key_by_slot.copy()
    send_tracker.reset()
    yield
    radio_manager._meshcore = prev
    radio_manager._operation_lock = prev_lock
    radio_manager._channel_slot_by_key = prev_slot_by_key
    radio_manager._channel_key_by_slot = prev_key_by_slot
    dm_ack_tracker._pending_acks.clear()
    dm_ack_tracker._buffered_acks.clear()
    send_tracker.reset()


@pytest.fixture(autouse=True)
def _no_background_retries(monkeypatch):
    """Default to send-once so tests opt into the retry loop deliberately."""
    monkeypatch.setattr(
        message_send_service,
        "resolve_max_send_attempts",
        AsyncMock(return_value=1),
    )


def _make_radio_result(payload=None):
    result = MagicMock()
    result.type = EventType.MSG_SENT
    result.payload = payload or {}
    return result


def _make_mc(name="TestNode"):
    mc = MagicMock()
    mc.self_info = {"name": name}
    mc.commands = MagicMock()
    mc.commands.set_flood_scope = AsyncMock(return_value=_make_radio_result())
    mc.commands.send = AsyncMock(return_value=_make_radio_result())
    mc.commands.send_msg = AsyncMock(return_value=_make_radio_result())
    mc.commands.send_chan_msg = AsyncMock(return_value=_make_radio_result())
    mc.commands.add_contact = AsyncMock(return_value=_make_radio_result())
    mc.commands.reset_path = AsyncMock(return_value=MagicMock(type=EventType.OK, payload={}))
    mc.commands.set_channel = AsyncMock(return_value=_make_radio_result())
    mc.get_contact_by_key_prefix = MagicMock(return_value=None)
    return mc


async def _insert_contact(public_key, name="Alice", **overrides):
    data = {
        "public_key": public_key,
        "name": name,
        "type": 0,
        "flags": 0,
        "direct_path": None,
        "direct_path_len": -1,
        "direct_path_hash_mode": -1,
        "last_advert": None,
        "lat": None,
        "lon": None,
        "last_seen": None,
        "on_radio": False,
        "last_contacted": None,
    }
    data.update(overrides)
    await ContactRepository.upsert(data)


def _track(loop, sink: list, coro):
    task = loop.create_task(coro)
    sink.append(task)
    return task


async def _forever(_seconds):
    await asyncio.Event().wait()


@contextlib.contextmanager
def _entered(managers):
    """Enter a tuple of patches as one, keeping the call sites short."""
    with contextlib.ExitStack() as stack:
        yield [stack.enter_context(manager) for manager in managers]


def _radio_patches(mc, broadcasts=None):
    sink = (
        (lambda t, d, **k: broadcasts.append({"type": t, "data": d}))
        if broadcasts is not None
        else MagicMock()
    )
    return (
        patch("app.routers.messages.radio_manager.require_connected", return_value=mc),
        patch.object(radio_manager, "_meshcore", mc),
        patch("app.routers.messages.broadcast_event", side_effect=sink),
        patch("app.routers.messages.track_pending_ack", return_value=False),
    )


class TestPersistedSendMetadata:
    @pytest.mark.asyncio
    async def test_dm_records_the_compression_it_put_on_air(self, test_db):
        mc = _make_mc()
        pub_key = "1a" * 32
        await _insert_contact(pub_key)
        assert await ContactRepository.set_mcmp_enabled(pub_key, True)

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text=LONG_TEXT)
            )

        wire = mc.commands.send_msg.await_args.kwargs["msg"]
        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None
        assert stored.compression == "mcmp2"
        assert stored.plain_bytes == len(LONG_TEXT.encode("utf-8"))
        assert stored.wire_bytes == len(wire.encode("utf-8"))
        assert stored.payload_bytes == stored.wire_bytes
        # The stored text stays plaintext; only the metadata describes the wire.
        assert stored.text == LONG_TEXT
        decoded = try_decode_incoming(wire)
        assert decoded is not None and decoded.text == LONG_TEXT

    @pytest.mark.asyncio
    async def test_dm_records_no_compression_when_it_rode_as_plain_text(self, test_db):
        mc = _make_mc()
        pub_key = "2b" * 32
        await _insert_contact(pub_key)  # MCMP off by default

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text=LONG_TEXT)
            )

        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None
        assert stored.compression is None
        assert stored.wire_bytes is None

    @pytest.mark.asyncio
    async def test_channel_measures_the_ratio_against_the_body_not_the_prefix(self, test_db):
        """The firmware adds "<name>: " outside the compressed payload."""
        mc = _make_mc(name="MyNode")
        chan_key = "3c" * 16
        await ChannelRepository.upsert(key=chan_key, name="#general")
        assert await ChannelRepository.set_mcmp_enabled(chan_key, True)

        with _entered(_radio_patches(mc)):
            sent = await send_channel_message(
                SendChannelMessageRequest(channel_key=chan_key, text=LONG_TEXT)
            )

        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None
        assert stored.compression == "mcmp2"
        assert stored.plain_bytes == len(LONG_TEXT.encode("utf-8"))
        assert stored.text == f"MyNode: {LONG_TEXT}"

    @pytest.mark.asyncio
    async def test_a_first_send_counts_as_one_attempt(self, test_db):
        mc = _make_mc()
        pub_key = "4d" * 32
        await _insert_contact(pub_key)

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )

        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None
        assert stored.send_attempts == 1
        assert stored.send_max_attempts == 1
        # No ACK to wait for at a cap of one, so nothing is still in flight.
        assert stored.send_state == "sent"

    @pytest.mark.asyncio
    async def test_a_retryable_send_is_marked_as_still_sending(self, test_db, monkeypatch):
        monkeypatch.setattr(
            message_send_service, "resolve_max_send_attempts", AsyncMock(return_value=3)
        )
        mc = _make_mc()
        pub_key = "5e" * 32
        await _insert_contact(pub_key)
        mc.commands.send_msg = AsyncMock(
            return_value=_make_radio_result(
                {"expected_ack": b"\xde\xad\xbe\xef", "suggested_timeout": 8000}
            )
        )

        retry_tasks: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()

        with _entered(
            (
                *_radio_patches(mc),
                patch(
                    "app.services.message_send.asyncio.create_task",
                    side_effect=lambda coro: _track(loop, retry_tasks, coro),
                ),
                patch("app.services.message_send.asyncio.sleep", side_effect=_forever),
            )
        ):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )

            stored = await MessageRepository.get_by_id(sent.id)
            assert stored is not None
            assert stored.send_state == "sending"
            assert stored.send_max_attempts == 3
            # The run is tracked, so the user has something to cancel.
            assert send_tracker.is_active(sent.id) is True

            for task in retry_tasks:
                task.cancel()
            await asyncio.gather(*retry_tasks, return_exceptions=True)


class TestCancelMessage:
    @pytest.mark.asyncio
    async def test_stops_a_running_retry_run(self, test_db, monkeypatch):
        monkeypatch.setattr(
            message_send_service, "resolve_max_send_attempts", AsyncMock(return_value=5)
        )
        mc = _make_mc()
        pub_key = "6f" * 32
        await _insert_contact(pub_key)
        mc.commands.send_msg = AsyncMock(
            return_value=_make_radio_result(
                {"expected_ack": b"\xde\xad\xbe\xef", "suggested_timeout": 8000}
            )
        )

        retry_tasks: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()

        with _entered(
            (
                *_radio_patches(mc),
                patch(
                    "app.services.message_send.asyncio.create_task",
                    side_effect=lambda coro: _track(loop, retry_tasks, coro),
                ),
                patch("app.services.message_send.asyncio.sleep", side_effect=_forever),
            )
        ):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            result = await cancel_message(sent.id)
            await asyncio.gather(*retry_tasks, return_exceptions=True)

        assert result.stopped_pending_sends is True
        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None and stored.send_state == "canceled"
        # No further transmissions: only the original send happened.
        assert mc.commands.send_msg.await_count == 1

    @pytest.mark.asyncio
    async def test_cancelling_a_finished_send_still_marks_it_cancelled(self, test_db):
        """The end state the caller asked for, even with nothing left to stop."""
        mc = _make_mc()
        pub_key = "7a" * 32
        await _insert_contact(pub_key)

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            result = await cancel_message(sent.id)

        assert result.stopped_pending_sends is False
        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None and stored.send_state == "canceled"

    @pytest.mark.asyncio
    async def test_rejects_an_incoming_message(self, test_db):
        msg_id = await MessageRepository.create(
            msg_type="PRIV",
            text="from them",
            conversation_key="8b" * 32,
            received_at=1_700_000_000,
        )
        assert msg_id is not None

        with pytest.raises(HTTPException) as err:
            await cancel_message(msg_id)
        assert err.value.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_404_for_an_unknown_message(self, test_db):
        with pytest.raises(HTTPException) as err:
            await cancel_message(999_999)
        assert err.value.status_code == 404


class TestRetryMessage:
    @pytest.mark.asyncio
    async def test_dm_retry_reuses_the_original_timestamp(self, test_db):
        """Reusing the timestamp is what makes this a retry rather than a duplicate."""
        mc = _make_mc()
        pub_key = "9c" * 32
        await _insert_contact(pub_key)

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text=LONG_TEXT)
            )
            first_wire = mc.commands.send_msg.await_args.kwargs["msg"]

            await retry_message(sent.id)

        retry_call = mc.commands.send_msg.await_args
        assert retry_call.kwargs["timestamp"] == sent.sender_timestamp
        assert retry_call.kwargs["msg"] == first_wire

    @pytest.mark.asyncio
    async def test_dm_retry_restarts_the_attempt_count(self, test_db):
        """Accumulating across runs would eventually display "attempt 7 of 3"."""
        mc = _make_mc()
        pub_key = "ad" * 32
        await _insert_contact(pub_key)

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            await retry_message(sent.id)
            await retry_message(sent.id)

        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None
        assert stored.send_attempts == 1
        assert stored.send_max_attempts == 1

    @pytest.mark.asyncio
    async def test_dm_retry_picks_up_a_raised_cap(self, test_db, monkeypatch):
        mc = _make_mc()
        pub_key = "be" * 32
        await _insert_contact(pub_key)

        with _entered(_radio_patches(mc)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            monkeypatch.setattr(
                message_send_service, "resolve_max_send_attempts", AsyncMock(return_value=7)
            )
            await retry_message(sent.id)

        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None and stored.send_max_attempts == 7

    @pytest.mark.asyncio
    async def test_channel_retry_with_a_new_timestamp_creates_a_new_row(self, test_db):
        mc = _make_mc(name="MyNode")
        chan_key = "cf" * 16
        await ChannelRepository.upsert(key=chan_key, name="#general")

        with _entered(_radio_patches(mc)):
            sent = await send_channel_message(
                SendChannelMessageRequest(channel_key=chan_key, text="Hello")
            )
            result = await retry_message(sent.id, new_timestamp=True)

        assert result.message is not None
        assert result.message.id != sent.id
        assert result.message.text == "MyNode: Hello"

    @pytest.mark.asyncio
    async def test_channel_byte_perfect_retry_counts_on_the_original_row(self, test_db):
        """A byte-perfect resend is another transmission of the same message."""
        mc = _make_mc(name="MyNode")
        chan_key = "d0" * 16
        await ChannelRepository.upsert(key=chan_key, name="#general")

        with _entered(_radio_patches(mc)):
            sent = await send_channel_message(
                SendChannelMessageRequest(channel_key=chan_key, text="Hello")
            )
            result = await retry_message(sent.id, new_timestamp=False)

        assert result.message_id == sent.id
        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None and stored.send_attempts == 2

    @pytest.mark.asyncio
    async def test_rejects_an_incoming_message(self, test_db):
        mc = _make_mc()
        msg_id = await MessageRepository.create(
            msg_type="PRIV",
            text="from them",
            conversation_key="e1" * 32,
            received_at=1_700_000_000,
        )
        assert msg_id is not None

        with _entered(_radio_patches(mc)), pytest.raises(HTTPException) as err:
            await retry_message(msg_id)
        assert err.value.status_code == 400

    @pytest.mark.asyncio
    async def test_returns_404_when_the_contact_is_gone(self, test_db):
        mc = _make_mc()
        msg_id = await MessageRepository.create(
            msg_type="PRIV",
            text="ours",
            conversation_key="f2" * 32,
            received_at=1_700_000_000,
            sender_timestamp=1_700_000_000,
            outgoing=True,
        )
        assert msg_id is not None

        with _entered(_radio_patches(mc)), pytest.raises(HTTPException) as err:
            await retry_message(msg_id)
        assert err.value.status_code == 404


class TestRetryRunTerminalStates:
    @pytest.mark.asyncio
    async def test_running_out_of_attempts_marks_the_message_failed(self, test_db, monkeypatch):
        """ "Failed" means we stopped trying -- a late ACK can still flip it to delivered."""
        monkeypatch.setattr(
            message_send_service, "resolve_max_send_attempts", AsyncMock(return_value=2)
        )
        mc = _make_mc()
        pub_key = "36" * 32
        await _insert_contact(pub_key)
        mc.commands.send_msg = AsyncMock(
            return_value=_make_radio_result(
                {"expected_ack": b"\xde\xad\xbe\xef", "suggested_timeout": 1}
            )
        )

        retry_tasks: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()
        statuses: list[dict] = []

        with _entered(
            (
                *_radio_patches(mc, statuses),
                patch(
                    "app.services.message_send.asyncio.create_task",
                    side_effect=lambda coro: _track(loop, retry_tasks, coro),
                ),
                patch("app.services.message_send.asyncio.sleep", new=AsyncMock()),
            )
        ):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            await asyncio.gather(*retry_tasks, return_exceptions=True)

        stored = await MessageRepository.get_by_id(sent.id)
        assert stored is not None
        assert stored.send_state == "failed"
        # Cap of two: the original send plus exactly one retry.
        assert stored.send_attempts == 2
        assert mc.commands.send_msg.await_count == 2
        failures = [
            s
            for s in statuses
            if s["type"] == "message_status" and s["data"]["send_state"] == "failed"
        ]
        assert failures, "the UI is never told the send gave up"


class TestDeleteMessage:
    @pytest.mark.asyncio
    async def test_removes_the_row_and_announces_it(self, test_db):
        mc = _make_mc()
        pub_key = "03" * 32
        await _insert_contact(pub_key)
        broadcasts: list[dict] = []

        with _entered(_radio_patches(mc, broadcasts)):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            await delete_message(sent.id)

        assert await MessageRepository.get_by_id(sent.id) is None
        deletions = [b for b in broadcasts if b["type"] == "message_deleted"]
        assert deletions and deletions[-1]["data"]["message_id"] == sent.id

    @pytest.mark.asyncio
    async def test_deleting_also_cancels_the_pending_attempts(self, test_db, monkeypatch):
        """Otherwise we would keep transmitting a message the user just removed."""
        monkeypatch.setattr(
            message_send_service, "resolve_max_send_attempts", AsyncMock(return_value=5)
        )
        mc = _make_mc()
        pub_key = "14" * 32
        await _insert_contact(pub_key)
        mc.commands.send_msg = AsyncMock(
            return_value=_make_radio_result(
                {"expected_ack": b"\xde\xad\xbe\xef", "suggested_timeout": 8000}
            )
        )

        retry_tasks: list[asyncio.Task] = []
        loop = asyncio.get_running_loop()

        with _entered(
            (
                *_radio_patches(mc),
                patch(
                    "app.services.message_send.asyncio.create_task",
                    side_effect=lambda coro: _track(loop, retry_tasks, coro),
                ),
                patch("app.services.message_send.asyncio.sleep", side_effect=_forever),
            )
        ):
            sent = await send_direct_message(
                SendDirectMessageRequest(destination=pub_key, text="Hello")
            )
            result = await delete_message(sent.id)
            await asyncio.gather(*retry_tasks, return_exceptions=True)

        assert result.stopped_pending_sends is True
        assert send_tracker.is_active(sent.id) is False
        assert mc.commands.send_msg.await_count == 1

    @pytest.mark.asyncio
    async def test_deletes_an_incoming_message_too(self, test_db):
        """Nothing to cancel, but hiding somebody else's message is still allowed."""
        msg_id = await MessageRepository.create(
            msg_type="PRIV",
            text="from them",
            conversation_key="25" * 32,
            received_at=1_700_000_000,
        )
        assert msg_id is not None

        with patch("app.routers.messages.broadcast_event"):
            result = await delete_message(msg_id)

        assert result.stopped_pending_sends is False
        assert await MessageRepository.get_by_id(msg_id) is None

    @pytest.mark.asyncio
    async def test_returns_404_for_an_unknown_message(self, test_db):
        with pytest.raises(HTTPException) as err:
            await delete_message(999_999)
        assert err.value.status_code == 404
