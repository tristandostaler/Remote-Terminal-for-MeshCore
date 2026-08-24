"""``MESHCORE_ENABLE_AEIC`` as a runtime kill switch.

The variable used to be install-time only: ``run.sh`` read it to decide whether
to ``uv pip install`` the optional extra. That made it one-way. On a server that
had once had it true, the dependency and the 958 MiB bundle were already on disk,
so setting it back to false changed nothing -- onnxruntime still imported, the
model still loaded, and received images were still reconstructed.

These tests pin the tri-state the app now reads at runtime:

* unset -> autodetect, the historical behaviour (a dev checkout that ran
  ``uv sync --extra aeic`` without ever setting the variable must keep working)
* false -> hard off, regardless of what is installed
* true  -> on, but it cannot conjure a missing dependency or model
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.imaging.aeic.service import AeicUnavailable, aeic_service


@pytest.fixture
def enable_aeic(monkeypatch):
    """Set ``settings.enable_aeic`` for one test."""

    def _set(value):
        monkeypatch.setattr(settings, "enable_aeic", value)

    return _set


class TestExplicitFalseIsHardOff:
    def test_false_reports_unavailable_for_decode(self, enable_aeic):
        enable_aeic(False)
        reason = aeic_service.unavailable_reason(for_decode=True)
        assert reason is not None
        assert "MESHCORE_ENABLE_AEIC" in reason

    def test_false_reports_unavailable_for_encode(self, enable_aeic):
        enable_aeic(False)
        assert aeic_service.unavailable_reason(for_decode=False) is not None

    def test_false_wins_even_when_everything_is_installed(self, enable_aeic, monkeypatch):
        """The whole point: installed dependency + installed model, still off.

        This is the exact state the bug lived in, so it is asserted rather than
        inferred -- both the runtime probe and the bundle are forced to report
        ready, and the codec must still refuse.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)

        class ReadyBundle:
            supports_encode = True
            supports_decode = True

            def missing_assets(self):
                return []

        monkeypatch.setattr(aeic_service, "bundle", lambda: ReadyBundle())

        # Sanity: with the switch unset this configuration IS usable, so the
        # assertion below is about the switch and nothing else.
        enable_aeic(None)
        assert aeic_service.unavailable_reason(for_decode=True) is None

        enable_aeic(False)
        assert aeic_service.unavailable_reason(for_decode=True) is not None

    def test_false_blocks_the_encode_entry_point(self, enable_aeic):
        enable_aeic(False)
        with pytest.raises(AeicUnavailable):
            aeic_service._require_ready(for_decode=False)

    def test_false_blocks_the_decode_entry_point(self, enable_aeic):
        enable_aeic(False)
        with pytest.raises(AeicUnavailable):
            aeic_service._require_ready(for_decode=True)

    def test_false_reports_no_runtime_in_status(self, enable_aeic, monkeypatch):
        """The settings panel keys off ``runtime_available``.

        Reporting it false is what makes the panel say "switched off on this
        server, set MESHCORE_ENABLE_AEIC=true" instead of offering to download a
        958 MiB model that nothing is allowed to load.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)
        enable_aeic(False)
        status = aeic_service.status()
        assert status["runtime_available"] is False
        assert status["supports_encode"] is False
        assert status["supports_decode"] is False


class TestUnsetKeepsAutodetect:
    def test_unset_does_not_by_itself_disable_the_codec(self, enable_aeic, monkeypatch):
        """Unset must mean "autodetect", not "off".

        Every dev checkout that ran ``uv sync --extra aeic`` without setting the
        variable depends on this, so treating unset as off would be a silent
        regression for them.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)

        class ReadyBundle:
            supports_encode = True
            supports_decode = True

            def missing_assets(self):
                return []

        monkeypatch.setattr(aeic_service, "bundle", lambda: ReadyBundle())
        enable_aeic(None)
        assert aeic_service.unavailable_reason(for_decode=True) is None

    def test_true_cannot_conjure_a_missing_runtime(self, enable_aeic, monkeypatch):
        """An explicit true still has to answer to reality."""
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: False)
        enable_aeic(True)
        reason = aeic_service.unavailable_reason(for_decode=True)
        assert reason is not None
        assert "onnxruntime" in reason


class TestEnvVarBinding:
    def test_the_setting_reads_meshcore_enable_aeic(self, monkeypatch):
        """The env var name is the contract; a rename would break every install."""
        from app.config import Settings

        monkeypatch.setenv("MESHCORE_ENABLE_AEIC", "false")
        assert Settings().enable_aeic is False

        monkeypatch.setenv("MESHCORE_ENABLE_AEIC", "true")
        assert Settings().enable_aeic is True

        monkeypatch.delenv("MESHCORE_ENABLE_AEIC")
        assert Settings().enable_aeic is None
