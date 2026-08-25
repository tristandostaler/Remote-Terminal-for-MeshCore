"""Registry of the background tasks still working on an outgoing message.

A direct message keeps retransmitting until it is ACKed or its attempt cap runs
out, and a channel message may have an echo watchdog pending. Both live in
fire-and-forget tasks, so cancelling a send means finding that task -- hence this
registry, keyed by message id.

Cancelling is best-effort by nature: the transmission currently on air cannot be
recalled, only the ones not yet made. One task per message is tracked; a fresh
run (a manual retry, say) replaces the previous entry, and every task deregisters
itself on completion so the map does not grow with the message history.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

_tasks: dict[int, asyncio.Task] = {}


def register(message_id: int, task: asyncio.Task) -> None:
    """Track ``task`` as the in-flight send work for ``message_id``."""
    previous = _tasks.get(message_id)
    if previous is not None and previous is not task and not previous.done():
        # A new run supersedes the old one; leaving both alive would double the
        # airtime and race on the attempt counter.
        previous.cancel()
    _tasks[message_id] = task
    task.add_done_callback(lambda finished: _deregister(message_id, finished))


def _deregister(message_id: int, task: asyncio.Task) -> None:
    if _tasks.get(message_id) is task:
        del _tasks[message_id]


def is_active(message_id: int) -> bool:
    """Whether more transmissions are still scheduled for this message."""
    task = _tasks.get(message_id)
    return task is not None and not task.done()


def cancel(message_id: int) -> bool:
    """Stop any further transmissions of this message.

    Returns whether there was anything left to stop. ``False`` means the send had
    already finished (or was never retried), which callers treat as success --
    the desired end state is the same either way.
    """
    task = _tasks.pop(message_id, None)
    if task is None or task.done():
        return False
    task.cancel()
    logger.info("Cancelled in-flight send work for message %d", message_id)
    return True


def active_message_ids() -> set[int]:
    """Message ids with send work still pending. Diagnostics only."""
    return {message_id for message_id, task in _tasks.items() if not task.done()}


def reset() -> None:
    """Drop every tracked task without cancelling. Tests only."""
    _tasks.clear()
