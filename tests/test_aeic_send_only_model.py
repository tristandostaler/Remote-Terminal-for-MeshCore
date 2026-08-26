"""Sending AI pictures without the 958 MiB bundle.

The bundle splits cleanly in two and only one half is anyone's decision:

* **send half** -- the send-side entropy graph and the CDF tables, 65 MiB on disk
  and ~0.35 GiB of memory. Fetched automatically, because a server that can run
  the codec at all should be able to send with it.
* **receive half** -- the 832 MiB of synthesis weights and the decode-side graph,
  which cost another 893 MiB on disk and ~1.4 GiB of memory *per picture*. That
  stays an explicit choice: a small Pi should not be volunteered for it.

The halves are separable but never mixable -- every asset is per-checkpoint, and
a send half from one checkpoint with a receive half from another desynchronises
rANS silently.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from app.imaging.aeic import service as service_module
from app.imaging.aeic.bundle import (
    AEIC_SE_FT32_ASSETS,
    BUNDLE_TOTAL_BYTES,
    SEND_HALF_ASSETS,
    SEND_HALF_TOTAL_BYTES,
    AeicAssetRole,
    AeicBundle,
)
from app.imaging.aeic.service import AeicService, AeicUnavailable

MODEL_DIR = Path("data/models/aeic")


def _bundle_ready() -> bool:
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return AeicBundle(root=MODEL_DIR).supports_decode


requires_bundle = pytest.mark.skipif(
    not _bundle_ready(), reason=f"the 958 MiB AEIC model bundle is not installed in {MODEL_DIR}"
)


@pytest.fixture
def service(tmp_path) -> AeicService:
    aeic = AeicService()
    aeic._model_dir = tmp_path
    return aeic


class TestWhatTheSendHalfIs:
    def test_it_is_exactly_what_encoding_asks_for(self, tmp_path):
        """Not a hand-picked list: whatever ``supports_encode`` needs, and no
        more. A file in here that encoding does not need is 64 MiB of somebody's
        uplink spent for nothing."""
        for asset in SEND_HALF_ASSETS:
            (tmp_path / asset.file_name).write_bytes(b"")

        bundle = AeicBundle(root=tmp_path)
        assert bundle.supports_encode is True
        assert bundle.supports_decode is False

    def test_dropping_either_file_takes_sending_with_it(self, tmp_path):
        for skipped in SEND_HALF_ASSETS:
            for asset in SEND_HALF_ASSETS:
                path = tmp_path / asset.file_name
                path.unlink(missing_ok=True)
                if asset is not skipped:
                    path.write_bytes(b"")
            assert AeicBundle(root=tmp_path).supports_encode is False, (
                f"sending claimed to work without {skipped.file_name}"
            )

    def test_the_weights_are_not_in_it(self):
        """The 832 MiB file is the whole reason for splitting at all."""
        roles = {asset.role for asset in SEND_HALF_ASSETS}
        assert AeicAssetRole.DECODER_WEIGHTS not in roles
        assert AeicAssetRole.DECODER_GRAPH not in roles
        assert AeicAssetRole.ENTROPY_DECODE_GRAPH not in roles
        assert SEND_HALF_TOTAL_BYTES < BUNDLE_TOTAL_BYTES / 10

    def test_it_is_a_subset_of_the_bundle(self):
        assert set(SEND_HALF_ASSETS) <= set(AEIC_SE_FT32_ASSETS)


class TestStartingAScopedDownload:
    async def test_the_send_scope_fetches_only_the_send_half(self, service, monkeypatch):
        seen = {}

        async def fake_download(root, *, assets, on_progress=None, should_cancel=None):
            seen["assets"] = assets
            return AeicBundle(root=root)

        monkeypatch.setattr(service_module, "download_bundle", fake_download)

        assert service.start_download(send_half_only=True) is True
        await service._download_task

        assert seen["assets"] == SEND_HALF_ASSETS

    async def test_the_default_still_fetches_everything(self, service, monkeypatch):
        seen = {}

        async def fake_download(root, *, assets, on_progress=None, should_cancel=None):
            seen["assets"] = assets
            return AeicBundle(root=root)

        monkeypatch.setattr(service_module, "download_bundle", fake_download)

        assert service.start_download() is True
        await service._download_task

        assert seen["assets"] == AEIC_SE_FT32_ASSETS

    async def test_progress_is_reported_against_the_half_in_flight(self, service, monkeypatch):
        """65 MiB measured against 958 MiB reads 3% and then stops, which looks
        exactly like a download that died."""
        released = []

        async def fake_download(root, *, assets, on_progress=None, should_cancel=None):
            on_progress(assets[0].file_name, assets[0].size_bytes // 2, assets[0].size_bytes)
            released.append(service.status())
            return AeicBundle(root=root)

        monkeypatch.setattr(service_module, "download_bundle", fake_download)
        service.start_download(send_half_only=True)
        await service._download_task

        (status,) = released
        assert status["download_scope"] == "send"
        assert status["download_target_bytes"] == SEND_HALF_TOTAL_BYTES
        assert status["download_done_bytes"] == SEND_HALF_ASSETS[0].size_bytes // 2
        assert status["send_half_total_bytes"] == SEND_HALF_TOTAL_BYTES

    def test_a_resting_service_reports_no_download(self, service):
        status = service.status()
        assert status["download_scope"] is None
        assert status["download_target_bytes"] == 0
        assert status["download_done_bytes"] == 0


class TestTheAutomaticFetch:
    @pytest.fixture(autouse=True)
    def _runtime(self, monkeypatch):
        monkeypatch.setattr(service_module, "onnxruntime_available", lambda: True)
        monkeypatch.setattr(service_module.settings, "enable_aeic", True)

    def test_it_starts_when_sending_is_not_possible_yet(self, service, monkeypatch):
        started = []
        monkeypatch.setattr(
            service, "start_download", lambda **kw: bool(started.append(kw)) or True
        )

        assert service.ensure_send_half_installed() is True
        assert started == [{"send_half_only": True}]

    def test_it_does_nothing_once_sending_works(self, service, monkeypatch):
        for asset in SEND_HALF_ASSETS:
            (service.model_dir / asset.file_name).write_bytes(b"")
        monkeypatch.setattr(service, "start_download", lambda **_: pytest.fail("re-downloaded"))

        assert service.ensure_send_half_installed() is False

    def test_it_does_not_interrupt_a_download_in_flight(self, service, monkeypatch):
        """A full download in progress already covers the send half."""
        monkeypatch.setattr(type(service), "is_downloading", property(lambda _self: True))
        monkeypatch.setattr(service, "start_download", lambda **_: pytest.fail("second download"))

        assert service.ensure_send_half_installed() is False

    def test_an_explicit_off_switch_is_honoured(self, service, monkeypatch):
        """MESHCORE_ENABLE_AEIC=false must not spend anyone's bandwidth."""
        monkeypatch.setattr(service_module.settings, "enable_aeic", False)
        monkeypatch.setattr(service, "start_download", lambda **_: pytest.fail("downloaded anyway"))

        assert service.ensure_send_half_installed() is False

    def test_it_stays_quiet_without_the_runtime(self, service, monkeypatch):
        monkeypatch.setattr(service_module, "onnxruntime_available", lambda: False)
        monkeypatch.setattr(service, "start_download", lambda **_: pytest.fail("downloaded anyway"))

        assert service.ensure_send_half_installed() is False

    def test_no_event_loop_is_not_a_crash(self, service, monkeypatch):
        """`ensure_send_half_installed` is called from startup and from a send;
        a synchronous caller off the loop must get False, not a traceback."""

        def no_loop(**_kwargs):
            raise RuntimeError("no running event loop")

        monkeypatch.setattr(service, "start_download", no_loop)

        assert service.ensure_send_half_installed() is False


