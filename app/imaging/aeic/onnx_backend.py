"""The native inference seam for AEIC: ONNX Runtime sessions and tensor marshalling.

This is the ONLY module in :mod:`app.imaging.aeic` that imports onnxruntime, and
the only one that needs the optional ``aeic`` extra installed. It drives three
graphs:

* the **decoder** (synthesis) half, int8 QDQ, ~835 MiB of external weights:
  ``y_hat float32 [1, 256, 16, 16] -> image float32 [1, 3, 512, 512]``
* the **send-side entropy** graph, fp32, 64 MiB:
  ``image -> z_q, yq0..3, sc0..3``, whose integer outputs feed the rANS coder
* the **decode-side entropy** graph, fp32, 58 MiB:
  ``(z_q, base, stage) -> (base0, means, scales)``, called five times per image
  because decoding is sequential

## MEMORY CONTRACT (this is not an optimisation)

Peaks measured upstream: entropy graph alone 0.35 GiB, image decoder alone
2.16 GiB, both resident 2.44 GiB. RemoteTerm often runs on a Pi or a small VPS,
so this matters more here than it did on a phone:

* :meth:`OnnxAeicBackend.encode` creates the SEND-side entropy session, runs,
  and **keeps** it. Sending a second photo then costs nothing. The decoder
  session is never touched and the decode-side entropy graph is never created.
* :meth:`OnnxAeicBackend.decode` creates the DECODE-side entropy session, runs
  the entropy loop, then calls :meth:`release_entropy_sessions` **before**
  creating the synthesis session. Holding both at once means 2.44 GiB.
* Only one entropy direction is ever resident; no operation needs both.
* :meth:`release_decoder_session` drops the expensive half first, because the
  send path only needs the small one.

## BIT-EXACTNESS REQUIREMENT (do not skip this)

AEIC's rANS coder is synchronous with the entropy model: the decoder re-runs
``h_s``, ``g_c`` and the adapters to reproduce the exact symbol probabilities
the encoder used. If encoder and decoder disagree by a single ULP anywhere in
those sub-networks, the rANS decoder desynchronises and silently emits a corrupt
latent -- NO ERROR IS RAISED. Upstream observed a 2.76e-7 layout-rounding
difference in one convolution corrupting 15,728 of 65,536 latents with the
decode reporting success.

Consequences, all of which this module honours:

* Ship the entropy-side graph as ONE artifact used by both sender and receiver,
  and never mix runtimes across the channel.
* Keep ``h_s``, ``g_c`` and the adapters in fp32. Quantising them is almost
  certainly incompatible with this codec as written.
* **Leave ``graph_optimization_level`` at ORT's default.** This is the opposite
  of the instinct, and it was measured, not guessed: at the default
  (``ORT_ENABLE_ALL``) both entropy graphs reproduce upstream's recorded tensors
  bit-for-bit, while ``ORT_DISABLE_ALL``, ``ORT_ENABLE_BASIC`` and
  ``ORT_ENABLE_EXTENDED`` each perturb ~63,000 of 65,536 values by up to
  6.6e-07 -- an order of magnitude more than the 2.76e-7 drift upstream saw
  corrupt 15,728 latents. "Turn optimisation off to be safe" is precisely the
  change that breaks interop here; see ``tests/test_aeic_onnx.py``, which pins
  this against the recordings.
* Thread count, by contrast, does NOT affect the result: ``intra_op = 1`` was
  measured bit-identical to the default, so the sessions are pinned to one
  thread for predictable CPU use on a small gateway.

Nothing above constrains the synthesis pass: it is downstream of the entropy
coder, so a ULP of drift there costs a hair of PSNR, not the image.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

from app.imaging.aeic.bundle import AeicBundle

# Constants and the runtime probe live in the stdlib-only `constants` module so
# that importing the AEIC package does not require numpy. See its docstring --
# the app failed to start entirely when they lived here.
from app.imaging.aeic.constants import (  # noqa: F401 - re-exported
    BASE0_OUTPUT,
    BASE_INPUT,
    BASE_SHAPE,
    DECODER_INPUT,
    DECODER_OUTPUT,
    HYPER_STAGE,
    IMAGE_INPUT,
    LATENT_ELEMENTS,
    LATENT_SHAPE,
    MEANS_OUTPUT,
    SCALES_OUTPUT,
    SQUARE_SIZE,
    STAGE_INPUT,
    ZQ_INPUT,
    ZQ_SHAPE,
    AeicRuntimeMissing,
    onnxruntime_available,
    require_onnxruntime,
)
from app.imaging.aeic.entropy import (
    AeicEntropyNetwork,
    EncodeSideTensors,
    StageParams,
)

logger = logging.getLogger(__name__)

_require_onnxruntime = require_onnxruntime


def _session_options(*, single_threaded: bool):
    """Session options for one graph.

    ``graph_optimization_level`` is deliberately NOT set, for either graph. See
    the bit-exactness note in this module's docstring: the default is the only
    level that reproduces the reference tensors, and lowering it silently
    perturbs every scale in the entropy model.

    ``single_threaded`` pins the two small entropy graphs to one thread -- it is
    measured bit-identical and keeps a mesh gateway responsive. The synthesis
    graph is far too expensive to run single-threaded and is downstream of the
    coder anyway, so it takes the default pool.
    """
    ort = _require_onnxruntime()
    options = ort.SessionOptions()
    if single_threaded:
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    return options


class OnnxAeicEntropyNetwork(AeicEntropyNetwork):
    """:class:`AeicEntropyNetwork` backed by the two fp32 entropy ONNX graphs.

    ## TWO GRAPHS, NOT ONE. This is not a packaging accident.

    The send-side export has one input ``image`` and emits every stage at once,
    which is only valid for an encoder -- it already knows ``y``. Decoding is
    inherently sequential, so a receiver needs the ``If``-branched export it can
    call five times per image.

    Unlike the Dart client, Python's ORT lets us request an output *subset*, so
    each call is pruned to the branch it needs on top of the graph's own ``If``.
    All three inputs are still fed on every run: ORT rejects a feed that omits a
    declared input, so the branch that does not read a tensor is handed zeros.
    Zeros, not a stale tensor -- a stale one would be silently wrong the day the
    export starts reading it.
    """

    def __init__(self, encode_session=None, decode_session=None) -> None:
        self._encode_session = encode_session
        self._decode_session = decode_session
        self._zero_base = np.zeros(BASE_SHAPE, dtype=np.float32)
        self._zero_zq = np.zeros(ZQ_SHAPE, dtype=np.float32)

    @property
    def supports_decode_side(self) -> bool:
        session = self._decode_session
        if session is None:
            return False
        inputs = {i.name for i in session.get_inputs()}
        outputs = {o.name for o in session.get_outputs()}
        return {ZQ_INPUT, BASE_INPUT, STAGE_INPUT} <= inputs and {
            BASE0_OUTPUT,
            MEANS_OUTPUT,
            SCALES_OUTPUT,
        } <= outputs

    def run_encode_side(self, image_chw: np.ndarray) -> EncodeSideTensors:
        session = self._encode_session
        if session is None:
            raise AeicEntropyNetworkMissing(
                "this network has no send-side entropy session, so it cannot encode"
            )
        names = {i.name for i in session.get_inputs()}
        if IMAGE_INPUT not in names:
            raise AeicEntropyNetworkMissing(
                'the entropy graph has no "image" input, so it cannot encode'
            )
        wanted = ["z_q", *[f"yq{i}" for i in range(4)], *[f"sc{i}" for i in range(4)]]
        results = session.run(
            wanted,
            {IMAGE_INPUT: np.ascontiguousarray(image_chw, dtype=np.float32)},
        )
        by_name = {
            name: np.asarray(value, dtype=np.float32)
            for name, value in zip(wanted, results, strict=True)
        }
        return EncodeSideTensors(
            z_q=by_name["z_q"],
            y_q=tuple(by_name[f"yq{i}"] for i in range(4)),
            scales=tuple(by_name[f"sc{i}"] for i in range(4)),
        )

    def run_hyper_synthesis(self, z_q: np.ndarray) -> np.ndarray:
        session = self._require_decode_side()
        array = np.ascontiguousarray(z_q, dtype=np.float32).reshape(ZQ_SHAPE)
        (base0,) = session.run(
            [BASE0_OUTPUT],
            {
                ZQ_INPUT: array,
                BASE_INPUT: self._zero_base,
                STAGE_INPUT: np.array([HYPER_STAGE], dtype=np.int32),
            },
        )
        return np.asarray(base0, dtype=np.float32)

    def run_stage(self, stage: int, base: np.ndarray) -> StageParams:
        session = self._require_decode_side()
        if not 0 <= stage <= 3:
            raise ValueError(f"stage {stage} must be 0..3")
        array = np.ascontiguousarray(base, dtype=np.float32).reshape(BASE_SHAPE)
        means, scales = session.run(
            [MEANS_OUTPUT, SCALES_OUTPUT],
            {
                ZQ_INPUT: self._zero_zq,
                BASE_INPUT: array,
                STAGE_INPUT: np.array([stage], dtype=np.int32),
            },
        )
        # Already masked by the graph. The entropy loop masks again, which is
        # exactly idempotent because the mask is a select -- and removing the
        # second mask would couple the arithmetic to this particular export.
        return StageParams(
            means_supp=np.asarray(means, dtype=np.float32),
            scales_supp=np.asarray(scales, dtype=np.float32),
        )

    def _require_decode_side(self):
        session = self._decode_session
        if session is None:
            raise AeicEntropyNetworkMissing(
                "this network has no decode-side entropy session; decoding needs "
                "the z_q -> base0 / base -> means,scales export"
            )
        return session


class AeicEntropyNetworkMissing(RuntimeError):
    """A session the requested direction needs was never created."""


class OnnxAeicBackend:
    """Owns the ORT sessions for one installed :class:`AeicBundle`.

    Sessions are created lazily and can be dropped individually; see the memory
    contract in this module's docstring. Every public method holds ``_lock``, so
    two concurrent conversations cannot interleave inference on one 2 GiB graph.
    """

    def __init__(self, bundle: AeicBundle) -> None:
        self.bundle = bundle
        self._lock = threading.Lock()
        self._encode_entropy = None
        self._decode_entropy = None
        self._synthesis = None

    @property
    def name(self) -> str:
        return f"onnxruntime/{self.bundle.rate_point}"

    # ---- session lifecycle --------------------------------------------------

    def _ensure_encode_entropy(self):
        if self._encode_entropy is None:
            ort = _require_onnxruntime()
            path = self.bundle.require(self.bundle.entropy_graph_path, "entropy graph")
            logger.info("Loading AEIC send-side entropy graph from %s", path)
            self._encode_entropy = ort.InferenceSession(
                str(path),
                sess_options=_session_options(single_threaded=True),
                providers=["CPUExecutionProvider"],
            )
        return self._encode_entropy

    def _ensure_decode_entropy(self):
        if self._decode_entropy is None:
            ort = _require_onnxruntime()
            path = self.bundle.require(
                self.bundle.entropy_decode_graph_path, "decode-side entropy graph"
            )
            logger.info("Loading AEIC decode-side entropy graph from %s", path)
            self._decode_entropy = ort.InferenceSession(
                str(path),
                sess_options=_session_options(single_threaded=True),
                providers=["CPUExecutionProvider"],
            )
        return self._decode_entropy

    def _ensure_synthesis(self):
        if self._synthesis is None:
            ort = _require_onnxruntime()
            path = self.bundle.require(self.bundle.decoder_graph_path, "decoder graph")
            # The graph references its ~835 MiB external-weights sibling by a
            # literal relative filename, so ORT must resolve it from the graph's
            # own directory. bundle.verify_layout() has already checked it is there.
            logger.info("Loading AEIC synthesis decoder from %s (~2.2 GiB peak)", path)
            self._synthesis = ort.InferenceSession(
                str(path),
                sess_options=_session_options(single_threaded=False),
                providers=["CPUExecutionProvider"],
            )
        return self._synthesis

    def release_decoder_session(self) -> None:
        """Drop the ~2.16 GiB synthesis session, keeping the entropy halves."""
        if self._synthesis is not None:
            logger.info("Releasing AEIC synthesis decoder session")
        self._synthesis = None

    def release_entropy_sessions(self) -> None:
        """Drop both entropy sessions, keeping the synthesis half."""
        self._encode_entropy = None
        self._decode_entropy = None

    def close(self) -> None:
        with self._lock:
            self.release_decoder_session()
            self.release_entropy_sessions()

    def handle_memory_pressure(self) -> None:
        """Shed the expensive half.

        The synthesis decoder goes first: it is 86% of the cost and the send path
        does not need it.
        """
        with self._lock:
            self.release_decoder_session()

    # ---- inference ---------------------------------------------------------

    def entropy_network(self, *, for_decode: bool) -> OnnxAeicEntropyNetwork:
        """A network wired for one direction only, per the memory contract."""
        if for_decode:
            return OnnxAeicEntropyNetwork(decode_session=self._ensure_decode_entropy())
        return OnnxAeicEntropyNetwork(encode_session=self._ensure_encode_entropy())

    def decode_latent_to_rgb(self, y_hat: np.ndarray) -> bytes:
        """Run the synthesis half: latent -> packed 8-bit RGB."""
        from app.imaging.aeic.entropy import chw_to_rgb

        if y_hat.size != LATENT_ELEMENTS:
            raise ValueError(
                f"expected {LATENT_ELEMENTS} float32 latents ({LATENT_SHAPE}), got {y_hat.size}"
            )
        session = self._ensure_synthesis()
        input_name = session.get_inputs()[0].name
        output_name = session.get_outputs()[0].name
        (raw,) = session.run(
            [output_name],
            {input_name: np.ascontiguousarray(y_hat, dtype=np.float32).reshape(LATENT_SHAPE)},
        )
        # ORT's run() is typed as returning a union that includes SparseTensor;
        # this graph's single output is a dense float32 tensor, so narrow it once
        # here rather than asserting the union away at each use.
        image = np.asarray(raw, dtype=np.float32)
        expected = SQUARE_SIZE * SQUARE_SIZE * 3
        if image.size != expected:
            raise ValueError(
                f"decoder returned {image.size} values for shape {image.shape}; "
                f"expected {expected} ({SQUARE_SIZE}x{SQUARE_SIZE} RGB). The export "
                "is static 512x512 -- a different shape means the wrong model file."
            )
        return chw_to_rgb(image, SQUARE_SIZE)
