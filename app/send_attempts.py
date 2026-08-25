"""The configurable cap on direct-message send attempts.

A direct message is retransmitted until the recipient's ACK comes back or the
cap is reached. The cap lives in app settings so a user on a marginal link can
push it up without a rebuild; it is bounded because each attempt waits out the
firmware-suggested ACK window first, so a runaway value would keep one message
occupying an ``expected_ack`` slot for minutes.

The default matches the value that was hardcoded before the setting existed, so
upgrading changes nothing until the user moves the dial.

Channel messages do not use this: they have no ACK to wait for, and keep their
one-shot echo watchdog (``auto_resend_channel``) plus manual retry.
"""

MIN_MESSAGE_RETRIES = 1
DEFAULT_MAX_MESSAGE_RETRIES = 3
MAX_MESSAGE_RETRIES = 10


def clamp_message_retries(value: int | None) -> int:
    """Coerce a stored or requested attempt cap into the legal range.

    Used on the read path as well as the write path: a database written by a
    future version (or hand-edited) should degrade to a sane cap rather than let
    an out-of-range value reach the retry loop.
    """
    if value is None:
        return DEFAULT_MAX_MESSAGE_RETRIES
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_MESSAGE_RETRIES
    return max(MIN_MESSAGE_RETRIES, min(MAX_MESSAGE_RETRIES, numeric))
