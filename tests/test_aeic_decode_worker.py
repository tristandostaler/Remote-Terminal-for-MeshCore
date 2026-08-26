"""Reconstruction runs in a worker process, and says so when it runs out of room.

The synthesis pass needs ~1.3 GiB. On a host without it the kernel's OOM killer
picks the largest process, which in-process was uvicorn -- so one received
picture killed the server and its radio link, with nothing in the log naming the
cause. These tests pin the three parts of the answer:

* a decode that dies is reported as a decode that died, never retried in the
  server's own process (which is how the server gets killed instead);
* SIGKILL is read as "out of memory" and says how much the host had;
* a host that plainly cannot fit one refuses up front, and refuses for
  *receiving* only -- sending stays available, since encoding costs 0.35 GiB.
"""

from __future__ import annotations

import signal
import subprocess
from pathlib import Path

import pytest

from app.imaging.aeic import service as service_module
from app.imaging.aeic.constants import (
    DECODE_WORKER_EXIT_BAD_REQUEST,
    DECODE_WORKER_EXIT_DECODE_FAILED,
)
from app.imaging.aeic.service import (
    AeicDecodeFailed,
    AeicDecodeOutOfMemory,
    AeicService,
    _WorkerNotStartable,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"rest of the image"


@pytest.fixture
def aeic() -> AeicService:
    return AeicService()


class TestReadingTheWorkersFate:
    def test_a_png_comes_straight_back(self, aeic):
        assert aeic._read_worker_result(0, PNG, b"") == PNG

    def test_sigkill_is_reported_as_running_out_of_memory(self, aeic):
        with pytest.raises(AeicDecodeOutOfMemory) as raised:
            aeic._read_worker_result(-signal.SIGKILL, b"", b"")

        message = str(raised.value)
        assert "ran out of memory" in message
        # The number is the point: "it failed" leaves nothing to act on, while
        # "needs about 1.4 GB" is a decision about the host.
        assert "1.4 GB" in message
        assert "picture is kept" in message

    def test_a_shell_flattened_signal_is_read_the_same_way(self, aeic):
        """A killed child reports 128+n if anything in between used a shell."""
        with pytest.raises(AeicDecodeOutOfMemory):
            aeic._read_worker_result(128 + int(signal.SIGKILL), b"", b"")

    def test_another_signal_is_a_crash_not_a_memory_verdict(self, aeic):
        with pytest.raises(AeicDecodeFailed) as raised:
            aeic._read_worker_result(-signal.SIGSEGV, b"", b"boom")

        assert not isinstance(raised.value, AeicDecodeOutOfMemory)
        assert "SIGSEGV" in str(raised.value)

    def test_the_workers_own_reason_reaches_the_user(self, aeic):
        with pytest.raises(AeicDecodeFailed, match="ChannelDataFormatError: bad chunk"):
            aeic._read_worker_result(
                DECODE_WORKER_EXIT_DECODE_FAILED,
                b"",
                b"INFO:something noisy\nChannelDataFormatError: bad chunk\n",
            )

    def test_a_bad_request_says_which(self, aeic):
        with pytest.raises(AeicDecodeFailed, match="rejected the request: no bitstream"):
            aeic._read_worker_result(DECODE_WORKER_EXIT_BAD_REQUEST, b"", b"no bitstream on stdin")

    def test_success_without_an_image_is_still_a_failure(self, aeic):
        with pytest.raises(AeicDecodeFailed, match="without producing an image"):
            aeic._read_worker_result(0, b"not a png", b"")


class TestWhoRunsTheDecode:
    async def test_a_dead_worker_is_never_retried_in_process(self, aeic, monkeypatch):
        """The safety property this whole design exists for.

        Falling back after an OOM kill would run the same 1.3 GiB allocation in
        the server's own process -- i.e. it would convert a reported failure back
        into the crash it replaced.
        """
        monkeypatch.setattr(aeic, "_require_ready", lambda **_: None)

        def killed(_bitstream):
            raise AeicDecodeOutOfMemory("out of memory")

        in_process = []
        monkeypatch.setattr(aeic, "_decode_in_worker", killed)
        monkeypatch.setattr(aeic, "_decode_in_process", lambda b: in_process.append(b) or PNG)

        with pytest.raises(AeicDecodeOutOfMemory):
            await aeic.decode_to_png(b"bitstream")

        assert in_process == [], "an out-of-memory decode was retried in the server's process"

    async def test_a_host_that_cannot_spawn_falls_back(self, aeic, monkeypatch):
        """A sandbox with no subprocesses at all still decodes -- riskily, but a
        host that refuses to fork is not evidence about memory."""
        monkeypatch.setattr(aeic, "_require_ready", lambda **_: None)

        def cannot_start(_bitstream):
            raise _WorkerNotStartable("no subprocesses here")

        monkeypatch.setattr(aeic, "_decode_in_worker", cannot_start)
        monkeypatch.setattr(aeic, "_decode_in_process", lambda _b: PNG)

        assert await aeic.decode_to_png(b"bitstream") == PNG

    def test_a_refusal_to_spawn_is_not_reported_as_a_decode_failure(self, aeic, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise OSError("fork: resource temporarily unavailable")

        monkeypatch.setattr(subprocess, "run", refuse)

        with pytest.raises(_WorkerNotStartable):
            aeic._decode_in_worker(b"bitstream")

    def test_a_worker_that_never_finishes_is_killed_and_reported(self, aeic, monkeypatch):
        def hang(*_args, **kwargs):
            raise subprocess.TimeoutExpired(cmd="worker", timeout=kwargs.get("timeout", 0))

        monkeypatch.setattr(subprocess, "run", hang)

        with pytest.raises(AeicDecodeFailed, match="gave up after"):
            aeic._decode_in_worker(b"bitstream")

    def test_the_worker_is_handed_the_model_directory_and_the_bitstream(self, aeic, monkeypatch):
        calls = {}

        def record(command, **kwargs):
            calls["command"] = command
            calls["input"] = kwargs["input"]
            calls["cwd"] = kwargs["cwd"]
            return subprocess.CompletedProcess(command, 0, PNG, b"")

        monkeypatch.setattr(subprocess, "run", record)

        assert aeic._decode_in_worker(b"the bitstream") == PNG
        assert calls["command"][1:3] == ["-m", service_module.DECODE_WORKER_MODULE]
        assert calls["command"][3] == str(aeic.model_dir)
        assert calls["input"] == b"the bitstream"
        # Started from the repository root, so `-m` resolves whatever directory
        # the server itself was launched from.
        assert (Path(calls["cwd"]) / "app" / "imaging" / "aeic").is_dir()


class TestTheWorkerProtocol:
    """stdin -> stdout, and nothing else on stdout ever."""

    def _stdin(self, monkeypatch, payload: bytes) -> None:
        import io
        import sys
        from types import SimpleNamespace

        monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload)))

    def test_a_missing_model_directory_is_a_bad_request(self, capsys):
        from app.imaging.aeic import decode_worker

        assert decode_worker.main(["decode_worker"]) == DECODE_WORKER_EXIT_BAD_REQUEST
        assert "usage" in capsys.readouterr().err

    def test_an_empty_bitstream_is_a_bad_request(self, monkeypatch, capsys):
        from app.imaging.aeic import decode_worker

        self._stdin(monkeypatch, b"")

        assert decode_worker.main(["decode_worker", "models"]) == DECODE_WORKER_EXIT_BAD_REQUEST
        assert "no bitstream" in capsys.readouterr().err

    def test_a_decode_that_raises_names_the_reason_on_stderr(self, monkeypatch, capsys):
        from app.imaging.aeic import decode_worker

        self._stdin(monkeypatch, b"bitstream")

        def boom(_dir, _bitstream):
            raise ValueError("desynchronised")

        monkeypatch.setattr(decode_worker, "decode", boom)

        assert decode_worker.main(["decode_worker", "models"]) == DECODE_WORKER_EXIT_DECODE_FAILED
        captured = capsys.readouterr()
        assert "ValueError: desynchronised" in captured.err
        assert captured.out == "", "stdout must carry the PNG or nothing at all"

    def test_a_decoded_picture_goes_to_stdout_alone(self, monkeypatch, capsysbinary):
        from app.imaging.aeic import decode_worker

        self._stdin(monkeypatch, b"bitstream")
        monkeypatch.setattr(decode_worker, "decode", lambda _dir, _bits: PNG)

        assert decode_worker.main(["decode_worker", "models"]) == 0
        assert capsysbinary.readouterr().out == PNG

    def test_it_volunteers_for_the_oom_killer_before_allocating(self, monkeypatch, tmp_path):
        """Order matters: the adjustment is worthless once the pages are taken."""
        from app.imaging.aeic import decode_worker

        self._stdin(monkeypatch, b"bitstream")
        events = []
        monkeypatch.setattr(
            decode_worker, "_volunteer_for_the_oom_killer", lambda: events.append("adjust")
        )
        monkeypatch.setattr(
            decode_worker, "decode", lambda _dir, _bits: events.append("decode") or PNG
        )

        decode_worker.main(["decode_worker", str(tmp_path)])

        assert events == ["adjust", "decode"]