class TestASendThatCannotYetSend:
    async def test_it_starts_the_fetch_and_says_to_try_again(self, service, monkeypatch):
        """So a server that had no uplink at boot fixes itself on first use
        rather than needing a restart."""
        monkeypatch.setattr(service_module, "onnxruntime_available", lambda: True)
        monkeypatch.setattr(service_module.settings, "enable_aeic", True)
        started = []
        monkeypatch.setattr(
            service, "start_download", lambda **kw: bool(started.append(kw)) or True
        )

        # The same 65 as the settings panel shows, in the MiB this bundle's
        # sizes have always been quoted in.
        with pytest.raises(AeicUnavailable, match="65 MiB"):
            await service.encode_rgb(b"\\x00" * (512 * 512 * 3))

        assert started == [{"send_half_only": True}], "a send did not trigger the fetch"

    def test_a_fetch_in_flight_reads_as_not_yet(self, service, monkeypatch):
        monkeypatch.setattr(service_module, "onnxruntime_available", lambda: True)
        monkeypatch.setattr(service_module.settings, "enable_aeic", True)
        monkeypatch.setattr(type(service), "is_downloading", property(lambda _self: True))

        reason = service.unavailable_reason(for_decode=False)

        assert reason is not None and "Try again in a moment" in reason


class TestStartupFetchesIt:
    """Startup is where "sending always works" actually comes from."""

    @staticmethod
    def _stubbed_startup(ensure):
        """The lifespan with everything but the AEIC call stubbed out."""
        from contextlib import ExitStack
        from unittest.mock import AsyncMock, patch

        stack = ExitStack()
        for target in (
            "app.main.db.connect",
            "app.main.db.disconnect",
            "app.radio_sync.ensure_default_channels",
            "app.radio.radio_manager.start_connection_monitor",
            "app.radio.radio_manager.stop_connection_monitor",
            "app.radio.radio_manager.disconnect",
            "app.radio.radio_manager.post_connect_setup",
            "app.fanout.manager.fanout_manager.load_from_db",
            "app.fanout.manager.fanout_manager.stop_all",
            "app.radio_sync.stop_message_polling",
            "app.radio_sync.stop_periodic_advert",
            "app.radio_sync.stop_periodic_sync",
        ):
            stack.enter_context(patch(target, new=AsyncMock()))
        stack.enter_context(
            patch("app.radio.radio_manager.reconnect", new=AsyncMock(return_value=True))
        )
        stack.enter_context(patch("app.websocket.broadcast_health"))
        stack.enter_context(
            patch.object(service_module.aeic_service, "ensure_send_half_installed", ensure)
        )
        return stack

    async def _run_lifespan(self) -> None:
        import asyncio

        from app.main import app, lifespan

        cm = lifespan(app)
        await asyncio.wait_for(cm.__aenter__(), timeout=5.0)
        await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=10.0)

    async def test_the_lifespan_asks_for_the_send_half(self):
        calls = []
        with self._stubbed_startup(lambda: calls.append(True)):
            await self._run_lifespan()

        assert calls == [True], "startup did not try to make sending work"

    async def test_a_failure_there_does_not_stop_the_server_starting(self):
        """A gateway with no uplink at boot must still come up. The next send
        tries again, so this costs a picture, never the server."""

        def explode():
            raise OSError("network unreachable")

        with self._stubbed_startup(explode):
            await self._run_lifespan()  # must not raise


