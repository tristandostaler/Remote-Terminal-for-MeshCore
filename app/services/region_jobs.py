"""Background region brute-force job.

A sweep can run for minutes or hours, so it must not live inside a request. The
router starts it here and returns immediately; clients poll ``current_job`` for
progress and partial results, and can cancel. One job runs at a time (the work
is CPU-bound) and only the latest job is kept, in memory, so a page reload can
pick a running sweep back up but a server restart loses it.
"""

import asyncio
import threading
import time
import uuid
from dataclasses import dataclass, field

from app.services import region_tools


@dataclass
class RegionJob:
    id: str
    max_seconds: float | None
    scoped_packets: int = 0
    tested_packets: int = 0
    candidates_total: int = 0
    tried: int = 0
    status: str = "running"  # running | completed | timed_out | cancelled | failed
    error: str | None = None
    results: list[region_tools.GuessResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    cancel: threading.Event = field(default_factory=threading.Event)

    @property
    def elapsed(self) -> float:
        return (self.finished_at or time.monotonic()) - self.started_at

    @property
    def running(self) -> bool:
        return self.status == "running"


_current: RegionJob | None = None


def current_job() -> RegionJob | None:
    return _current


def cancel_job(job_id: str) -> RegionJob | None:
    job = _current
    if job is None or job.id != job_id:
        return None
    job.cancel.set()
    return job


def start_job(
    *,
    rows: list,
    known: list[str],
    user_names: list[str],
    min_letters: int | None,
    max_letters: int | None,
    max_packets: int,
    min_hits: int,
    max_seconds: float | None,
) -> RegionJob:
    """Start a sweep in the background. Raises ``RuntimeError`` if one is running."""
    global _current
    if _current is not None and _current.running:
        raise RuntimeError("A region brute force is already running")
    job = RegionJob(id=uuid.uuid4().hex, max_seconds=max_seconds or None)
    _current = job
    job.candidates_total = len(
        list(region_tools.expand_candidates(user_names, min_letters=None, max_letters=None))
    )
    if min_letters is not None and max_letters is not None:
        job.candidates_total += region_tools.brute_force_total(min_letters, max_letters)

    def work() -> None:
        try:
            packets, scoped_total = region_tools.scoped_packets_from_rows(
                rows, known, limit=max_packets
            )
            job.scoped_packets = scoped_total
            job.tested_packets = len(packets)
            candidates = region_tools.expand_candidates(
                user_names, min_letters=min_letters, max_letters=max_letters
            )
            known_lower = {name.lower() for name in known}

            def on_progress(tried: int, found: list[region_tools.GuessResult]) -> None:
                job.tried = tried
                job.results = [r for r in found if r.region.lower() not in known_lower]

            results, timed_out, tried = region_tools.guess_regions(
                packets,
                candidates,
                min_hits=min_hits,
                max_seconds=max_seconds,
                cancel=job.cancel,
                on_progress=on_progress,
            )
            job.tried = tried
            job.results = [r for r in results if r.region.lower() not in known_lower]
            job.status = (
                "cancelled" if job.cancel.is_set() else "timed_out" if timed_out else "completed"
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the poller
            job.status = "failed"
            job.error = str(exc)
        finally:
            job.finished_at = time.monotonic()

    asyncio.get_running_loop().run_in_executor(None, work)
    return job
