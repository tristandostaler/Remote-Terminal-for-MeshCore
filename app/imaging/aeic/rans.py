"""The AEIC rANS entropy coder.

Port of ``lib/services/rans_coder.dart`` from meshcore-open's MCO Advanced
fork, which is itself a port of the C++ reference in ``aeic/src/cpp/rans/``
(ryg_rans plus CompressAI's unbounded-index range coding).

THE OUTPUT MUST BE BYTE-IDENTICAL to that reference. AEIC's coder is
synchronous with the entropy model: the decoder re-runs the network to
reproduce the exact symbol probabilities the encoder used, so a single differing
byte does not raise -- it desynchronises the coder and silently emits a corrupt
latent that decodes to a sharp, plausible, wrong image. The golden vectors under
``tests/fixtures/aeic/`` pin this down.

Format recap:

* precision 16, bypass precision 2, ``RANS_L = 1 << 23``, 2 sub-streams.
* Each :meth:`RansEncoder.encode_with_indexes` call is split evenly across the
  sub-streams: part ``p`` covers ``[p * (n // parts), ...)``, the last part
  taking the remainder. Every part accumulates across all calls; the flush
  happens once.
* A sub-stream's first 4 bytes are the final rANS state, little-endian.
* Container: ``flag = ((n_parts - 1) << 4) | (1 if hdr_len == 2 else 0)``, then
  the lengths of the first ``n_parts - 1`` sub-streams (``hdr_len`` bytes each,
  little-endian), then the sub-streams back to back.

Notes on the port. The C++ state is ``uint32_t`` and Python ints are unbounded,
so every place the reference relies on 32-bit wraparound masks explicitly with
``& 0xFFFFFFFF``. Everywhere else the state invariant keeps it below ``2**31``
and no mask is needed -- but the masks that *are* here are load-bearing, not
defensive.

Stdlib-only, like :mod:`app.imaging.aeic.tables`: the wire format has to be
testable on an install that never opted into the ``aeic`` extra.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence

from app.imaging.aeic.tables import CdfGroup, EntropyTables

RANS_LOWER_BOUND = 1 << 23
"""Lower bound of the rANS normalisation interval (``RANS_BYTE_L``)."""

MAX_SUB_STREAMS = 16


class RansFormatError(ValueError):
    """A bitstream cannot be interpreted."""


def parse_rans_container(stream: bytes) -> list[bytes]:
    """Split the shipped bytes into rANS sub-streams (``RansDecoder::set_stream``)."""
    if not stream:
        raise RansFormatError("empty stream")
    flag = stream[0]
    n_streams = (flag >> 4) + 1
    hdr = 2 if (flag & 0x0F) == 1 else 4
    off = 1
    sizes: list[int] = []
    total = 0
    for _ in range(n_streams - 1):
        if off + hdr > len(stream):
            raise RansFormatError("truncated sub-stream size table")
        size = int.from_bytes(stream[off : off + hdr], "little")
        off += hdr
        sizes.append(size)
        total += size
    last = len(stream) - off - total
    if last < 0:
        raise RansFormatError("sub-stream sizes exceed the stream length")
    sizes.append(last)
    parts: list[bytes] = []
    pos = off
    for size in sizes:
        if pos + size > len(stream):
            raise RansFormatError("truncated sub-stream")
        parts.append(stream[pos : pos + size])
        pos += size
    return parts


def build_rans_container(parts: Sequence[bytes]) -> bytes:
    """Assemble sub-streams into the bytes that go on the air."""
    if not parts:
        raise RansFormatError("no sub-streams")
    if len(parts) > MAX_SUB_STREAMS:
        raise RansFormatError(f"too many sub-streams ({len(parts)})")
    # Only the sizes that are actually written decide the header width; the last
    # sub-stream's length is implied by the total and never stored.
    maximum = max((len(p) for p in parts[:-1]), default=0)
    hdr = 4 if maximum > 0xFFFF else 2
    flag = ((len(parts) - 1) << 4) | (1 if hdr == 2 else 0)
    out = bytearray([flag])
    for part in parts[:-1]:
        out += len(part).to_bytes(hdr, "little")
    for part in parts:
        out += part
    return bytes(out)


class RansEncoder:
    """rANS encoder.

    Call :meth:`encode_with_indexes` once per stage in format order
    (z, y0, y1, y2, y3), then :meth:`finish` exactly once.
    """

    __slots__ = (
        "_tables",
        "_stream_parts",
        "_precision",
        "_bypass_precision",
        "_entries",
        "_finished",
    )

    def __init__(self, tables: EntropyTables, stream_parts: int | None = None) -> None:
        self._tables = tables
        self._stream_parts = stream_parts if stream_parts is not None else tables.stream_parts
        self._precision = tables.precision
        self._bypass_precision = tables.bypass_precision
        # One flat (start, range) list per sub-stream. ``range == 0`` is the
        # bypass sentinel: ``start`` then carries the raw bits.
        self._entries: list[list[tuple[int, int]]] = [[] for _ in range(self._stream_parts)]
        self._finished = False

    def encode_with_indexes(
        self, symbols: Sequence[int], indexes: Sequence[int], cdf_group_index: int
    ) -> None:
        """Accumulate one encode call.

        ``symbols`` and ``indexes`` must be the same length; an index ``< 0``
        emits nothing at all.

        Never pass an odd-length array: the reference splitter -- and therefore
        the on-air format -- mis-sizes the last part's index vector in that case.
        """
        if self._finished:
            raise RuntimeError("RansEncoder.finish() has already been called")
        if len(symbols) != len(indexes):
            raise ValueError(f"symbols ({len(symbols)}) and indexes ({len(indexes)}) differ")
        if not 0 <= cdf_group_index < len(self._tables.groups):
            raise ValueError(f"no CDF group {cdf_group_index}")
        group = self._tables.groups[cdf_group_index]
        total = len(symbols)
        each = total // self._stream_parts
        for part in range(self._stream_parts):
            lo = part * each
            hi = total if part == self._stream_parts - 1 else lo + each
            self._push(self._entries[part], symbols, indexes, lo, hi, group)

    def _push(
        self,
        out: list[tuple[int, int]],
        symbols: Sequence[int],
        indexes: Sequence[int],
        lo: int,
        hi: int,
        group: CdfGroup,
    ) -> None:
        cdf = group.quantized_cdf
        cdf_length = group.cdf_length
        offsets = group.offset
        width = group.cdf_width
        num_cdfs = group.num_cdfs
        bypass_precision = self._bypass_precision
        max_bypass_val = (1 << bypass_precision) - 1
        append = out.append

        for i in range(lo, hi):
            cdf_idx = indexes[i]
            if cdf_idx < 0:
                continue
            if cdf_idx >= num_cdfs:
                raise RansFormatError(f"index {cdf_idx} out of range ({num_cdfs} CDF rows)")
            max_value = cdf_length[cdf_idx] - 2
            value = symbols[i] - offsets[cdf_idx]
            raw_val = 0
            if value < 0:
                raw_val = -2 * value - 1
                value = max_value
            elif value >= max_value:
                raw_val = 2 * (value - max_value)
                value = max_value

            base = cdf_idx * width
            start = cdf[base + value]
            append((start, cdf[base + value + 1] - start))

            if value == max_value:
                # Bypass mode: raw bits, ``bypass_precision`` at a time.
                n_bypass = 0
                while (raw_val >> (n_bypass * bypass_precision)) != 0:
                    n_bypass += 1
                val = n_bypass
                while val >= max_bypass_val:
                    append((max_bypass_val, 0))
                    val -= max_bypass_val
                append((val, 0))
                for j in range(n_bypass):
                    append(((raw_val >> (j * bypass_precision)) & max_bypass_val, 0))

    def finish(self) -> bytes:
        """Flush every sub-stream and return the container bytes."""
        if self._finished:
            raise RuntimeError("RansEncoder.finish() has already been called")
        self._finished = True
        return build_rans_container([self._flush(e) for e in self._entries])

    def reset(self) -> None:
        """Discard accumulated entries so the encoder can be reused."""
        for entries in self._entries:
            entries.clear()
        self._finished = False

    def _flush(self, entries: list[tuple[int, int]]) -> bytes:
        # Emitted in reverse; the sub-stream is this buffer reversed.
        sink = bytearray()
        append = sink.append
        precision = self._precision
        bypass_precision = self._bypass_precision
        x = RANS_LOWER_BOUND
        bypass_x_max = (1 << (precision - bypass_precision)) << 15
        for start, rng in reversed(entries):
            if rng != 0:
                x_max = rng << 15
                while x >= x_max:
                    append(x & 0xFF)
                    x >>= 8
                x = ((x // rng) << precision) + (x % rng) + start
            else:
                while x >= bypass_x_max:
                    append(x & 0xFF)
                    x >>= 8
                x = ((x << bypass_precision) | start) & 0xFFFFFFFF
        # RansEncFlush writes the 32-bit state little-endian at the front of the
        # stream, i.e. in emission order it is the high byte first.
        append((x >> 24) & 0xFF)
        append((x >> 16) & 0xFF)
        append((x >> 8) & 0xFF)
        append(x & 0xFF)
        sink.reverse()
        return bytes(sink)


class RansDecoder:
    """rANS decoder.

    Decoding is INCREMENTAL: construct once over the whole container, then call
    :meth:`decode_stream` per stage in the same order the encoder used
    (z, y0, y1, y2, y3). Each call resumes the sub-stream states where the
    previous one left off, because stage ``i``'s indexes are unknown until the
    earlier stages have been decoded and run back through the network.
    """

    __slots__ = ("_tables", "_precision", "_bypass_precision", "_parts", "_states", "_ptrs")

    def __init__(
        self, tables: EntropyTables, stream: bytes, stream_parts: int | None = None
    ) -> None:
        expected = stream_parts if stream_parts is not None else tables.stream_parts
        self._tables = tables
        self._precision = tables.precision
        self._bypass_precision = tables.bypass_precision
        self._parts = parse_rans_container(stream)
        if len(self._parts) != expected:
            raise RansFormatError(
                f"container has {len(self._parts)} sub-streams, expected {expected}"
            )
        self._states: list[int] = []
        self._ptrs: list[int] = []
        for i, part in enumerate(self._parts):
            if len(part) < 4:
                raise RansFormatError(f"sub-stream {i} is shorter than 4 bytes")
            self._states.append(int.from_bytes(part[:4], "little"))
            self._ptrs.append(4)

    @property
    def stream_parts(self) -> int:
        return len(self._parts)

    def decode_stream(self, indexes: Sequence[int], cdf_group_index: int) -> list[int]:
        """Decode one stage.

        Returns one symbol per entry of ``indexes``; positions whose index is
        ``< 0`` yield a literal 0 and consume nothing.
        """
        if not 0 <= cdf_group_index < len(self._tables.groups):
            raise ValueError(f"no CDF group {cdf_group_index}")
        group = self._tables.groups[cdf_group_index]
        cdf = group.quantized_cdf
        cdf_length = group.cdf_length
        offsets = group.offset
        width = group.cdf_width
        num_cdfs = group.num_cdfs
        precision = self._precision
        mask = (1 << precision) - 1
        bypass_precision = self._bypass_precision
        max_bypass_val = (1 << bypass_precision) - 1

        total = len(indexes)
        n_parts = len(self._parts)
        each = total // n_parts
        out = [0] * total

        for pi in range(n_parts):
            lo = pi * each
            hi = total if pi == n_parts - 1 else lo + each
            buf = self._parts[pi]
            buf_len = len(buf)
            x = self._states[pi]
            ptr = self._ptrs[pi]

            for i in range(lo, hi):
                cdf_idx = indexes[i]
                if cdf_idx < 0:
                    continue
                if cdf_idx >= num_cdfs:
                    raise RansFormatError(f"index {cdf_idx} out of range ({num_cdfs} CDF rows)")
                n = cdf_length[cdf_idx]
                max_value = n - 2
                base = cdf_idx * width
                cum = x & mask

                # upper_bound(row[0:n], cum) - 1
                s = bisect_right(cdf, cum, base, base + n) - base - 1
                if s < 0 or s >= n - 1:
                    raise RansFormatError(f"corrupt stream: symbol {s} out of range")
                start = cdf[base + s]
                rng = cdf[base + s + 1] - start

                x = (rng * (x >> precision) + cum - start) & 0xFFFFFFFF
                while x < RANS_LOWER_BOUND:
                    if ptr >= buf_len:
                        raise RansFormatError(f"sub-stream {pi} exhausted")
                    x = ((x << 8) | buf[ptr]) & 0xFFFFFFFF
                    ptr += 1

                value = s
                if value == max_value:
                    # Bypass mode: raw bits, ``bypass_precision`` at a time. The
                    # renormalisation is a single ``if``, not a loop -- that
                    # asymmetry with the symbol path above is part of the format,
                    # not an oversight.
                    #
                    # The read is written out twice rather than factored into a
                    # closure: this is the hot loop (~67,000 symbols per image),
                    # and a closure over `x`/`ptr` would both allocate per symbol
                    # and force those two into cells instead of plain locals.
                    n_bypass = 0
                    while True:
                        val = x & max_bypass_val
                        x >>= bypass_precision
                        if x < RANS_LOWER_BOUND:
                            if ptr >= buf_len:
                                raise RansFormatError(f"sub-stream {pi} exhausted")
                            x = ((x << 8) | buf[ptr]) & 0xFFFFFFFF
                            ptr += 1
                        n_bypass += val
                        if val != max_bypass_val:
                            break
                    raw_val = 0
                    for j in range(n_bypass):
                        val = x & max_bypass_val
                        x >>= bypass_precision
                        if x < RANS_LOWER_BOUND:
                            if ptr >= buf_len:
                                raise RansFormatError(f"sub-stream {pi} exhausted")
                            x = ((x << 8) | buf[ptr]) & 0xFFFFFFFF
                            ptr += 1
                        raw_val |= val << (j * bypass_precision)
                    value = raw_val >> 1
                    if raw_val & 1:
                        value = -value - 1
                    else:
                        value += max_value
                out[i] = value + offsets[cdf_idx]

            self._states[pi] = x
            self._ptrs[pi] = ptr
        return out
