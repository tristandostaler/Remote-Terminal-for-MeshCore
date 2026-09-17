"""Historical decrypt sweeps -- re-trying stored packets against keys we only have now.

A sweep walks every raw packet that never became a message (``message_id IS
NULL``) and tries one or more keys against it. Whatever decrypts is stored as
a message at the packet's original ``received_at``, tagged ``recovered_at`` so
the UI can tell a recovered message from one heard live.

One module owns the workflow because the callers -- the packets router, the
channels router's bulk room add, contact creation and the auto-decrypt-on-advert
path -- otherwise each grew their own copy of the scan loop.

**Sweeps run one at a time.** Every sweep is a full pass over the same table, so
running several concurrently only multiplies disk reads for the same rows.
A queue keeps the cost linear in the number of sweeps and leaves one honest
progress figure to report. Submitting while a sweep runs enqueues behind it.

Progress is broadcast as a throttled ``decrypt_progress`` WebSocket event: at
most one every :data:`PROGRESS_MIN_SECONDS`, and then only after
:data:`PROGRESS_PACKET_INTERVAL` more packets or a fresh decrypt. A sweep over
a large database examines tens of thousands of packets in a few seconds, and an
event per packet would cost far more than the scan itself.
"""

import asyncio
import contextlib
import hashlib
import logging
import time
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.decoder import (
    PayloadType,
    decrypt_group_text,
    derive_public_key,
    get_packet_payload_type,
    parse_packet,
    try_decrypt_dm,
)
from app.models import DecryptSweepProgress, DecryptSweepStatus, DecryptSweepTarget
from app.region_resolver import resolve_region
from app.repository import AppSettingsRepository, RawPacketRepository
from app.services.messages import (
    create_dm_message_from_decrypted,
    create_message_from_decrypted,
)
from app.websocket import broadcast_event, broadcast_success

logger = logging.getLogger(__name__)

# Emit at most one progress event per this many packets examined...
PROGRESS_PACKET_INTERVAL = 250
# ...and never more often than this, however fast the scan runs.
PROGRESS_MIN_SECONDS = 0.75

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"

KIND_CHANNELS = "channels"
KIND_CONTACT = "contact"


@dataclass(frozen=True)
class ChannelTarget:
    """One channel key to try, with the name to credit its finds to."""

    key_bytes: bytes
    key_hex: str
    name: str


@dataclass(frozen=True)
class ContactTarget:
    """One contact's DM key pair to try."""

    private_key: bytes
    public_key_bytes: bytes
    public_key_hex: str
    name: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.public_key_hex[:12]


@dataclass
class SweepSubmission:
    """What the caller learns from asking for a sweep."""

    started: bool
    job_id: str | None
    total_packets: int
    queued: int
    message: str


@dataclass
class _Job:
    job_id: str
    kind: str
    label: str
    total: int
    channel_targets: tuple[ChannelTarget, ...] = ()
    contact_target: ContactTarget | None = None
    status: str = STATUS_QUEUED
    processed: int = 0
    decrypted: int = 0
    per_target: dict[str, int] = field(default_factory=dict)
    target_names: dict[str, str] = field(default_factory=dict)
    started_at: int | None = None
    finished_at: int | None = None
    # Throttle bookkeeping -- never serialized.
    _last_emit_at: float = 0.0
    _last_emit_processed: int = 0
    _last_emit_decrypted: int = 0

    @property
    def target_count(self) -> int:
        return len(self.channel_targets) if self.contact_target is None else 1

    def credit(self, key: str, name: str) -> None:
        self.decrypted += 1
        self.per_target[key] = self.per_target.get(key, 0) + 1
        self.target_names[key] = name

    def snapshot(self, queued: int) -> DecryptSweepProgress:
        """Progress as the UI sees it.

        Only targets that actually found something are listed: a sweep across
        every known room would otherwise ship a row of zeroes per room on every
        tick, which is both the bulk of the payload and none of the news. A
        contact sweep is the exception -- it has one target and names it up
        front, so the UI can tell whose conversation the sweep belongs to.
        ``total`` is taken at submit time, so live traffic during a long sweep
        can push ``processed`` past it -- report the larger of the two rather
        than a bar that reads over 100%.
        """
        return DecryptSweepProgress(
            job_id=self.job_id,
            kind=self.kind,
            label=self.label,
            status=self.status,
            total=max(self.total, self.processed),
            processed=self.processed,
            decrypted=self.decrypted,
            target_count=self.target_count,
            targets=[
                DecryptSweepTarget(
                    kind="contact" if self.contact_target is not None else "channel",
                    key=key,
                    name=self.target_names.get(key, key[:12]),
                    decrypted=found,
                )
                for key, found in sorted(
                    self.per_target.items(), key=lambda item: (-item[1], item[0])
                )
            ],
            started_at=self.started_at,
            finished_at=self.finished_at,
            queued=queued,
        )


