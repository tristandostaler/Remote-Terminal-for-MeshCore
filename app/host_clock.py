"""Is this server's own clock right?

The app pushes its wall clock to repeaters (CLI ``time <epoch>``), and the
repeater firmware only ever applies that *forward*. One push from a host whose
clock has jumped ahead -- a Docker Desktop VM after the laptop slept, a board
that booted before NTP settled -- is permanent on every repeater it reached
until someone reboots them (see ``radio_sync.fix_forward_clock``). So before
pushing, ask something that is not this machine.

References, in order (each disabled by an empty setting):

1. SNTP over UDP/123 against ``MESHCORE_HOST_CLOCK_NTP_SERVER``. Millisecond
   accuracy, no dependency beyond asyncio.
2. The ``Date`` header of a HEAD request to ``MESHCORE_HOST_CLOCK_HTTP_REFERENCE``
   for networks that block NTP. Truncated to the second, which is plenty to
   catch a clock that is minutes or days out.

With neither reachable the clock is *unverified*: pushes proceed as before,
except when the wall clock has visibly stepped against the monotonic clock
since the last verified check. A step is exactly what a bad VM clock looks
like from the inside, so it holds pushes for ``STEP_QUARANTINE_SECONDS`` and
then, still unverified, lets them resume -- an offline mesh server that only
ever corrected its clock forward at boot must not be locked out for good.

Results are cached for ``CACHE_SECONDS``; the cache is bypassed when a step is
detected, so a jump is noticed at the next push, not ten minutes later.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import struct
import time
from dataclasses import dataclass

from app.config import settings
from app.models import HostClockStatus

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
# Below this a wall/monotonic disagreement is scheduler jitter, not a step.
STEP_TOLERANCE_SECONDS = 5.0
STEP_QUARANTINE_SECONDS = 3600
NTP_TIMEOUT_SECONDS = 3.0
HTTP_TIMEOUT_SECONDS = 5.0
# Seconds between the NTP epoch (1900) and the Unix epoch (1970).
_NTP_EPOCH_OFFSET = 2_208_988_800

# Indirection so tests can steer both clocks without touching asyncio's own.
_wall = time.time
_mono = time.monotonic


@dataclass
class _State:
    status: HostClockStatus | None = None
    checked_mono: float = 0.0
    # Anchor for step detection. Re-anchored on every verified check, and after
    # an unverified quarantine has run its course.
    anchor_wall: float = 0.0
    anchor_mono: float = 0.0
    unverified_step_since: float | None = None  # monotonic


_state = _State(anchor_wall=_wall(), anchor_mono=_mono())
_lock = asyncio.Lock()


def _step_since_anchor() -> float:
    """How far the wall clock moved against the monotonic clock since the anchor."""
    return (_wall() - _state.anchor_wall) - (_mono() - _state.anchor_mono)


def _re_anchor() -> None:
    _state.anchor_wall = _wall()
    _state.anchor_mono = _mono()


def _reset_for_tests() -> None:
    """Forget cached results and re-anchor step detection."""
    _state.status = None
    _state.checked_mono = 0.0
    _state.unverified_step_since = None
    _re_anchor()


def _ntp_to_unix(raw: bytes) -> float:
    seconds, fraction = struct.unpack("!II", raw)
    return seconds - _NTP_EPOCH_OFFSET + fraction / 2**32


class _SntpProtocol(asyncio.DatagramProtocol):
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.reply: asyncio.Future[tuple[bytes, float]] = loop.create_future()

    def datagram_received(self, data: bytes, addr) -> None:
        if not self.reply.done():
            self.reply.set_result((data, _wall()))

    def error_received(self, exc: Exception) -> None:
        if not self.reply.done():
            self.reply.set_exception(exc)

    def connection_lost(self, exc: Exception | None) -> None:
        if exc is not None and not self.reply.done():
            self.reply.set_exception(exc)


async def query_sntp(
    server: str, *, port: int = 123, timeout: float = NTP_TIMEOUT_SECONDS
) -> float | None:
    """Host clock minus *server*'s clock in seconds, or ``None`` if unreachable."""
    loop = asyncio.get_running_loop()
    try:
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _SntpProtocol(loop), remote_addr=(server, port)
        )
    except OSError as exc:
        logger.debug("Host clock: cannot reach NTP server %s: %s", server, exc)
        return None
    try:
        request = bytearray(48)
        request[0] = 0x1B  # LI=0, version 3, mode 3 (client)
        t0 = _wall()
        transport.sendto(bytes(request))
        data, t3 = await asyncio.wait_for(protocol.reply, timeout)
    except (TimeoutError, OSError) as exc:
        logger.debug("Host clock: no NTP reply from %s: %s", server, exc)
        return None
    finally:
        transport.close()

    if len(data) < 48 or data[1] == 0:  # stratum 0 is a kiss-of-death packet
        logger.debug("Host clock: unusable NTP reply from %s", server)
        return None
    t1 = _ntp_to_unix(data[32:40])  # server received our request
    t2 = _ntp_to_unix(data[40:48])  # server sent its reply
    reference_minus_host = ((t1 - t0) + (t2 - t3)) / 2
    return -reference_minus_host


