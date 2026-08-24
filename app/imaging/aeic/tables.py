"""Parser for the AEIC CDF table file that ships in the image-codec bundle.

Port of ``lib/services/entropy_tables.dart`` from meshcore-open's MCO Advanced
fork. The file itself is produced by the AEIC research repo's
``export_golden.py::write_table_file``; layout is little-endian and tightly
packed, with no padding:

    off  type      field
    0    char[8]   magic            = "AEICCDF\\x01"
    8    u32       version          = 1
    12   u32       precision        = 16
    16   u32       bypassPrecision  = 2
    20   u32       streamParts      = 2
    24   u32       numGroups        = 2
    28   ...       group blocks (group 0 = z, group 1 = y)
         ...       index-quantizer block
         char[4]   "END\\0"

group block::

    u32      numCdfs   R
    u32      cdfWidth  W
    i32[R]   cdfLength      row r is valid only on [0, cdfLength[r])
    i32[R]   offset         symbol offset for row r
    i32[R*W] quantizedCdf   row-major

index-quantizer block::

    char[4]  "IDXP"
    f64      logScaleMin
    f64      logScaleStep
    u32      scalesLevels
    f32      scaleThreshold   (scale below this => index -1)
    f32      scaleFloor
    f32[N]   scaleTable

Deliberately stdlib-only (``struct`` + ``array``): this module and
:mod:`app.imaging.aeic.rans` are the two halves of the wire format, and their
tests must run on an install that never opted into the ``aeic`` extra.
"""

from __future__ import annotations

import struct
from array import array
from dataclasses import dataclass

MAGIC = b"AEICCDF\x01"
_IDXP = b"IDXP"
_END = b"END\x00"
_HEADER_BYTES = 32


class EntropyTableFormatError(ValueError):
    """The CDF table file is malformed or of an unsupported version."""


@dataclass(frozen=True)
class CdfGroup:
    """One CDF group: ``z`` (entropy bottleneck) or ``y`` (Gaussian conditional)."""

    num_cdfs: int
    """Number of CDF rows (``R``)."""

    cdf_width: int
    """Stride of a row in :attr:`quantized_cdf` (``W``). Entries at or beyond
    ``cdf_length[row]`` are padding and must never be read."""

    cdf_length: array
    """Valid length of each row; ``cdf_length[r] - 2`` is the escape symbol."""

    offset: array
    """Symbol offset per row."""

    quantized_cdf: array
    """Row-major CDF table, ``R * W`` int32 entries."""

    def cdf_at(self, row: int, col: int) -> int:
        return self.quantized_cdf[row * self.cdf_width + col]


@dataclass(frozen=True)
class IndexQuantizerParams:
    """Constants that reproduce ``my_build_indexes`` (scale -> CDF row).

    Carried for completeness and for cross-checking
    :func:`app.imaging.aeic.entropy.build_indexes`, which hardcodes the same
    values so the entropy layer does not have to be handed a table file just to
    quantize a scale.
    """

    log_scale_min: float
    log_scale_step: float
    scales_levels: int
    scale_threshold: float
    scale_floor: float
    scale_table: array


@dataclass(frozen=True)
class EntropyTables:
    """The parsed contents of ``aeic_cdf_ft32.bin``."""

    version: int
    precision: int
    """rANS probability precision in bits (16)."""

    bypass_precision: int
    """Bits per bypass symbol (2)."""

    stream_parts: int
    """Number of interleaved rANS sub-streams the bitstream is split into (2)."""

    groups: tuple[CdfGroup, ...]
    """CDF groups in file order: index 0 = z, index 1 = y."""

    index_quantizer: IndexQuantizerParams

    @property
    def z_group(self) -> CdfGroup:
        return self.groups[0]

    @property
    def y_group(self) -> CdfGroup:
        return self.groups[1]