class _SweepRunner:
    """Serial queue of sweeps plus the progress everyone reads."""

    def __init__(self) -> None:
        self._pending: deque[_Job] = deque()
        self._active: _Job | None = None
        self._last: DecryptSweepProgress | None = None
        self._worker: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    # -- submission -------------------------------------------------

    def submit(self, job: _Job) -> SweepSubmission:
        # Sweeps ahead of this one: whatever is already waiting, plus the one
        # currently scanning. Zero means this sweep starts immediately.
        ahead = len(self._pending) + (1 if self._active is not None else 0)
        self._pending.append(job)
        self._ensure_worker()
        self._idle.clear()
        logger.info(
            "Queued %s decrypt sweep %s (%s) over %d packets; %d ahead of it",
            job.kind,
            job.job_id,
            job.label,
            job.total,
            ahead,
        )
        return SweepSubmission(
            started=True,
            job_id=job.job_id,
            total_packets=job.total,
            queued=ahead,
            message=(
                f"Started decrypt of {job.total} packet{'s' if job.total != 1 else ''} "
                "in background"
                if ahead == 0
                else f"Queued behind {ahead} running sweep{'s' if ahead != 1 else ''}"
            ),
        )

    def _ensure_worker(self) -> None:
        """(Re)start the worker for the loop we are running on.

        The runner is a module global while the event loop is not: tests build a
        fresh loop per case, and a task left over from a closed one can never
        drain the queue.
        """
        loop = asyncio.get_running_loop()
        if self._worker is not None and not self._worker.done() and self._loop is loop:
            return
        self._loop = loop
        self._idle = asyncio.Event()
        self._worker = loop.create_task(self._drain())

    async def _drain(self) -> None:
        while self._pending:
            job = self._pending.popleft()
            self._active = job
            try:
                await _run_job(job, queued=len(self._pending))
            except Exception:
                job.status = STATUS_FAILED
                job.finished_at = int(time.time())
                logger.exception("Decrypt sweep %s failed", job.job_id)
                _emit(job, queued=len(self._pending))
            finally:
                self._last = job.snapshot(queued=len(self._pending))
                self._active = None
        self._idle.set()

    # -- reads ------------------------------------------------------

    def status(self) -> DecryptSweepStatus:
        active = self._active
        return DecryptSweepStatus(
            active=active.snapshot(queued=len(self._pending)) if active else None,
            last=self._last,
            queued=len(self._pending),
        )

    def queued_count(self) -> int:
        return len(self._pending)

    async def wait_until_idle(self, timeout: float = 30.0) -> None:
        """Block until the queue drains. For tests and shutdown, not request paths."""
        if self._worker is None:
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)

    def reset(self) -> None:
        """Drop all state. Tests only -- a live queue is never thrown away."""
        if self._worker is not None:
            self._worker.cancel()
        self._pending.clear()
        self._active = None
        self._last = None
        self._worker = None
        self._loop = None
        self._idle = asyncio.Event()
        self._idle.set()


_runner = _SweepRunner()


def _emit(job: _Job, *, queued: int) -> None:
    job._last_emit_at = time.monotonic()
    job._last_emit_processed = job.processed
    job._last_emit_decrypted = job.decrypted
    broadcast_event("decrypt_progress", job.snapshot(queued).model_dump(), realtime=False)


async def _tick(job: _Job, *, queued: int) -> None:
    """Yield on a fixed cadence; emit progress only when enough has changed.

    The scan is synchronous CPU work between database batches, so a run of
    non-matching packets would otherwise hold the event loop for the whole
    batch. The yield is unconditional every ``PROGRESS_PACKET_INTERVAL``
    packets; the event on top of it is what the throttle rations.
    """
    if job.processed % PROGRESS_PACKET_INTERVAL == 0:
        await asyncio.sleep(0)
    now = time.monotonic()
    if now - job._last_emit_at < PROGRESS_MIN_SECONDS:
        return
    if (
        job.processed - job._last_emit_processed < PROGRESS_PACKET_INTERVAL
        and job.decrypted == job._last_emit_decrypted
    ):
        return
    _emit(job, queued=queued)