async def query_http_date(
    url: str, *, timeout: float = HTTP_TIMEOUT_SECONDS, transport=None
) -> float | None:
    """Host clock minus the ``Date`` header of a HEAD to *url*, or ``None``."""
    import httpx

    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, transport=transport
        ) as client:
            t0 = _wall()
            response = await client.head(url)
            t3 = _wall()
    except Exception as exc:
        logger.debug("Host clock: HTTP reference %s unreachable: %s", url, exc)
        return None
    date_header = response.headers.get("date")
    if not date_header:
        logger.debug("Host clock: HTTP reference %s sent no Date header", url)
        return None
    try:
        reference = email.utils.parsedate_to_datetime(date_header).timestamp()
    except (TypeError, ValueError):
        logger.debug("Host clock: unparseable Date header from %s: %r", url, date_header)
        return None
    return (t0 + t3) / 2 - reference


async def _measure_offset() -> tuple[float | None, str, str]:
    """(host minus reference, source, reference) from the first reference that answers."""
    if settings.host_clock_ntp_server:
        offset = await query_sntp(settings.host_clock_ntp_server)
        if offset is not None:
            return offset, "ntp", settings.host_clock_ntp_server
    if settings.host_clock_http_reference:
        offset = await query_http_date(settings.host_clock_http_reference)
        if offset is not None:
            return offset, "http", settings.host_clock_http_reference
    return None, "none", ""


def _describe(offset: float | None, source: str, trusted: bool, step: float) -> str:
    if offset is not None:
        direction = "ahead of" if offset > 0 else "behind"
        via = "NTP" if source == "ntp" else "the HTTP reference"
        if trusted:
            return f"Server clock verified via {via}: {abs(offset):.1f}s {direction} the reference."
        return (
            f"Server clock is {abs(offset):.0f}s ({abs(offset) / 3600:.1f}h) {direction} the "
            f"reference (via {via}). Clock pushes to repeaters are refused until it is fixed."
        )
    if trusted:
        note = "no time reference is reachable"
        if step:
            note += f"; wall clock has moved {step:+.0f}s against the monotonic clock"
        return f"Server clock unverified ({note}). Clock pushes proceed unchecked."
    return (
        f"Server clock stepped {step:+.0f}s since it was last checked and no time reference "
        "is reachable to confirm the new time. Clock pushes to repeaters are held for "
        f"{STEP_QUARANTINE_SECONDS // 60} minutes."
    )


async def check_host_clock(*, force: bool = False) -> HostClockStatus:
    """Current verdict on this server's clock, cached unless *force* or a step."""
    async with _lock:
        now_mono = _mono()
        step = _step_since_anchor()
        stepped = abs(step) > STEP_TOLERANCE_SECONDS
        cached = _state.status
        fresh = cached is not None and now_mono - _state.checked_mono < CACHE_SECONDS
        if cached is not None and fresh and not force and not stepped:
            return cached

        offset, source, reference = await _measure_offset()
        threshold = settings.host_clock_max_offset_seconds

        if offset is not None:
            verified = True
            trusted = abs(offset) <= threshold
            _re_anchor()
            _state.unverified_step_since = None
            reported_step = round(step, 1)
        else:
            verified = False
            if stepped and _state.unverified_step_since is None:
                _state.unverified_step_since = now_mono
            since = _state.unverified_step_since
            if since is not None and now_mono - since >= STEP_QUARANTINE_SECONDS:
                # Quarantine served with nothing to check against: stop holding
                # pushes, and measure future steps from here.
                _state.unverified_step_since = None
                _re_anchor()
                since = None
                step = 0.0
            trusted = since is None
            reported_step = round(step, 1)

        status = HostClockStatus(
            checked_at=int(_wall()),
            trusted=trusted,
            verified=verified,
            offset_seconds=None if offset is None else round(offset, 3),
            source=source,  # type: ignore[arg-type]
            reference=reference,
            step_seconds=reported_step,
            threshold_seconds=threshold,
            message=_describe(offset, source, trusted, reported_step),
        )

        previous = cached
        if not trusted:
            logger.warning("Host clock: %s", status.message)
        elif previous is None or previous.trusted != trusted or previous.verified != verified:
            logger.info("Host clock: %s", status.message)
        else:
            logger.debug("Host clock: %s", status.message)

        _state.status = status
        _state.checked_mono = now_mono
        return status
