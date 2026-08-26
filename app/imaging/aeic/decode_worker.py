"""Reconstruct one AEIC picture in a process of its own.

Run as ``python -m app.imaging.aeic.decode_worker <model_dir>``: the bitstream
arrives on stdin, the PNG leaves on stdout, anything human goes to stderr.

## Why a separate process

The synthesis pass needs ~1.3 GiB even with the weights mapped, and RemoteTerm
runs on hosts that do not have it. When the kernel's OOM killer arrives it picks
the biggest process, and in-process that was **uvicorn** -- so a single received
picture took the whole server down, radio connection included, with nothing in
the log to say why. Out of process the kill lands on this worker, the server sees
a child that died on a signal, and the picture is reported as needing more memory
than the host has. It is stored either way, so it can be opened later or
elsewhere.

Two lesser benefits, both real on a long-running gateway: every byte is returned
to the OS when the worker exits (ORT's arena does not release on its own), and
the parent never holds an inference session for a path it only uses on receipt.

## Protocol

* stdin: the raw rANS bitstream, EOF-terminated.
* stdout: the PNG, and nothing else. Never write anything else here.
* exit 0 with a PNG on success; :data:`EXIT_BAD_REQUEST` for an unusable
  argument or an empty bitstream, :data:`EXIT_DECODE_FAILED` when the decode
  itself raised. Death by signal is the interesting case and is diagnosed by the
  parent, not here -- nothing in this process gets to run at that point.
"""

from __future__ import annotations

import sys
from pathlib import Path

from app.imaging.aeic.constants import (  # noqa: E402 - after the module docstring
    DECODE_WORKER_EXIT_BAD_REQUEST as EXIT_BAD_REQUEST,
)
from app.imaging.aeic.constants import (  # noqa: E402
    DECODE_WORKER_EXIT_DECODE_FAILED as EXIT_DECODE_FAILED,
)

OOM_SCORE_ADJ = "500"
"""Make the kernel prefer this process over its parent.

Raising one's own ``oom_score_adj`` needs no privilege (only lowering it does).
The worker is already the biggest thing in the cgroup so it would usually be
chosen anyway; this removes the "usually" -- what must never be chosen is the
server holding the radio link.
"""


def _volunteer_for_the_oom_killer() -> None:
    try:
        Path("/proc/self/oom_score_adj").write_text(OOM_SCORE_ADJ)
    except OSError:
        # Not Linux, or a hardened /proc. Nothing is lost: the worker is still
        # far larger than the parent, so it is still the likely pick.
        pass


def decode(model_dir: Path, bitstream: bytes) -> bytes:
    """The bitstream -> a PNG. Mirrors the in-process path step for step."""
    from app.imaging.aeic.bundle import RATE_POINT, AeicBundle
    from app.imaging.aeic.constants import SQUARE_SIZE
    from app.imaging.aeic.entropy import AeicEntropyCodec, AeicGeometry
    from app.imaging.aeic.onnx_backend import OnnxAeicBackend
    from app.imaging.aeic.png import encode_png
    from app.imaging.aeic.tables import parse_entropy_tables

    bundle = AeicBundle(root=model_dir, rate_point=RATE_POINT)
    bundle.verify_layout()
    backend = OnnxAeicBackend(bundle)
    tables = parse_entropy_tables(bundle.require(bundle.tables_path, "CDF tables").read_bytes())
    codec = AeicEntropyCodec(
        AeicGeometry.for_resolution(SQUARE_SIZE),
        backend.entropy_network(for_decode=True),
        tables,
    )
    try:
        y_hat = codec.decode_to_latent(bitstream)
        # The releases still matter here even though the process is about to end:
        # they keep the entropy graph out of the peak, and the peak is the whole
        # reason this runs where it does.
        backend.release_entropy_sessions()
        rgb = backend.decode_latent_to_rgb(y_hat)
    finally:
        backend.close()
    return encode_png(rgb, SQUARE_SIZE, SQUARE_SIZE)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.imaging.aeic.decode_worker <model_dir>", file=sys.stderr)
        return EXIT_BAD_REQUEST
    model_dir = Path(argv[1])
    bitstream = sys.stdin.buffer.read()
    if not bitstream:
        print("no bitstream on stdin", file=sys.stderr)
        return EXIT_BAD_REQUEST

    _volunteer_for_the_oom_killer()
    try:
        png = decode(model_dir, bitstream)
    except Exception as exc:  # noqa: BLE001 - the parent shows this to the user
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_DECODE_FAILED
    sys.stdout.buffer.write(png)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