async def _run_job(job: _Job, *, queued: int) -> None:
    job.status = STATUS_RUNNING
    job.started_at = int(time.time())
    _emit(job, queued=queued)

    if job.contact_target is not None:
        await _scan_for_contact(job, queued=queued)
    else:
        await _scan_for_channels(job, queued=queued)

    job.status = STATUS_COMPLETE
    job.finished_at = int(time.time())
    _emit(job, queued=queued)

    logger.info(
        "Decrypt sweep %s complete: %d/%d packets decrypted across %d target(s)",
        job.job_id,
        job.decrypted,
        job.processed,
        len(job.per_target),
    )
    _announce(job)


def _announce(job: _Job) -> None:
    """Toast the outcome. Silence when a sweep found nothing is deliberate."""
    if job.decrypted == 0:
        return
    plural = "s" if job.decrypted != 1 else ""
    if job.contact_target is not None or len(job.per_target) == 1:
        name = next(iter(job.target_names.values()), job.label)
        broadcast_success(
            f"Historical decrypt complete for {name}",
            f"Recovered {job.decrypted} message{plural}",
        )
        return
    rooms = len(job.per_target)
    broadcast_success(
        "Historical decrypt complete",
        f"Recovered {job.decrypted} message{plural} across {rooms} "
        f"conversation{'s' if rooms != 1 else ''}",
    )


def _index_by_channel_hash(targets: Sequence[ChannelTarget]) -> dict[int, list[ChannelTarget]]:
    """Group keys by the byte a GroupText packet carries to name its channel.

    That byte is the first of ``sha256(key)``, so at most one key in 256 can
    own any given packet. Indexing on it turns "try every key against every
    packet" into one dict lookup per packet and an AES attempt only on the rare
    match -- an all-rooms sweep over a large database is otherwise dominated by
    hashing and parsing that could never succeed.
    """
    by_hash: dict[int, list[ChannelTarget]] = {}
    for target in targets:
        by_hash.setdefault(hashlib.sha256(target.key_bytes).digest()[0], []).append(target)
    return by_hash


async def _scan_for_channels(job: _Job, *, queued: int) -> None:
    known_regions = (await AppSettingsRepository.get()).known_regions
    recovered_at = int(time.time())
    by_hash = _index_by_channel_hash(job.channel_targets)

    async for (
        packet_id,
        packet_data,
        packet_timestamp,
    ) in RawPacketRepository.stream_all_undecrypted():
        job.processed += 1

        packet_info = parse_packet(packet_data)
        if (
            packet_info is None
            or packet_info.payload_type != PayloadType.GROUP_TEXT
            or not packet_info.payload
        ):
            await _tick(job, queued=queued)
            continue

        for target in by_hash.get(packet_info.payload[0], ()):
            result = decrypt_group_text(packet_info.payload, target.key_bytes)
            if result is None:
                continue

            transport_code: int | None = None
            region: str | None = None
            if packet_info.transport_codes is not None:
                transport_code = packet_info.transport_codes[0]
                region = resolve_region(
                    int(packet_info.payload_type),
                    packet_info.payload,
                    transport_code,
                    known_regions,
                )

            msg_id = await create_message_from_decrypted(
                packet_id=packet_id,
                channel_key=target.key_hex,
                channel_name=target.name,
                sender=result.sender,
                message_text=result.message,
                timestamp=result.timestamp,
                received_at=packet_timestamp,
                path=packet_info.path.hex(),
                path_len=packet_info.path_length,
                realtime=False,  # a sweep is not live traffic: no fanout, no bots
                broadcast_fn=broadcast_event,
                transport_code=transport_code,
                region=region,
                recovered_at=recovered_at,
            )
            if msg_id is not None:
                job.credit(target.key_hex, target.name)
            # One packet belongs to one channel; a second key cannot also own it.
            break

        await _tick(job, queued=queued)


