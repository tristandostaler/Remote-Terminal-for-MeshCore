"""Everything about AEIC that does NOT need numpy, onnxruntime or Pillow.

This module exists so the rest of the app can import the AEIC package on an
install that never opted into the ``aeic`` extra.

That is not a nicety. ``app/services/messages.py`` imports
:func:`app.imaging.aeic.ingest.note_inbound_chunk` at module level, so the AEIC
package sits on the import path of the whole application — and if importing it
pulls numpy, a base install cannot start at *all*. It shipped that way once
(``onnx_backend`` imports numpy at module level, and ``service`` imported a
constant from it), and the failure is total: ``ModuleNotFoundError: No module
named 'numpy'`` out of ``uvicorn`` before the radio ever connects.

So the rule is: **anything reachable from ``service``, ``ingest``,
``transport``, ``prepare`` or ``__init__`` at import time lives here or in another
stdlib-only module.** The heavy modules (``entropy``, ``onnx_backend``) are
imported lazily, inside the functions that actually run inference.
``tests/test_aeic_optional_extra.py`` enforces this statically.
"""

from __future__ import annotations

SQUARE_SIZE = 512
"""Square edge length the codec encodes.

512 is a hard floor: the SD-Turbo UNet in the synthesis decoder needs a 64x64
latent and collapses below it.
"""

LATENT_SHAPE = (1, 256, 16, 16)
LATENT_ELEMENTS = 256 * 16 * 16
ZQ_SHAPE = (1, 128, 4, 4)
BASE_SHAPE = (1, 256, 16, 16)

RGB_BYTES_EXPECTED = SQUARE_SIZE * SQUARE_SIZE * 3
"""What the encoder takes: ``512 * 512 * 3`` bytes of packed 8-bit RGB."""

# Graph input/output names in the promoted exports.
IMAGE_INPUT = "image"
ZQ_INPUT = "z_q"
BASE_INPUT = "base"
STAGE_INPUT = "stage"
BASE0_OUTPUT = "base0"
MEANS_OUTPUT = "means"
SCALES_OUTPUT = "scales"
DECODER_INPUT = "y_hat"
DECODER_OUTPUT = "image"

HYPER_STAGE = -1
"""The ``stage`` value that selects the hyper-synthesis branch.

Any negative value works; -1 is the documented one. Note the graph does NOT
validate ``stage``: anything >= 4 silently falls into the stage-3 branch, which
desynchronises rANS without an error, so callers check.
"""


DECODE_WORKER_EXIT_BAD_REQUEST = 2
"""The decode worker was called wrongly (no bitstream, no model directory)."""

DECODE_WORKER_EXIT_DECODE_FAILED = 3
"""The decode worker ran and the decode itself raised; stderr carries the why."""


class AeicRuntimeMissing(RuntimeError):
    """onnxruntime is not installed, so the AEIC codec cannot run at all."""


def onnxruntime_available() -> bool:
    """Whether the optional ``aeic`` extra is installed."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return True


def require_onnxruntime():
    """Import onnxruntime or raise with the command that installs it."""
    try:
        import onnxruntime
    except ImportError as exc:  # pragma: no cover - depends on the install
        raise AeicRuntimeMissing(
            "the AEIC image codec needs onnxruntime, which is an optional extra. "
            "Install it with `uv sync --extra aeic` (or set MESHCORE_ENABLE_AEIC=1 "
            "in Docker / the Home Assistant add-on)."
        ) from exc
    return onnxruntime
