"""Time windows for the statistics snapshot.

The statistics endpoint used to hardcode 24h (and 72h for the packet chart).
Every windowed metric now takes one of the keys below, so a single ``window``
query parameter drives the whole snapshot and the UI selector can offer more
than "yesterday".

``all`` means "no lower bound" — the caller passes ``None`` as the cutoff and
scans everything retained in the database.
"""

SECONDS_1H = 3600
SECONDS_1D = 86400
SECONDS_1W = 604800
SECONDS_30D = 2592000
SECONDS_90D = 7776000
SECONDS_1Y = 31536000

DEFAULT_STATS_WINDOW = "1d"

# Ordered oldest-narrowest first; the frontend renders the selector in this order.
STATS_WINDOWS: dict[str, int | None] = {
    "1h": SECONDS_1H,
    "1d": SECONDS_1D,
    "1w": SECONDS_1W,
    "1M": SECONDS_30D,
    "3M": SECONDS_90D,
    "1y": SECONDS_1Y,
    "all": None,
}

# Chart buckets are chosen to land near this many points: enough shape to read,
# few enough that a year of minute-resolution samples does not ship 500k rows.
TARGET_CHART_BUCKETS = 200

# Candidate bucket widths, ascending. Each is a round wall-clock unit so bucket
# boundaries fall somewhere a human would put them.
BUCKET_STEPS: tuple[int, ...] = (
    60,  # 1 min
    300,  # 5 min
    900,  # 15 min
    1800,  # 30 min
    3600,  # 1 hour
    10800,  # 3 hours
    21600,  # 6 hours
    43200,  # 12 hours
    86400,  # 1 day
    172800,  # 2 days
    604800,  # 1 week
    2592000,  # 30 days
)


def is_valid_window(window: str) -> bool:
    return window in STATS_WINDOWS


def window_seconds(window: str) -> int | None:
    """Seconds covered by ``window``, or ``None`` for the unbounded ``all``.

    Unknown keys fall back to the default rather than raising: the router
    validates user input, and internal callers should not blow up the whole
    snapshot over a typo.
    """
    if window not in STATS_WINDOWS:
        window = DEFAULT_STATS_WINDOW
    return STATS_WINDOWS[window]


def window_cutoff(window: str, now: int) -> int | None:
    """Unix timestamp the window starts at, or ``None`` for ``all``."""
    seconds = window_seconds(window)
    return None if seconds is None else now - seconds


def bucket_seconds_for_span(span_seconds: int, *, minimum: int = 60) -> int:
    """Pick a bucket width that keeps a chart near ``TARGET_CHART_BUCKETS``.

    ``minimum`` is the finest useful resolution for the series — there is no
    point bucketing 60-second noise-floor samples into 5-second slots.
    """
    if span_seconds <= 0:
        return minimum
    ideal = span_seconds / TARGET_CHART_BUCKETS
    for step in BUCKET_STEPS:
        if step >= ideal and step >= minimum:
            return step
    return max(BUCKET_STEPS[-1], minimum)