class _Cursor:
    __slots__ = ("data", "off")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.off = 0

    def need(self, count: int) -> None:
        if self.off < 0 or self.off + count > len(self.data):
            raise EntropyTableFormatError(
                f"truncated file: needed {count} bytes at {self.off} of {len(self.data)}"
            )

    def u32(self) -> int:
        self.need(4)
        (value,) = struct.unpack_from("<I", self.data, self.off)
        self.off += 4
        return value

    def i32_array(self, count: int) -> array:
        self.need(count * 4)
        out = array("i")
        out.frombytes(self.data[self.off : self.off + count * 4])
        if out.itemsize != 4:  # pragma: no cover - CPython's 'i' is always 4
            raise EntropyTableFormatError("this platform has no 32-bit array type")
        if struct.pack("<i", 1) != struct.pack("=i", 1):  # pragma: no cover - LE hosts
            out.byteswap()
        self.off += count * 4
        return out

    def f32_array(self, count: int) -> array:
        self.need(count * 4)
        out = array("f")
        out.frombytes(self.data[self.off : self.off + count * 4])
        if struct.pack("<f", 1.0) != struct.pack("=f", 1.0):  # pragma: no cover
            out.byteswap()
        self.off += count * 4
        return out

    def literal(self, expected: bytes, what: str) -> None:
        self.need(len(expected))
        found = self.data[self.off : self.off + len(expected)]
        if found != expected:
            raise EntropyTableFormatError(f"missing {what} block at byte {self.off}")
        self.off += len(expected)


def parse_entropy_tables(data: bytes) -> EntropyTables:
    """Parse a CDF table file, raising :class:`EntropyTableFormatError` on any
    structural problem.

    Validation is strict on purpose. A table set that disagrees with the
    checkpoint does not fail loudly at decode time -- it desynchronises rANS and
    yields a sharp, plausible, *wrong* image with no error anywhere. Refusing a
    file that is not exactly the expected shape is the only cheap defence.
    """
    if len(data) < _HEADER_BYTES:
        raise EntropyTableFormatError(f"file too short ({len(data)} B)")
    if not data.startswith(MAGIC):
        raise EntropyTableFormatError("bad magic")

    cursor = _Cursor(data)
    cursor.off = len(MAGIC)

    version = cursor.u32()
    if version != 1:
        raise EntropyTableFormatError(f"unsupported version {version}")
    precision = cursor.u32()
    bypass_precision = cursor.u32()
    stream_parts = cursor.u32()
    num_groups = cursor.u32()

    if not 0 < precision <= 16:
        raise EntropyTableFormatError(f"bad precision {precision}")
    if not 0 < bypass_precision < precision:
        raise EntropyTableFormatError(f"bad bypassPrecision {bypass_precision}")
    if not 1 <= stream_parts <= 16:
        raise EntropyTableFormatError(f"bad streamParts {stream_parts}")
    if not 1 <= num_groups <= 64:
        raise EntropyTableFormatError(f"bad numGroups {num_groups}")

    groups: list[CdfGroup] = []
    for g in range(num_groups):
        rows = cursor.u32()
        width = cursor.u32()
        if not 0 < rows <= 1 << 20 or not 0 < width <= 1 << 24:
            raise EntropyTableFormatError(f"group {g} has bad shape {rows}x{width}")
        cdf_length = cursor.i32_array(rows)
        offset = cursor.i32_array(rows)
        cdf = cursor.i32_array(rows * width)
        for row in range(rows):
            n = cdf_length[row]
            if not 2 <= n <= width:
                raise EntropyTableFormatError(
                    f"group {g} row {row} has cdfLength {n} (width {width})"
                )
        groups.append(
            CdfGroup(
                num_cdfs=rows,
                cdf_width=width,
                cdf_length=cdf_length,
                offset=offset,
                quantized_cdf=cdf,
            )
        )

    cursor.literal(_IDXP, "IDXP")
    cursor.need(8 + 8 + 4 + 4 + 4)
    log_scale_min, log_scale_step, levels, scale_threshold, scale_floor = struct.unpack_from(
        "<ddIff", cursor.data, cursor.off
    )
    cursor.off += 28
    if not 0 < levels <= 1 << 20:
        raise EntropyTableFormatError(f"bad scalesLevels {levels}")
    scale_table = cursor.f32_array(levels)

    cursor.literal(_END, "END")
    if cursor.off != len(data):
        raise EntropyTableFormatError(f"trailing data: parsed {cursor.off} of {len(data)} bytes")

    return EntropyTables(
        version=version,
        precision=precision,
        bypass_precision=bypass_precision,
        stream_parts=stream_parts,
        groups=tuple(groups),
        index_quantizer=IndexQuantizerParams(
            log_scale_min=log_scale_min,
            log_scale_step=log_scale_step,
            scales_levels=levels,
            scale_threshold=scale_threshold,
            scale_floor=scale_floor,
            scale_table=scale_table,
        ),
    )