async def _scan_for_contact(job: _Job, *, queued: int) -> None:
    target = job.contact_target
    assert target is not None
    # Name the target up front. A channel sweep can carry fifty keys and only
    # reports the ones that hit, but a DM sweep has exactly one, and the UI
    # needs to know whose conversation this sweep belongs to from the start.
    job.per_target.setdefault(target.public_key_hex, 0)
    job.target_names[target.public_key_hex] = target.display_name
    our_public_key_bytes = derive_public_key(target.private_key)
    our_first_byte = format(our_public_key_bytes[0], "02x").lower()
    recovered_at = int(time.time())

    async for (
        packet_id,
        packet_data,
        packet_timestamp,
    ) in RawPacketRepository.stream_all_undecrypted():
        job.processed += 1
        if get_packet_payload_type(packet_data) != PayloadType.TEXT_MESSAGE:
            await _tick(job, queued=queued)
            continue

        # our_public_key=None disables the outbound hash check in try_decrypt_dm,
        # leaving only the inbound one (src_hash == their first byte). Outgoing DMs
        # are stored by the send endpoint, so a sweep only needs to recover inbound.
        result = try_decrypt_dm(
            packet_data,
            target.private_key,
            target.public_key_bytes,
            our_public_key=None,
        )
        if result is None:
            await _tick(job, queued=queued)
            continue

        # Direction from both hashes, for the 1/256 case where our first public
        # key byte matches the contact's and an outgoing packet decrypts too.
        outgoing = (
            result.src_hash.lower() == our_first_byte and result.dest_hash.lower() != our_first_byte
        )

        packet_info = parse_packet(packet_data)
        msg_id = await create_dm_message_from_decrypted(
            packet_id=packet_id,
            decrypted=result,
            their_public_key=target.public_key_hex,
            our_public_key=our_public_key_bytes.hex(),
            received_at=packet_timestamp,
            path=packet_info.path.hex() if packet_info else None,
            path_len=packet_info.path_length if packet_info else None,
            outgoing=outgoing,
            realtime=False,
            broadcast_fn=broadcast_event,
            recovered_at=recovered_at,
        )
        if msg_id is not None:
            job.credit(target.public_key_hex, target.display_name)

        await _tick(job, queued=queued)


def _no_packets(message: str = "No undecrypted packets to process") -> SweepSubmission:
    return SweepSubmission(
        started=False, job_id=None, total_packets=0, queued=_runner.queued_count(), message=message
    )


def _channel_job(targets: Sequence[ChannelTarget], label: str | None, total: int) -> _Job:
    return _Job(
        job_id=uuid.uuid4().hex[:12],
        kind=KIND_CHANNELS,
        label=label or (targets[0].name if len(targets) == 1 else f"{len(targets)} conversations"),
        total=total,
        channel_targets=tuple(targets),
    )


def _contact_job(target: ContactTarget, total: int) -> _Job:
    return _Job(
        job_id=uuid.uuid4().hex[:12],
        kind=KIND_CONTACT,
        label=target.display_name,
        total=total,
        contact_target=target,
    )


async def run_channel_sweep(
    targets: Sequence[ChannelTarget], label: str | None = None
) -> DecryptSweepProgress:
    """Scan now, in the caller's task, and return the finished progress.

    The queue is what request handlers want; this is what a caller that needs
    the result in hand (a test, a script) can await. It bypasses the queue, so
    it neither waits for a running sweep nor shows up in :func:`get_status`.
    """
    total = await RawPacketRepository.get_undecrypted_count()
    job = _channel_job(targets, label, total)
    await _run_job(job, queued=0)
    return job.snapshot(queued=0)


async def run_contact_sweep(target: ContactTarget) -> DecryptSweepProgress:
    """Scan now for one contact's DMs. See :func:`run_channel_sweep`."""
    total = await RawPacketRepository.get_undecrypted_count()
    job = _contact_job(target, total)
    await _run_job(job, queued=0)
    return job.snapshot(queued=0)


async def submit_channel_sweep(
    targets: Sequence[ChannelTarget], label: str | None = None
) -> SweepSubmission:
    """Queue a sweep of every undecrypted packet against ``targets``.

    All keys are tried against each packet in one pass, so adding twenty rooms
    costs one scan, not twenty.
    """
    if not targets:
        return _no_packets("No channel keys to try")

    total = await RawPacketRepository.get_undecrypted_count()
    if total == 0:
        return _no_packets()

    return _runner.submit(_channel_job(targets, label, total))


async def submit_contact_sweep(target: ContactTarget) -> SweepSubmission:
    """Queue a sweep of every undecrypted text-message packet against one contact."""
    total = await RawPacketRepository.get_undecrypted_count()
    if total == 0:
        return _no_packets()

    return _runner.submit(_contact_job(target, total))


def get_status() -> DecryptSweepStatus:
    """Current sweep, last finished sweep, and how many are waiting."""
    return _runner.status()


async def wait_until_idle(timeout: float = 30.0) -> None:
    """Block until every queued sweep has finished. Tests and shutdown only."""
    await _runner.wait_until_idle(timeout=timeout)


def stop_sweeps() -> None:
    """Cancel the running sweep and drop the queue.

    Used at shutdown, where the database is about to close under a scan, and by
    tests between cases so a sweep never outlives the database it was reading.
    """
    _runner.reset()