class TestTheSynthesisSessionMapsItsWeights:
    """The 2 GiB -> 1.3 GiB change, pinned where it is easy to lose.

    Prepacking copies the 832 MiB of int8 weights onto the heap; mapped, they are
    clean file-backed pages the kernel can evict instead of OOM-killing. Measured
    under hard cgroup caps: prepacked is killed at 1536 MiB, mapped completes at
    1280 MiB, with bit-identical output.
    """

    class _Options:
        """Enough of ORT's SessionOptions to record what was asked of it."""

        def __init__(self) -> None:
            self.entries: dict[str, str] = {}
            self.intra_op_num_threads = 0
            self.inter_op_num_threads = 0
            self.execution_mode = None

        def add_session_config_entry(self, key: str, value: str) -> None:
            self.entries[key] = value

    def _stub_ort(self, monkeypatch, created: list):
        """An onnxruntime stand-in, so this runs without the 958 MiB bundle."""
        from types import SimpleNamespace

        from app.imaging.aeic import onnx_backend

        test = self

        class _Ort:
            SessionOptions = staticmethod(lambda: test._Options())
            ExecutionMode = SimpleNamespace(ORT_SEQUENTIAL="sequential")

            @staticmethod
            def InferenceSession(path, sess_options=None, providers=None):
                created.append((path, sess_options))
                return SimpleNamespace(path=path)

        monkeypatch.setattr(onnx_backend, "_require_onnxruntime", lambda: _Ort)
        return _Ort

    def _backend(self, monkeypatch, created: list):
        from app.imaging.aeic.onnx_backend import OnnxAeicBackend

        self._stub_ort(monkeypatch, created)

        class _Bundle:
            root = Path("models")
            rate_point = "ft32"
            decoder_graph_path = Path("models/decoder.onnx")
            entropy_decode_graph_path = Path("models/entropy_decode.onnx")
            entropy_graph_path = Path("models/entropy_side.onnx")

            @staticmethod
            def require(path, _what):
                return path

        return OnnxAeicBackend(_Bundle())

    def test_the_synthesis_session_is_created_with_mapped_weights(self, monkeypatch):
        """Pins the call site, not just the option builder: this is the whole
        2 GiB -> 1.3 GiB change, and it is one keyword argument wide."""
        created: list = []
        backend = self._backend(monkeypatch, created)

        backend._ensure_synthesis()

        (path, options) = created[-1]
        assert "decoder" in str(path)
        assert options.entries.get("session.disable_prepacking") == "1"

    def test_the_entropy_sessions_are_left_exactly_as_they_were(self, monkeypatch):
        """The entropy graphs must not be touched: they are 64 MB, and their
        arithmetic has to stay bit-identical to the sender's."""
        created: list = []
        backend = self._backend(monkeypatch, created)

        backend._ensure_decode_entropy()
        backend._ensure_encode_entropy()

        assert len(created) == 2
        for path, options in created:
            assert "entropy" in str(path)
            assert "session.disable_prepacking" not in options.entries
            assert options.intra_op_num_threads == 1


