"""GRP_DATA frames and the firmware's message queue.

Frame 27 usually arrives as the firmware's answer to ``CMD_SYNC_NEXT_MESSAGE``
(MCO Advanced's own sync tracker lists ``respCodeChannelDataRecv`` beside the
msg-recv codes), but meshcore-py's ``get_msg`` resolves only on CONTACT_MSG_RECV /
CHANNEL_MSG_RECV / ERROR / NO_MORE_MSGS. Our adapter consumes the frame, which
left the caller that asked for it waiting on a reply that had already come:

* the auto-fetch loop asks with NO timeout, so one queued image hung it forever
  -- and its MESSAGES_WAITING handler starts no new task while one is alive, so
  every later push-driven fetch died with it;
* the drain loops burned a 2 s timeout per image and stopped early.

Channel *text* never touches this queue (it is sniffed off raw RF), which is why
the failure presented as "text always, images never".

The adapter now dispatches a placeholder CHANNEL_MSG_RECV per frame so the waiter
resolves, and the pulled-message consumers skip the placeholder but keep draining.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from meshcore import EventType
from meshcore.events import Event, EventDispatcher

import app.event_handlers as event_handlers
from app.imaging.aeic.channel_data import (
    RESP_CODE_CHANNEL_DATA_RECV,
    grp_data_placeholder_payload,
    is_grp_data_placeholder,
)
from app.radio_sync import drain_pending_messages, poll_for_messages

FRAME_27 = bytes([RESP_CODE_CHANNEL_DATA_RECV]) + bytes(8) + b"x" * 20


class _FakeMeshcore:
    """A meshcore with a real dispatcher, which is the part under test."""

    def __init__(self) -> None:
        self.dispatcher = EventDispatcher()
        self.original_frames: list[bytes] = []

        async def handle_rx(data: bytearray) -> None:
            self.original_frames.append(bytes(data))

        self._reader = SimpleNamespace(handle_rx=handle_rx)


@pytest.fixture
async def meshcore(monkeypatch):
    mc = _FakeMeshcore()
    await mc.dispatcher.start()
    # The image path itself is covered elsewhere; here it must only not explode.
    monkeypatch.setattr(event_handlers, "on_channel_data", AsyncMock())
    event_handlers.install_channel_data_adapter(mc)
    yield mc
    await mc.dispatcher.stop()


class TestReleasingTheWaiter:
    async def test_a_get_msg_with_no_timeout_is_not_hung_forever(self, meshcore):
        """The auto-fetch case, and the one that took message fetching down.

        ``get_msg()`` without a timeout waits until one of its four event types
        arrives. A frame 27 used to be none of them, so this wait -- and the
        auto-fetch task built on it -- never ended.
        """
        waiter = asyncio.ensure_future(
            meshcore.dispatcher.wait_for_event(EventType.CHANNEL_MSG_RECV, {}, timeout=None)
        )
        await asyncio.sleep(0)  # let the waiter subscribe

        await meshcore._reader.handle_rx(bytearray(FRAME_27))

        event = await asyncio.wait_for(waiter, timeout=2.0)
        assert event is not None, "the waiter was never released"
        assert is_grp_data_placeholder(event.payload), (
            "the waiter resolved on something other than the frame's stand-in"
        )

    async def test_the_frame_is_still_handled_and_consumed(self, meshcore):
        await meshcore._reader.handle_rx(bytearray(FRAME_27))

        event_handlers.on_channel_data.assert_awaited_once_with(FRAME_27)
        assert meshcore.original_frames == [], "frame 27 leaked to meshcore-py's reader"

    async def test_other_frames_pass_through_untouched(self, meshcore):
        other = bytes([16]) + b"hello"
        waiter = asyncio.ensure_future(
            meshcore.dispatcher.wait_for_event(EventType.CHANNEL_MSG_RECV, {}, timeout=0.2)
        )
        await asyncio.sleep(0)

        await meshcore._reader.handle_rx(bytearray(other))

        assert meshcore.original_frames == [other]
        event_handlers.on_channel_data.assert_not_awaited()
        assert await waiter is None, "an unrelated frame dispatched a placeholder"

    async def test_a_dispatcher_that_is_not_running_does_not_lose_the_image(self, monkeypatch):
        """Dispatch can fail (the dispatcher starts after connect); the picture
        must survive it -- the cost is one get_msg timeout, nothing more."""
        mc = _FakeMeshcore()  # dispatcher never started: dispatch raises
        handled = AsyncMock()
        monkeypatch.setattr(event_handlers, "on_channel_data", handled)
        event_handlers.install_channel_data_adapter(mc)

        await mc._reader.handle_rx(bytearray(FRAME_27))

        handled.assert_awaited_once_with(FRAME_27)


def _mc_returning(*events: Event):
    """A meshcore whose get_msg hands back these events, then NO_MORE_MSGS."""
    queue = list(events) + [Event(EventType.NO_MORE_MSGS, {})]

    async def get_msg(timeout=None):
        return queue.pop(0)

    return SimpleNamespace(commands=SimpleNamespace(get_msg=AsyncMock(side_effect=get_msg)))


class TestDrainingPastAnImage:
    async def test_the_drain_continues_past_a_placeholder(self, monkeypatch):
        """One image in the queue must not strand the messages behind it.

        The drain used to spend its 2 s timeout on the frame and break, leaving
        everything queued after the image waiting for the next poll -- an hour
        away unless the aggressive fallback is on.
        """
        stored: list[dict] = []

        async def store(mc, payload):
            stored.append(payload)

        monkeypatch.setattr(event_handlers, "on_channel_data", AsyncMock())
        import app.radio_sync as radio_sync

        monkeypatch.setattr(radio_sync, "_store_pending_channel_message", store)
        real_message = {"channel_idx": 0, "text": "behind the image"}
        mc = _mc_returning(
            Event(EventType.CHANNEL_MSG_RECV, grp_data_placeholder_payload()),
            Event(EventType.CHANNEL_MSG_RECV, real_message),
        )

        count = await drain_pending_messages(mc)

        assert stored == [real_message], "the message behind the image was stranded"
        assert count == 1, "the placeholder was counted as a caught message"
        assert mc.commands.get_msg.await_count == 3, "the drain stopped at the image"

    async def test_the_poll_does_not_report_an_image_as_a_missed_message(self, monkeypatch):
        """poll_for_messages' count feeds an audit that reports anything above
        zero as a message the event path missed, loudly. A frame the adapter
        already handled is not one."""
        import app.radio_sync as radio_sync

        monkeypatch.setattr(radio_sync, "_store_pending_channel_message", AsyncMock())
        mc = _mc_returning(Event(EventType.CHANNEL_MSG_RECV, grp_data_placeholder_payload()))

        count = await poll_for_messages(mc)

        assert count == 0, "an already-handled image frame was reported as missed"
        assert mc.commands.get_msg.await_count == 2, "the queue behind the image was not drained"


class TestTheCliFetchPath:
    async def test_the_shared_store_ignores_a_placeholder(self):
        """server_control's CLI fetch stores every CHANNEL_MSG_RECV it consumes
        through this function, so the guard has to live inside it too."""
        from app.radio_sync import _store_pending_channel_message

        # Nothing is patched: reaching any repository with this payload would
        # blow up on the disconnected test database, so returning cleanly IS the
        # assertion.
        await _store_pending_channel_message(SimpleNamespace(), grp_data_placeholder_payload())
