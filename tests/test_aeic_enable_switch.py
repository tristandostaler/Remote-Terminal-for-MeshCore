"""``MESHCORE_ENABLE_AEIC`` as the runtime switch for *reconstruction*.

The variable used to be install-time only: ``run.sh`` read it to decide whether
to ``uv pip install`` the optional extra. That made it one-way. On a server that
had once had it true, the dependency and the 958 MiB bundle were already on disk,
so setting it back to false changed nothing -- onnxruntime still imported, the
model still loaded, and received images were still reconstructed.

Made a runtime switch, it then went too far the other way and took sending with
it. The two halves do not cost the same thing: rebuilding a received picture is
893 MiB of weights and ~1.4 GiB of memory *per picture*, which is what anyone
turning this off is turning off; sending is 65 MiB and ~0.35 GiB and never loads
any of it. So false now means "never rebuild", and sending is left alone.

These tests pin the tri-state the app reads at runtime:

* unset -> autodetect, the historical behaviour (a dev checkout that ran
  ``uv sync --extra aeic`` without ever setting the variable must keep working)
* false -> never reconstruct, regardless of what is installed; sending unaffected
* true  -> on, but it cannot conjure a missing dependency or model
"""

from __future__ import annotations

import pathlib

import pytest

from app.config import settings
from app.imaging.aeic.service import AeicUnavailable, aeic_service


@pytest.fixture
def enable_aeic(monkeypatch):
    """Set ``settings.enable_aeic`` for one test."""

    def _set(value):
        monkeypatch.setattr(settings, "enable_aeic", value)

    return _set


class _ReadyBundle:
    supports_encode = True
    supports_decode = True

    def missing_assets(self):
        return []

    def installed_bytes(self, assets=None):
        return 0

    def path_for(self, _asset):
        return pathlib.Path("nowhere")


class TestExplicitFalseStopsRebuilding:
    def test_false_reports_unavailable_for_decode(self, enable_aeic):
        enable_aeic(False)
        reason = aeic_service.unavailable_reason(for_decode=True)
        assert reason is not None
        assert "MESHCORE_ENABLE_AEIC" in reason

    def test_false_says_the_picture_is_kept(self, enable_aeic):
        """It is not lost, and switching the codec back on decodes it -- which is
        the difference between a setting and a shredder."""
        enable_aeic(False)
        reason = aeic_service.unavailable_reason(for_decode=True)
        assert reason is not None and "picture is kept" in reason

    def test_false_leaves_sending_alone(self, enable_aeic, monkeypatch):
        """The point of this switch is the ~1.4 GiB reconstruction, not the codec.

        A host told not to rebuild pictures can still send them: 65 MiB on disk
        and ~0.35 GiB of memory, none of it the synthesis weights.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)
        monkeypatch.setattr(aeic_service, "bundle", lambda: _ReadyBundle())
        enable_aeic(False)

        assert aeic_service.unavailable_reason(for_decode=False) is None
        aeic_service._require_ready(for_decode=False)  # must not raise

    def test_false_wins_even_when_everything_is_installed(self, enable_aeic, monkeypatch):
        """The whole point: installed dependency + installed model, still off.

        This is the exact state the bug lived in, so it is asserted rather than
        inferred -- both the runtime probe and the bundle are forced to report
        ready, and the codec must still refuse.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)
        monkeypatch.setattr(aeic_service, "bundle", lambda: _ReadyBundle())

        # Sanity: with the switch unset this configuration IS usable, so the
        # assertion below is about the switch and nothing else.
        enable_aeic(None)
        assert aeic_service.unavailable_reason(for_decode=True) is None

        enable_aeic(False)
        assert aeic_service.unavailable_reason(for_decode=True) is not None

    def test_false_blocks_the_decode_entry_point(self, enable_aeic):
        enable_aeic(False)
        with pytest.raises(AeicUnavailable):
            aeic_service._require_ready(for_decode=True)

    def test_status_separates_the_switch_from_the_dependency(self, enable_aeic, monkeypatch):
        """``runtime_available`` is the dependency and nothing else.

        The panel renders it as "switched off, set MESHCORE_ENABLE_AEIC=true and
        restart -- it installs ~120 MB", which is only ever true of a missing
        onnxruntime. Folding the switch into it hid a server that could still
        send, and told the reader to install something already installed.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)
        monkeypatch.setattr(aeic_service, "bundle", lambda: _ReadyBundle())
        enable_aeic(False)

        status = aeic_service.status()

        assert status["runtime_available"] is True
        assert status["reconstruction_enabled"] is False
        assert status["supports_encode"] is True, "sending was taken away with rebuilding"
        assert status["supports_decode"] is False


class TestUnsetKeepsAutodetect:
    def test_unset_does_not_by_itself_disable_the_codec(self, enable_aeic, monkeypatch):
        """Unset must mean "autodetect", not "off".

        Every dev checkout that ran ``uv sync --extra aeic`` without setting the
        variable depends on this, so treating unset as off would be a silent
        regression for them.
        """
        monkeypatch.setattr("app.imaging.aeic.service.onnxruntime_available", lambda: True)
        monkeypatch.setattr(aeic_service, "bundle", lambda: _ReadyBundle())
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
