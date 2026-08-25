"""The registry that makes an in-flight send cancellable."""

import asyncio

import pytest

from app.services import send_tracker


@pytest.fixture(autouse=True)
def _clean_registry():
    send_tracker.reset()
    yield
    send_tracker.reset()


async def _never_finishes() -> None:
    await asyncio.Event().wait()


class TestRegisterAndCancel:
    @pytest.mark.asyncio
    async def test_cancels_a_registered_task(self):
        task = asyncio.create_task(_never_finishes())
        send_tracker.register(1, task)

        assert send_tracker.is_active(1) is True
        assert send_tracker.cancel(1) is True

        with pytest.raises(asyncio.CancelledError):
            await task
        assert send_tracker.is_active(1) is False

    @pytest.mark.asyncio
    async def test_cancelling_an_unknown_message_reports_nothing_to_stop(self):
        """Not an error: the desired end state is the same either way."""
        assert send_tracker.cancel(999) is False

    @pytest.mark.asyncio
    async def test_cancelling_a_finished_send_reports_nothing_to_stop(self):
        async def done() -> None:
            return None

        task = asyncio.create_task(done())
        send_tracker.register(2, task)
        await task

        assert send_tracker.is_active(2) is False
        assert send_tracker.cancel(2) is False

    @pytest.mark.asyncio
    async def test_a_new_run_supersedes_the_previous_one(self):
        """Two live runs for one message would double the airtime and race on the counter."""
        first = asyncio.create_task(_never_finishes())
        send_tracker.register(3, first)

        second = asyncio.create_task(_never_finishes())
        send_tracker.register(3, second)

        with pytest.raises(asyncio.CancelledError):
            await first
        assert send_tracker.is_active(3) is True

        send_tracker.cancel(3)
        with pytest.raises(asyncio.CancelledError):
            await second

    @pytest.mark.asyncio
    async def test_registering_the_same_task_twice_does_not_cancel_it(self):
        task = asyncio.create_task(_never_finishes())
        send_tracker.register(4, task)
        send_tracker.register(4, task)

        assert task.cancelled() is False
        assert send_tracker.is_active(4) is True

        send_tracker.cancel(4)
        with pytest.raises(asyncio.CancelledError):
            await task


class TestRegistryHousekeeping:
    @pytest.mark.asyncio
    async def test_a_completed_task_deregisters_itself(self):
        """Otherwise the map would grow with the whole message history."""

        async def done() -> None:
            return None

        task = asyncio.create_task(done())
        send_tracker.register(5, task)
        await task
        await asyncio.sleep(0)  # let the done callback run

        assert send_tracker.active_message_ids() == set()

    @pytest.mark.asyncio
    async def test_reports_every_pending_message(self):
        tasks = [asyncio.create_task(_never_finishes()) for _ in range(3)]
        for index, task in enumerate(tasks):
            send_tracker.register(index, task)

        assert send_tracker.active_message_ids() == {0, 1, 2}

        for index in range(3):
            send_tracker.cancel(index)
        await asyncio.gather(*tasks, return_exceptions=True)