# The 123-byte bitstream of a real photo: sent from MCO Advanced to #bots as one
# GRP_DATA packet, captured off the air, and the vector the RF decode path is
# pinned against in tests/test_grp_data_rf_decode.py.
GOLDEN_BITSTREAM = bytes.fromhex(
    "114700D29992021110E70239A0661F880694E384C24F6474EA345B0CD1DC1BCEDD4CB115E1"
    "DBB973B57F35D09180E8FE63A25EA82883068E5F3362A20AB616B8E18D54E59D1686A9EA40"
    "63BA4B11E38944CAFF0B1681543B39D65F613064A1DEC844DD9E21394613CCF647F5E28C4F"
    "9240EBD7D8DC7693C1754545"
)


def _bundle_ready() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    from app.imaging.aeic.bundle import AeicBundle

    return AeicBundle(root=Path("data/models/aeic")).supports_decode


@pytest.mark.skipif(
    not _bundle_ready(),
    reason="the 958 MiB AEIC model bundle is not installed in data/models/aeic",
)
class TestARealPictureThroughARealWorker:
    async def test_the_captured_photo_decodes_out_of_process(self):
        """End to end on the real model: 123 bytes in, a 512x512 PNG out.

        Skipped wherever the bundle is not installed, which is most places -- but
        it is the only test that proves the spawn, the module path, the stdin/
        stdout handover and the model all line up at once.
        """
        service = AeicService()

        png = await service.decode_to_png(GOLDEN_BITSTREAM)

        assert png.startswith(b"\x89PNG")
        assert int.from_bytes(png[16:20], "big") == 512
        assert int.from_bytes(png[20:24], "big") == 512

    async def test_the_server_process_does_not_grow_by_a_decode(self):
        """The point of the worker: the ~1.3 GiB is spent somewhere disposable."""
        import resource
        import sys

        service = AeicService()
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

        await service.decode_to_png(GOLDEN_BITSTREAM)

        after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        scale = 1 if sys.platform == "darwin" else 1024  # ru_maxrss: bytes vs kB
        grew = (after - before) * scale
        # Generous on purpose: the claim is "no model was loaded in here", not a
        # byte count. A model would show up as ~1.3 GiB.
        assert grew < 300_000_000, (
            f"the server process grew by {grew / 1e6:.0f} MB during a decode that "
            "was supposed to happen in a child"
        )
