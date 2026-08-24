"""Loaders for the AEIC golden fixtures under ``tests/fixtures/aeic``.

The fixtures come from meshcore-open branch ``origin/rename-mco-advanced``
(``test/services/golden/``) and are what proves the port is bit-exact rather
than merely self-consistent -- a Python encoder and a Python decoder that agree
with each other prove nothing about the wire format.

Committed here: the CDF tables, all seven synthetic cases (which between them
exercise every tricky coder path -- escape exact, escape mixed, dense, long
bypass, skipped indexes, edges, tiny) and the two real images that bracket the
measured size range (kodim01 at 136 B, kodim08 at 209 B).

NOT committed, because they are 7.4 MB each: the five ``.aeicrec`` ONNX-I/O
recordings and the other eight image vectors. To validate against those, extract
them from a meshcore-open checkout::

    git show origin/rename-mco-advanced:test/services/golden/e2e/kodim01.aeicrec > kodim01.aeicrec

and point ``AEIC_RECORDING_DIR`` at the directory. ``tests/test_aeic_onnx.py``
picks them up automatically and skips when they are absent.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from pathlib import Path

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "aeic"
TABLES_PATH = FIXTURE_DIR / "aeic_cdf_ft32.bin"
VECTOR_DIR = FIXTURE_DIR / "vectors"

_GV_MAGIC = b"AEICGV\x00\x01"
_REC_MAGIC = b"AEICREC1"
_DT_INT16 = 0


def manifest() -> dict:
    return json.loads((FIXTURE_DIR / "manifest.json").read_text())


def read_gv(path: Path) -> dict[str, list[int]]:
    """Read a ``.gv`` symbol/index container.

    Layout (``tools/aeic/export_golden.py::write_gv_file``), little-endian::

        char[8]  magic = "AEICGV\\x00\\x01"
        u32      version = 1
        u32      n_arrays
        -- n_arrays fixed 24-byte descriptors:
             char[16] name, NUL-padded
             u32      dtype (0 = int16, 1 = int32)
             u32      count
        -- payloads back to back, in descriptor order
    """
    raw = path.read_bytes()
    if raw[:8] != _GV_MAGIC:
        raise ValueError(f"{path}: bad magic")
    version, count = struct.unpack_from("<2I", raw, 8)
    if version != 1:
        raise ValueError(f"{path}: unsupported version {version}")
    offset = 16
    descriptors = []
    for _ in range(count):
        name = raw[offset : offset + 16].rstrip(b"\x00").decode()
        dtype, size = struct.unpack_from("<2I", raw, offset + 16)
        descriptors.append((name, dtype, size))
        offset += 24
    out: dict[str, list[int]] = {}
    for name, dtype, size in descriptors:
        code = "h" if dtype == _DT_INT16 else "i"
        width = 2 if dtype == _DT_INT16 else 4
        out[name] = list(struct.unpack_from(f"<{size}{code}", raw, offset))
        offset += size * width
    if offset != len(raw):
        raise ValueError(f"{path}: trailing data")
    return out


@dataclass(frozen=True)
class GoldenCase:
    """One golden case: the arrays to encode and the bytes that must come out."""

    label: str
    pairs: tuple[tuple[list[int], list[int], int], ...]
    """``(symbols, indexes, cdf_group)`` per coder call, in format order."""

    expected_bitstream: bytes
    expected_sha256: str
    substream_sizes: list[int]
    container_flag: int


def synthetic_cases() -> list[GoldenCase]:
    """The seven single-call cases that pin the coder's edge behaviour."""
    data = manifest()
    cases = []
    for record in data["synthetic"]:
        arrays = read_gv(VECTOR_DIR / record["vector_file"])
        cases.append(
            GoldenCase(
                label=record["name"],
                pairs=((arrays["symbols"], arrays["indexes"], record["cdf_group"]),),
                expected_bitstream=(VECTOR_DIR / record["bitstream_file"]).read_bytes(),
                expected_sha256=record["bitstream_sha256"],
                substream_sizes=record["substream_sizes"],
                container_flag=record["container_flag"],
            )
        )
    return cases


def image_cases() -> list[GoldenCase]:
    """Real-image cases: five coder calls each, in the order z, y0, y1, y2, y3."""
    data = manifest()
    z_group = data["z_cdf_group_index"]
    y_group = data["y_cdf_group_index"]
    cases = []
    for record in data["images"]:
        arrays = read_gv(VECTOR_DIR / record["vector_file"])
        pairs = [(arrays["z_q"], arrays["z_indexes"], z_group)]
        for stage in range(4):
            pairs.append((arrays[f"y_q{stage}"], arrays[f"y_indexes{stage}"], y_group))
        cases.append(
            GoldenCase(
                label=record["stem"],
                pairs=tuple(pairs),
                expected_bitstream=(VECTOR_DIR / record["bitstream_file"]).read_bytes(),
                expected_sha256=record["bitstream_sha256"],
                substream_sizes=record["substream_sizes"],
                container_flag=record["container_flag"],
            )
        )
    return cases


def read_recording(path: Path) -> tuple[dict, dict]:
    """Read an ``.aeicrec`` ONNX-I/O recording: ``(json index, name -> ndarray)``.

    Needs numpy, so it is only called from the tests that already require it.
    """
    import numpy as np

    dtypes = {"f32": np.float32, "i32": np.int32, "i16": np.int16, "u8": np.uint8}
    raw = path.read_bytes()
    if raw[:8] != _REC_MAGIC:
        raise ValueError(f"{path}: bad magic")
    version, _flags, index_offset, index_length, _reserved = struct.unpack("<IIQII", raw[8:32])
    if version != 1:
        raise ValueError(f"{path}: unsupported version {version}")
    index = json.loads(raw[index_offset : index_offset + index_length].decode())
    arrays = {}
    for entry in index["entries"]:
        dtype = dtypes[entry["dtype"]]
        arrays[entry["name"]] = np.frombuffer(
            raw,
            dtype=dtype,
            count=entry["length"] // np.dtype(dtype).itemsize,
            offset=entry["offset"],
        ).reshape(entry["shape"])
    return index, arrays