@requires_bundle
class TestARealSendOnlyInstall:
    """The claim, on the real model: 65 MiB installed and sending works."""

    @pytest.fixture
    def send_only(self, tmp_path) -> Path:
        for asset in SEND_HALF_ASSETS:
            shutil.copy(MODEL_DIR / asset.file_name, tmp_path / asset.file_name)
        return tmp_path

    async def test_a_real_encode_runs_with_only_the_send_half(self, send_only):
        service = AeicService()
        service._model_dir = send_only

        assert service.unavailable_reason(for_decode=False) is None
        bitstream = await service.encode_rgb(bytes(512 * 512 * 3))

        assert 0 < len(bitstream) < 400, f"a 512px encode produced {len(bitstream)} bytes"

    async def test_receiving_is_still_refused_and_says_what_is_missing(self, send_only):
        service = AeicService()
        service._model_dir = send_only

        reason = service.unavailable_reason(for_decode=True)

        assert reason is not None
        assert "3 of 5 files missing" in reason

    async def test_a_scoped_download_of_what_is_present_touches_no_network(
        self, send_only, monkeypatch
    ):
        """Also proves the scope is honoured: iterating all five assets here
        would try to fetch the three that are absent."""
        import httpx

        def refuse(*_args, **_kwargs):
            raise AssertionError("the downloader went to the network")

        monkeypatch.setattr(httpx.AsyncClient, "stream", refuse)

        bundle = await service_module.download_bundle(send_only, assets=SEND_HALF_ASSETS)

        assert bundle.supports_encode is True
