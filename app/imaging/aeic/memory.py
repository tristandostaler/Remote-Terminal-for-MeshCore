"""How much memory reconstruction needs, and how much this host actually has.

Stdlib only, like :mod:`app.imaging.aeic.constants` -- the settings panel and the
decode preflight both ask these questions on hosts where onnxruntime is not even
installed.

## The measured numbers

The synthesis pass is the whole cost; the entropy loop before it peaks at
~0.35 GiB. Measured under hard cgroup caps with no swap (aarch64 Linux,
onnxruntime 1.29, the ft32 bundle), running :mod:`decode_worker` end to end on a
real 123-byte bitstream -- so the numbers include Python, numpy and the app's own
imports, which a bare graph benchmark does not:

===============  ==================  =========================
memory cap       weights prepacked   weights memory-mapped
===============  ==================  =========================
2048 MiB         completes           completes
1792 MiB         **OOM-killed**      completes
1408 MiB         **OOM-killed**      completes
1280 MiB         **OOM-killed**      **OOM-killed**
===============  ==================  =========================

The two columns produce a byte-identical PNG (same SHA-256), so this costs
nothing but ~0.4 s. Memory-mapping the 832 MiB of int8 weights (see
``session.disable_prepacking`` in :mod:`onnx_backend`) is what moves the
requirement from ~2 GiB to ~1.4 GiB: the weights become clean file-backed pages
the kernel can evict and re-read instead of an anonymous heap copy it can only
kill. Below ~1.3 GiB nothing helps -- what is left is the activations of a
512x512 pass, and no session option makes those smaller.

The kill arrives as SIGKILL, which the parent reads back as exit 137 and reports
as "out of memory" rather than as a broken picture. That path is measured too,
not assumed: the 1280 MiB row above is where it was observed.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

ROOT = Path("/")
"""Where the kernel's answers live. A parameter so the tests can supply their
own /proc and /sys instead of asserting against whatever host they run on."""

DECODE_PEAK_BYTES = 1_400_000_000
"""What a decode needs free: the smallest cap the worker completed under."""

DECODE_REFUSE_BELOW_BYTES = 900_000_000
"""Below this, refuse rather than attempt.

Deliberately well under :data:`DECODE_PEAK_BYTES`. Where exactly a host falls
between 1280 and 1408 MiB depends on its page cache and how much of the weights
stay resident, so a host in that band gets to try -- and the worker process is
what keeps a wrong guess from taking the server with it. Below 900 MB there is no
version of this that fits, so trying only costs the user a wait."""


def _read_int(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    if text == "max":
        return None
    try:
        return int(text.split()[0])
    except ValueError:
        return None


def _meminfo_available(root: Path = ROOT) -> int | None:
    """``MemAvailable`` from /proc/meminfo, in bytes."""
    try:
        for line in (root / "proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, IndexError, ValueError):
        return None
    return None


def _cgroup_headroom(root: Path = ROOT) -> int | None:
    """Headroom inside this container's memory cgroup, in bytes.

    RemoteTerm is usually a container, and inside one ``MemAvailable`` reports
    the *host's* free memory -- which is why a decode can be killed on a box
    that looks like it has gigabytes spare. The cgroup limit is the number that
    actually decides, so whichever is smaller wins.
    """
    v2_limit = root / "sys/fs/cgroup/memory.max"
    if v2_limit.is_file():
        limit = _read_int(v2_limit)
        used = _read_int(root / "sys/fs/cgroup/memory.current")
        if limit is not None and used is not None:
            return max(0, limit - used)
        return None
    limit = _read_int(root / "sys/fs/cgroup/memory/memory.limit_in_bytes")
    used = _read_int(root / "sys/fs/cgroup/memory/memory.usage_in_bytes")
    if limit is None or used is None:
        return None
    # An unset v1 limit is a huge sentinel (PAGE_COUNTER_MAX * page size), not a
    # number anyone configured. Treat anything absurd as no limit at all.
    if limit >= 1 << 62:
        return None
    return max(0, limit - used)


def available_memory_bytes(root: Path = ROOT) -> int | None:
    """Free memory this process could actually use, or None if unknowable.

    None on macOS and Windows, where neither source exists -- the caller must
    treat that as "no answer" and carry on, never as "no memory".
    """
    candidates = [
        value for value in (_meminfo_available(root), _cgroup_headroom(root)) if value is not None
    ]
    return min(candidates) if candidates else None


def format_bytes(value: int) -> str:
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f} GB"
    return f"{value / 1_000_000:.0f} MB"


def decode_memory_shortfall(root: Path = ROOT) -> str | None:
    """A sentence for the user when this host cannot fit a decode, else None."""
    available = available_memory_bytes(root)
    if available is None or available >= DECODE_REFUSE_BELOW_BYTES:
        return None
    return (
        f"This server has only {format_bytes(available)} of memory free, and "
        f"reconstructing an AI picture needs about "
        f"{format_bytes(DECODE_PEAK_BYTES)}. The picture is kept, so it can be "
        "opened from a machine with more memory; adding swap also works, slowly."
    )
