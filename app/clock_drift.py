"""Clock drift measured from the timestamp inside every advertisement.

A MeshCore advert payload is ``pub_key(32) || timestamp(4 LE) || signature(64)
|| app_data``. The timestamp is the sender's own wall clock at the moment it
built the advert, and it sits inside the Ed25519-signed message, so it cannot
be altered in flight without failing the signature check the packet pipeline
already enforces. Comparing it against our receive time is therefore a free,
passive measurement of every node's clock — no login, no CLI, no cooperation
from the far end.

Sign convention: ``drift = advert_timestamp - observed_at``. Positive means the
node's clock is **ahead** of this server's; negative means **behind**.

Two biases are inherent to the measurement and are handled rather than hidden:

- **Propagation delay** only ever pushes drift negative — the advert was built
  before it was heard. Per-hop LoRa airtime is seconds, so a node whose clock
  is perfect reads as a small negative. That is why a bucket keeps its
  *maximum* drift (:func:`app.repository.contacts.ContactClockDriftRepository`)
  and why zero-hop samples are counted separately: a direct arrival has almost
  no delay to subtract.
- **Our own clock.** Every number here is relative to this server. If the
  server clock is wrong, the whole mesh reads as drifted in the same direction,
  which is exactly why the statistics panel surfaces the signed *median* across
  repeaters — one node off is that node's problem, all of them off by the same
  amount is ours.
"""

from collections.abc import Sequence
from typing import Literal

# Samples are folded into hourly buckets on write. A node advertising every few
# minutes would otherwise store hundreds of rows a day for a measurement whose
# interesting timescale is hours-to-weeks; an hour of resolution still shows a
# clock jump the moment it happens.
DRIFT_BUCKET_SECONDS = 3600

# Nothing is ever deleted. A drifting clock is one of the few things here worth
# looking at over years — an oscillator ageing, a repeater that only misbehaves
# in summer — and none of that survives a retention cut.
#
# Instead, buckets older than this are folded down to one per day (see
# ``ContactClockDriftRepository.compact``). Hourly resolution is what shows a
# clock *jumping*, which is only actionable while it is recent; for a
# years-old trend a daily point says everything a day of hourly ones did, at a
# twenty-fourth of the rows. That keeps a node's whole history at roughly 60 kB
# a year instead of 1.4 MB, so "keep everything" stays affordable rather than
# becoming a reason to start deleting later.
DRIFT_FULL_RESOLUTION_SECONDS = 90 * 86400

DAY_SECONDS = 86400

# Severity bands. 60s is the same threshold the ``ping`` bot uses before it
# bothers reporting a sender's offset, so the two surfaces agree on what counts
# as "close enough".
IN_SYNC_SECONDS = 60
MINOR_SECONDS = 300
MAJOR_SECONDS = 3600

DriftSeverity = Literal["in_sync", "minor", "major", "severe"]

# An advert timestamp below this is not a drifted clock, it is a clock that was
# never set — firmware boots at or near zero and stays there until something
# hands it the time. Reporting "56 years behind" as drift is technically true
# and practically useless, so these are counted separately.
UNSET_CLOCK_BEFORE = 1_000_000_000  # 2001-09-09

# A rate needs a real lever arm. Two samples an hour apart can "prove" any
# slope you like, because the noise floor of a single measurement (propagation
# delay, bucket edges) is seconds.
MIN_RATE_SAMPLES = 3
MIN_RATE_SPAN_SECONDS = 6 * 3600

# Below a minute a day a clock is holding position as far as anyone cares -- it
# would take a month to leave the in-sync band. Above it, the offset is going to
# come back after a resync, which is a different repair, so only these belong in
# a "still moving" ranking.
NOTABLE_RATE_SECONDS_PER_DAY = 60

# Signed histogram edges in seconds, ascending. Bin i holds drifts in
# [edges[i-1], edges[i]) with the two open-ended tails at either end.
_HISTOGRAM_EDGES: tuple[int, ...] = (
    -86400,
    -3600,
    -300,
    -60,
    60,
    300,
    3600,
    86400,
)

_HISTOGRAM_LABELS: tuple[str, ...] = (
    "< -1d",
    "-1d…-1h",
    "-1h…-5m",
    "-5m…-1m",
    "±1m",
    "1m…5m",
    "5m…1h",
    "1h…1d",
    "> 1d",
)


def bucket_start(observed_at: int) -> int:
    """Floor a receive timestamp to its storage bucket."""
    return (observed_at // DRIFT_BUCKET_SECONDS) * DRIFT_BUCKET_SECONDS


def day_start(timestamp: int) -> int:
    """Floor a timestamp to its UTC day, the resolution old buckets fold to."""
    return (timestamp // DAY_SECONDS) * DAY_SECONDS


def classify_drift(drift_seconds: int | float) -> DriftSeverity:
    """Band a drift value by magnitude, ignoring direction."""
    magnitude = abs(drift_seconds)
    if magnitude <= IN_SYNC_SECONDS:
        return "in_sync"
    if magnitude <= MINOR_SECONDS:
        return "minor"
    if magnitude <= MAJOR_SECONDS:
        return "major"
    return "severe"


def is_unset_clock(advert_timestamp: int) -> bool:
    """True when the advert's clock was never set rather than merely wrong."""
    return advert_timestamp < UNSET_CLOCK_BEFORE


def histogram_labels() -> tuple[str, ...]:
    """Bin labels for the signed drift distribution, ascending."""
    return _HISTOGRAM_LABELS


def histogram_bin(drift_seconds: int | float) -> int:
    """Index of the signed histogram bin ``drift_seconds`` falls in."""
    for index, edge in enumerate(_HISTOGRAM_EDGES):
        if drift_seconds < edge:
            return index
    return len(_HISTOGRAM_EDGES)


def drift_rate_per_day(points: Sequence[tuple[int, int]]) -> float | None:
    """Least-squares drift trend in seconds per day, or ``None`` if unprovable.

    ``points`` are ``(observed_at, drift_seconds)`` pairs in any order. The
    slope is what separates the two failure modes that look identical in a
    single reading: a clock set wrong once sits at a constant offset (slope ~0
    and no amount of waiting fixes it), while a bad oscillator or a missing
    RTC walks away steadily (slope large, and it will be worse tomorrow).

    Returns ``None`` rather than a meaningless number when there are too few
    samples or they span too little time — see ``MIN_RATE_*``.
    """
    if len(points) < MIN_RATE_SAMPLES:
        return None

    times = [float(t) for t, _ in points]
    span = max(times) - min(times)
    if span < MIN_RATE_SPAN_SECONDS:
        return None

    drifts = [float(d) for _, d in points]
    n = float(len(points))
    mean_t = sum(times) / n
    mean_d = sum(drifts) / n

    covariance = sum((t - mean_t) * (d - mean_d) for t, d in zip(times, drifts, strict=True))
    variance = sum((t - mean_t) ** 2 for t in times)
    if variance <= 0:
        return None

    # Slope is seconds of drift per second of wall clock; a day of wall clock
    # is the unit an operator can actually reason about.
    return round((covariance / variance) * 86400.0, 2)


def median(values: Sequence[float]) -> float:
    """Plain median. Used instead of the mean wherever one unset clock would
    otherwise drag a mesh-wide summary into meaninglessness."""
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0
