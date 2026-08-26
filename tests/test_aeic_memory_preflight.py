"""Reading how much memory this host really has, before attempting a decode.

The reason this is not one call to ``psutil``: RemoteTerm is usually a container,
and inside one ``/proc/meminfo`` reports the *host's* free memory. A 512 MB
container on a 16 GB machine reads as 15 GB free and then gets its decode killed
anyway, which is exactly the crash this preflight exists to explain. The cgroup
limit is the number that decides, so the smaller of the two wins.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.imaging.aeic import service as service_module
from app.imaging.aeic.memory import (
    DECODE_REFUSE_BELOW_BYTES,
    available_memory_bytes,
    decode_memory_shortfall,
)
from app.imaging.aeic.service import AeicService


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _meminfo(root: Path, kilobytes: int) -> None:
    _write(
        root / "proc/meminfo",
        f"MemTotal:       16384000 kB\nMemAvailable:   {kilobytes} kB\nBuffers: 1 kB\n",
    )


class TestWhatTheHostReports:
    def test_meminfo_is_read_in_bytes(self, tmp_path):
        _meminfo(tmp_path, 2_000_000)  # 2 GB expressed in kB
        assert available_memory_bytes(tmp_path) == 2_000_000 * 1024

    def test_a_cgroup_limit_beats_a_roomy_host(self, tmp_path):
        """The container case, and the whole reason this module exists."""
        _meminfo(tmp_path, 15_000_000)  # the host has ~15 GB free
        _write(tmp_path / "sys/fs/cgroup/memory.max", "536870912\n")  # 512 MiB cap
        _write(tmp_path / "sys/fs/cgroup/memory.current", "134217728\n")  # 128 MiB used

        assert available_memory_bytes(tmp_path) == 536870912 - 134217728

    def test_an_uncapped_cgroup_defers_to_the_host(self, tmp_path):
        _meminfo(tmp_path, 3_000_000)
        _write(tmp_path / "sys/fs/cgroup/memory.max", "max\n")
        _write(tmp_path / "sys/fs/cgroup/memory.current", "134217728\n")

        assert available_memory_bytes(tmp_path) == 3_000_000 * 1024

    def test_cgroup_v1_is_read_too(self, tmp_path):
        _write(tmp_path / "sys/fs/cgroup/memory/memory.limit_in_bytes", "1073741824\n")
        _write(tmp_path / "sys/fs/cgroup/memory/memory.usage_in_bytes", "73741824\n")

        assert available_memory_bytes(tmp_path) == 1073741824 - 73741824

    def test_the_v1_no_limit_sentinel_is_not_a_limit(self, tmp_path):
        """An unset v1 limit is a huge sentinel, not something anyone configured."""
        _meminfo(tmp_path, 2_000_000)
        _write(tmp_path / "sys/fs/cgroup/memory/memory.limit_in_bytes", f"{(1 << 63) - 4096}\n")
        _write(tmp_path / "sys/fs/cgroup/memory/memory.usage_in_bytes", "1000\n")

        assert available_memory_bytes(tmp_path) == 2_000_000 * 1024

    def test_a_host_with_neither_answers_none(self, tmp_path):
        """macOS and Windows. None means "no answer", never "no memory"."""
        assert available_memory_bytes(tmp_path) is None
        assert decode_memory_shortfall(tmp_path) is None


class TestTheRefusal:
    def test_a_host_too_small_is_told_so_with_both_numbers(self, tmp_path):
        _meminfo(tmp_path, 400_000)  # ~410 MB free

        sentence = decode_memory_shortfall(tmp_path)

        assert sentence is not None
        assert "410 MB" in sentence, sentence
        assert "1.4 GB" in sentence
        # Not a dead end: the bitstream is kept either way, so the picture can be
        # opened from somewhere else.
        assert "picture is kept" in sentence

    def test_a_host_just_above_the_line_gets_to_try(self, tmp_path):
        """The measured floor sits between 1024 and 1280 MiB and depends on the
        page cache, so the refusal is set well below it: a host in that band
        attempts the decode, and the worker process contains a wrong guess."""
        _meminfo(tmp_path, (DECODE_REFUSE_BELOW_BYTES + 50_000_000) // 1024)

        assert decode_memory_shortfall(tmp_path) is None


class TestTheDecodePreflight:
    def test_receiving_is_refused_and_sending_is_not(self, monkeypatch):
        """Encoding peaks at 0.35 GiB, so a host that cannot reconstruct can
        still send. Refusing both would take away the half that works."""
        service = AeicService()
        monkeypatch.setattr(service_module, "onnxruntime_available", lambda: True)
        monkeypatch.setattr(
            service_module, "decode_memory_shortfall", lambda: "This server has only 410 MB free."
        )

        class _Bundle:
            supports_encode = True
            supports_decode = True

            def missing_assets(self):
                return ()

        monkeypatch.setattr(service, "bundle", _Bundle)

        assert service.unavailable_reason(for_decode=True) == "This server has only 410 MB free."
        assert service.unavailable_reason(for_decode=False) is None

    async def test_the_refusal_reaches_the_caller_before_anything_is_spawned(self, monkeypatch):
        service = AeicService()
        monkeypatch.setattr(
            service, "unavailable_reason", lambda **_: "This server has only 410 MB free."
        )

        def must_not_run(_bitstream):
            raise AssertionError("a decode was attempted on a host that cannot fit one")

        monkeypatch.setattr(service, "_decode_in_worker", must_not_run)
        monkeypatch.setattr(service, "_decode_in_process", must_not_run)

        with pytest.raises(service_module.AeicUnavailable, match="410 MB free"):
            await service.decode_to_png(b"bitstream")
