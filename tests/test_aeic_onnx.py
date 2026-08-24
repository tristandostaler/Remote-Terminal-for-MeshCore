"""The AEIC ONNX seam, and the full entropy pipeline against recorded tensors.

Two groups of tests, both skipped unless the machine has what they need:

* **Recording replays** need the ``.aeicrec`` files, which are 7.4 MB each and
  therefore not committed. Point ``AEIC_RECORDING_DIR`` at a directory holding
  them (see ``tests/aeic_fixtures.py`` for how to extract them). These prove the
  entropy layer -- squeeze, build_indexes, the masks, merge_context -- reproduces
  the reference symbols, indexes, bitstream and ``y_hat`` exactly, using recorded
  network output and no model.
* **Live graph tests** additionally need the 958 MiB model bundle installed.
  These prove the ORT session setup feeds and reads the graphs correctly, and
  pin the session-option finding described below.

## The session-option finding this file exists to protect

``graph_optimization_level`` must be left at ORT's DEFAULT. That is the opposite
of the instinct -- "disable optimisation so the arithmetic cannot be
re-associated" -- and it was measured, not guessed: at the default both entropy
graphs reproduce the recorded tensors bit-for-bit, while ORT_DISABLE_ALL,
ORT_ENABLE_BASIC and ORT_ENABLE_EXTENDED each perturb ~63,000 of 65,536 values
by up to 6.6e-07. Upstream saw a 2.76e-7 drift corrupt 15,728 latents with the
decode reporting success, so this is an order of magnitude past the danger line.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="the AEIC codec needs numpy")

from app.imaging.aeic.bundle import AeicBundle  # noqa: E402
from app.imaging.aeic.entropy import (  # noqa: E402  # noqa: E402
    AeicEntropyCodec,
    AeicEntropyNetwork,
    AeicGeometry,
    AeicMaskSet,
    EncodeSideTensors,
    StageParams,
    build_indexes,
    to_symbols,
    z_indexes,
)
from app.imaging.aeic.tables import parse_entropy_tables  # noqa: E402
from tests.aeic_fixtures import TABLES_PATH, read_recording  # noqa: E402

MODEL_DIR = Path(os.environ.get("AEIC_MODEL_DIR", "data/models/aeic"))
RECORDING_DIR = Path(os.environ.get("AEIC_RECORDING_DIR", "tests/fixtures/aeic/e2e"))


def _recordings() -> list[Path]:
    return sorted(RECORDING_DIR.glob("*.aeicrec")) if RECORDING_DIR.is_dir() else []


requires_recordings = pytest.mark.skipif(
    not _recordings(),
    reason=(
        f"no .aeicrec recordings in {RECORDING_DIR} (7.4 MB each, not committed; "
        "set AEIC_RECORDING_DIR)"
    ),
)


def _bundle_ready() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return AeicBundle(root=MODEL_DIR).supports_decode


requires_bundle = pytest.mark.skipif(
    not _bundle_ready(),
    reason=f"the 958 MiB AEIC model bundle is not installed in {MODEL_DIR}",
)


@pytest.fixture(scope="module")
def tables():
    return parse_entropy_tables(TABLES_PATH.read_bytes())


@pytest.fixture(scope="module")
def geometry():
    return AeicGeometry.for_resolution(512)


class ReplayNetwork(AeicEntropyNetwork):
    """Feeds back recorded ONNX tensors, asserting the entropy layer's inputs.

    The assertions in :meth:`run_hyper_synthesis` and :meth:`run_stage` are the
    point: they check that the decode loop hands the network exactly the tensors
    the real run was handed, which is what proves the masks, ``merge_context``
    and ``unsqueeze`` are right. Without them this would only test that the code
    agrees with itself.
    """

    def __init__(self, arrays: dict) -> None:
        self._a = arrays
        self.base_inputs_matched: list[bool] = []
        self.zq_input_matched: bool | None = None

    @property
    def supports_decode_side(self) -> bool:
        return True

    def run_encode_side(self, image_chw):
        return EncodeSideTensors(
            z_q=self._a["enc/z_q"],
            y_q=tuple(self._a[f"enc/yq{i}"] for i in range(4)),
            scales=tuple(self._a[f"enc/sc{i}"] for i in range(4)),
        )

    def run_hyper_synthesis(self, z_q):
        self.zq_input_matched = np.array_equal(
            z_q.reshape(-1), self._a["dec/call0/in/z_q"].reshape(-1)
        )
        return self._a["dec/call0/out/base0"]

    def run_stage(self, stage, base):
        self.base_inputs_matched.append(
            np.array_equal(base.reshape(-1), self._a[f"dec/call{stage + 1}/in/base"].reshape(-1))
        )
        return StageParams(
            means_supp=self._a[f"dec/call{stage + 1}/out/means"],
            scales_supp=self._a[f"dec/call{stage + 1}/out/scales"],
        )


@requires_recordings
@pytest.mark.parametrize("path", _recordings(), ids=lambda p: p.stem)
class TestAgainstRecordedTensors:
    def test_squeeze_and_quantizers_reproduce_the_reference_arrays(self, path, geometry, tables):
        """Element-for-element, for all five coder calls.

        This is the tightest available check on ``squeeze`` and
        ``build_indexes``: the recording stores exactly what the reference fed
        the coder.
        """
        _index, a = read_recording(path)
        masks = AeicMaskSet(geometry)
        assert np.array_equal(to_symbols(a["enc/z_q"]), a["enc/z_symbols"])
        assert np.array_equal(z_indexes(geometry), a["enc/z_indexes"])
        for stage in range(4):
            assert np.array_equal(
                to_symbols(masks.squeeze(a[f"enc/yq{stage}"])), a[f"enc/symbols{stage}"]
            ), f"stage {stage} symbols"
            assert np.array_equal(
                build_indexes(masks.squeeze(a[f"enc/sc{stage}"])), a[f"enc/indexes{stage}"]
            ), f"stage {stage} indexes"

    def test_encode_produces_the_reference_bitstream_byte_for_byte(self, path, geometry, tables):
        index, a = read_recording(path)
        codec = AeicEntropyCodec(geometry, ReplayNetwork(a), tables)
        # The image bytes are ignored: ReplayNetwork returns recorded tensors.
        stream = codec.encode(bytes(512 * 512 * 3))
        assert stream == a["enc/bitstream"].tobytes()
        assert hashlib.sha256(stream).hexdigest() == index["meta"]["bitstream_sha256"]

    def test_decode_reconstructs_the_latent_and_feeds_the_graph_correctly(
        self, path, geometry, tables
    ):
        index, a = read_recording(path)
        network = ReplayNetwork(a)
        codec = AeicEntropyCodec(geometry, network, tables)
        y_hat = codec.decode_to_latent(a["enc/bitstream"].tobytes())

        assert network.zq_input_matched, "decoded z_q differs from the recorded graph input"
        assert all(network.base_inputs_matched), (
            f"stage context differs from the recording: {network.base_inputs_matched}. "
            "The masks, merge_context or unsqueeze are wrong."
        )
        assert np.array_equal(y_hat.reshape(-1), a["dec/y_hat"].reshape(-1))

    def test_the_receiver_latent_equals_the_sender_latent(self, path, geometry, tables):
        """The silent-corruption check, as the recording itself asserts it."""
        index, a = read_recording(path)
        assert index["meta"]["decoded_y_hat_equals_encoder_y_hat"] is True
        assert np.array_equal(a["dec/y_hat"].reshape(-1), a["enc/y_hat"].reshape(-1))


@requires_bundle
class TestLiveGraphs:
    @pytest.fixture(scope="class")
    def backend(self):
        from app.imaging.aeic.onnx_backend import OnnxAeicBackend

        instance = OnnxAeicBackend(AeicBundle(root=MODEL_DIR))
        yield instance
        instance.close()

    def test_decode_side_graph_has_the_expected_contract(self, backend):
        network = backend.entropy_network(for_decode=True)
        assert network.supports_decode_side

    def test_a_send_only_network_reports_no_decode_side(self, backend):
        network = backend.entropy_network(for_decode=False)
        assert not network.supports_decode_side

    def test_encode_side_graph_returns_the_documented_tensor_shapes(self, backend):
        from app.imaging.aeic.entropy import rgb_to_chw

        network = backend.entropy_network(for_decode=False)
        tensors = network.run_encode_side(rgb_to_chw(bytes(512 * 512 * 3), 512))
        assert tensors.z_q.shape == (1, 128, 4, 4)
        assert len(tensors.y_q) == 4 and len(tensors.scales) == 4
        for stage in range(4):
            assert tensors.y_q[stage].shape == (1, 256, 16, 16)
            assert tensors.scales[stage].shape == (1, 256, 16, 16)

    def test_run_stage_rejects_a_stage_outside_zero_to_three(self, backend):
        """The graph does NOT validate `stage`: anything >= 4 silently falls into
        the stage-3 branch, which desynchronises rANS without an error."""
        network = backend.entropy_network(for_decode=True)
        base = np.zeros((1, 256, 16, 16), dtype=np.float32)
        for stage in (-1, 4, 99):
            with pytest.raises(ValueError, match="0..3"):
                network.run_stage(stage, base)

    @requires_recordings
    @pytest.mark.parametrize("path", _recordings(), ids=lambda p: p.stem)
    def test_live_graph_reproduces_the_recorded_tensors_bit_for_bit(self, backend, path):
        """The default session options are the only ones that do this.

        If this fails after a session-option change, read the module docstring
        before touching anything else -- lowering the optimisation level is the
        most likely cause and is precisely what must not be done.
        """
        _index, a = read_recording(path)
        network = backend.entropy_network(for_decode=True)

        base0 = network.run_hyper_synthesis(a["dec/call0/in/z_q"])
        assert np.array_equal(base0.reshape(-1), a["dec/call0/out/base0"].reshape(-1))
        for stage in range(4):
            params = network.run_stage(stage, a[f"dec/call{stage + 1}/in/base"])
            assert np.array_equal(
                params.means_supp.reshape(-1), a[f"dec/call{stage + 1}/out/means"].reshape(-1)
            ), f"stage {stage} means"
            assert np.array_equal(
                params.scales_supp.reshape(-1), a[f"dec/call{stage + 1}/out/scales"].reshape(-1)
            ), f"stage {stage} scales"

    def test_session_options_leave_graph_optimization_at_the_default(self):
        """A unit-level guard, so the intent survives even without recordings."""
        import onnxruntime as ort

        from app.imaging.aeic.onnx_backend import _session_options

        for single_threaded in (True, False):
            options = _session_options(single_threaded=single_threaded)
            assert options.graph_optimization_level == ort.GraphOptimizationLevel.ORT_ENABLE_ALL, (
                "graph_optimization_level must stay at ORT's default; lowering it "
                "perturbs every scale in the entropy model and desynchronises rANS"
            )

    def test_a_real_round_trip_agrees_on_every_latent(self, backend, tables, geometry):
        """End to end on real pixels: the definitive silent-corruption test.

        The send-side graph exposes ``y_hat``, so the sender's belief about the
        latent is knowable. If the receiver's 65,536 latents are bit-identical
        after a trip through ~150 bytes, the coder and both graphs agree.
        """
        import onnxruntime as ort

        rng = np.random.default_rng(3)
        # Smooth, photograph-like content rather than white noise, which is
        # pathological for a learned image codec.
        small = rng.random((3, 32, 32), dtype=np.float32)
        upsampled = np.repeat(np.repeat(small, 16, axis=1), 16, axis=2)
        rgb = (upsampled * 255).astype(np.uint8).transpose(1, 2, 0).tobytes()

        from app.imaging.aeic.entropy import rgb_to_chw

        bundle = AeicBundle(root=MODEL_DIR)
        send = ort.InferenceSession(
            str(bundle.entropy_graph_path), providers=["CPUExecutionProvider"]
        )
        (y_hat_sender,) = send.run(["y_hat"], {"image": rgb_to_chw(rgb, 512)})

        stream = AeicEntropyCodec(
            geometry, backend.entropy_network(for_decode=False), tables
        ).encode(rgb)
        assert 1 <= len(stream) <= 1024, f"implausible bitstream size {len(stream)}"

        y_hat_receiver = AeicEntropyCodec(
            geometry, backend.entropy_network(for_decode=True), tables
        ).decode_to_latent(stream)

        differing = int((y_hat_sender.reshape(-1) != y_hat_receiver.reshape(-1)).sum())
        assert differing == 0, (
            f"{differing} of {y_hat_sender.size} latents differ -- rANS desynchronised. "
            "This is the failure mode that produces a sharp, plausible, WRONG image."
        )

    def test_synthesis_pass_produces_a_full_size_rgb_frame(self, backend, tables, geometry):
        latent = np.zeros((1, 256, 16, 16), dtype=np.float32)
        rgb = backend.decode_latent_to_rgb(latent)
        assert len(rgb) == 512 * 512 * 3
        backend.release_decoder_session()

    def test_decode_latent_rejects_the_wrong_element_count(self, backend):
        with pytest.raises(ValueError, match="expected"):
            backend.decode_latent_to_rgb(np.zeros(1234, dtype=np.float32))
