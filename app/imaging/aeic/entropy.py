"""The AEIC entropy layer -- everything between the ONNX tensors and rANS.

Port of ``lib/services/image_codec_entropy.dart`` from meshcore-open's MCO
Advanced fork, which in turn ports AEIC's ``codec_practical.py``
(``PixelCodec.compress`` / ``.decompress``). This module owns the parts that are
neither neural nor range coding:

* the four checkerboard masks (``get_mask_four_parts``),
* ``sequeeze`` / ``unsequeeze_with_mask`` (the 4:1 channel fold),
* ``my_build_indexes`` (scale -> CDF row selector),
* the z index array (``_build_indexes``: the channel arange),
* the fixed coder call order z, y0, y1, y2, y3.

## Why every detail here is load-bearing

The rANS decoder re-derives symbol probabilities by re-running the same network
and the same arithmetic. A single wrong symbol *position* -- an off-by-one in a
mask, the wrong channel-group permutation, ``squeeze`` folding in the wrong
order -- does not raise: it desynchronises the coder and produces a sharp,
plausible, wrong image. Both failure modes are silent, which is why
``tests/test_aeic_entropy.py`` replays upstream's recorded ONNX tensors through
this code and compares the symbol and index arrays element for element.

## float32, deliberately

Every tensor here is ``numpy.float32`` and every scalar a ``numpy.float32``,
because torch evaluated the reference on float32 tensors with float32 scalars.
Where the Dart port has to call an explicit ``f32()`` rounding helper (Dart has
no 32-bit double), numpy gets it from the dtype -- but the *intent* is the same
and widening any intermediate to float64 would change the last bit of
:func:`build_indexes`, whose measured boundary margin is only 1.12x.

The neural half arrives through :class:`AeicEntropyNetwork` and the coder
through the :mod:`app.imaging.aeic.rans` classes, both injected, so the
arithmetic is testable without a 64 MB graph or a 958 MiB download.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from app.imaging.aeic.rans import RansDecoder, RansEncoder

Z_CDF_GROUP = 0
"""CDF group indices, fixed by ``EntropyCoder.update()``'s ``add_cdf`` call
order: the entropy bottleneck (z) is registered first, the Gaussian conditional
(y) second. The group index is part of the bitstream format."""

Y_CDF_GROUP = 1

SCALES_LEVELS = 64
"""``my_build_indexes`` constants (``codec_practical.py``, ``SCALES_MIN/MAX/LEVELS``)."""

LOG_SCALE_MIN = np.float32(-2.2072749131897207)  # ln(0.11)
LOG_SCALE_STEP = np.float32(0.12305479932808384)  # (ln(256) - ln(0.11)) / 63
SCALE_THRESHOLD = np.float32(0.08)  # below this the symbol is skipped entirely
SCALE_FLOOR = np.float32(1e-5)  # torch.maximum floor, applied first

STAGE_PERMUTATION = (0, 3, 2, 1)
"""``perm[stage]``: XOR with the channel group to get the micro-pattern.

Reading ``get_mask_four_parts``: each mask is four channel-groups of ``M/4``
channels stacked, and each group carries one of the four 2x2 micro-patterns

    micro 0 = (y%2, x%2) == (0, 0)      micro 1 = (0, 1)
    micro 2 = (1, 0)                    micro 3 = (1, 1)

so ``micro k`` is live exactly where ``(y%2)*2 + (x%2) == k``. The stacking
order per stage is

    mask_0 = [m0, m1, m2, m3]     mask_1 = [m3, m2, m1, m0]
    mask_2 = [m2, m3, m0, m1]     mask_3 = [m1, m0, m3, m2]

which is exactly ``micro = group XOR perm[stage]``. Getting this permutation
wrong is the single easiest way to silently reorder every symbol in the stream.
"""


class AeicEntropyUnavailable(RuntimeError):
    """The entropy loop was asked to run without a piece it needs."""


class AeicEntropyCancelled(RuntimeError):
    """Cancellation, raised out of the entropy loop at a stage boundary."""


CancelCheck = Callable[[], bool]


@dataclass(frozen=True)
class AeicGeometry:
    """Tensor shapes for one image, derived the way ``compress()`` derives them.

    ``y`` is the image downsampled by 32; ``z`` is ``y`` downsampled by a
    further 4, rounded **up** (``compress()`` reflect-pads ``y`` to a multiple
    of 4 first).
    """

    resolution: int
    y_channels: int
    y_height: int
    y_width: int
    z_channels: int
    z_height: int
    z_width: int

    @classmethod
    def for_resolution(cls, resolution: int, y_channels: int = 256) -> AeicGeometry:
        if resolution <= 0 or resolution % 32 != 0:
            raise ValueError(
                f"resolution {resolution} must be a positive multiple of 32 (g_a downsamples by 32)"
            )
        if y_channels % 4 != 0:
            raise ValueError(
                f"y_channels {y_channels}: the four-part mask splits the channels "
                "into 4 equal groups"
            )
        y_side = resolution // 32
        z_side = (y_side + 3) // 4
        return cls(
            resolution=resolution,
            y_channels=y_channels,
            y_height=y_side,
            y_width=y_side,
            z_channels=y_channels // 2,
            z_height=z_side,
            z_width=z_side,
        )

    @property
    def squeezed_channels(self) -> int:
        """Channels after ``sequeeze`` folds the four groups together."""
        return self.y_channels // 4

    @property
    def y_elements(self) -> int:
        return self.y_channels * self.y_height * self.y_width

    @property
    def symbols_per_stage(self) -> int:
        """Symbols coded per y stage (``[1, M/4, y_h, y_w]`` flattened)."""
        return self.squeezed_channels * self.y_height * self.y_width

    @property
    def z_elements(self) -> int:
        return self.z_channels * self.z_height * self.z_width

    @property
    def y_shape(self) -> tuple[int, int, int, int]:
        return (1, self.y_channels, self.y_height, self.y_width)

    @property
    def z_shape(self) -> tuple[int, int, int, int]:
        return (1, self.z_channels, self.z_height, self.z_width)

    @property
    def image_shape(self) -> tuple[int, int, int, int]:
        return (1, 3, self.resolution, self.resolution)


class AeicMaskSet:
    """The four masks of ``get_mask_four_parts``, as index maps rather than tensors.

    All four operations this class exposes are driven by one per-stage map,
    :meth:`live_group_map`: at position ``(y, x)`` in ``stage``, exactly one of
    the four channel groups is live, and it is
    ``(((y & 1) << 1) | (x & 1)) ^ perm[stage]``. Because the map partitions
    every position, ``apply_mask`` / ``unsqueeze`` / ``merge_context`` are all
    exact selects -- never adds -- and so cannot perturb a float.
    """

    __slots__ = ("geometry", "_group_maps")

    def __init__(self, geometry: AeicGeometry) -> None:
        self.geometry = geometry
        h, w = geometry.y_height, geometry.y_width
        ys = np.arange(h, dtype=np.int8).reshape(h, 1)
        xs = np.arange(w, dtype=np.int8).reshape(1, w)
        pos_micro = ((ys & 1) << 1) | (xs & 1)
        self._group_maps = tuple(
            (pos_micro ^ np.int8(perm)).astype(np.int8) for perm in STAGE_PERMUTATION
        )

    def live_group_map(self, stage: int) -> np.ndarray:
        """``[y_h, y_w]`` of channel-group indices: which group is live where."""
        return self._group_maps[stage]

    @staticmethod
    def micro_for(stage: int, channel_group: int) -> int:
        """Micro-pattern index used by ``channel_group`` in ``stage``."""
        return channel_group ^ STAGE_PERMUTATION[stage]

    def _grouped(self, full: np.ndarray) -> np.ndarray:
        """View a ``[1, M, h, w]`` (or flat) tensor as ``[4, M/4, h, w]``."""
        g = self.geometry
        return full.reshape(4, g.squeezed_channels, g.y_height, g.y_width)

    def mask_tensor(self, stage: int) -> np.ndarray:
        """Materialise ``mask_i`` as ``[1, M, y_h, y_w]`` float32.

        Only needed to hand the mask to something else, or to check it in a test.
        """
        g = self.geometry
        gmap = self.live_group_map(stage)
        out = np.zeros((4, g.squeezed_channels, g.y_height, g.y_width), np.float32)
        for group in range(4):
            out[group][:, gmap == group] = 1.0
        return out.reshape(g.y_shape)

    def squeeze(self, full: np.ndarray) -> np.ndarray:
        """``sequeeze``: sum the four channel chunks as ``(g0 + g1) + (g2 + g3)``.

        The association matters in principle -- float addition is not associative
        -- so it is reproduced exactly, even though for a masked tensor three of
        the four terms are a hard zero.
        """
        g = self.geometry
        if full.size != g.y_elements:
            raise ValueError(f"expected {g.y_elements} elements ({g.y_shape}), got {full.size}")
        v = self._grouped(np.ascontiguousarray(full, dtype=np.float32))
        return (v[0] + v[1]) + (v[2] + v[3])

    def unsqueeze(self, squeezed: np.ndarray, stage: int) -> np.ndarray:
        """``unsequeeze_with_mask``: broadcast back to ``[1, M, h, w]``.

        Each element is kept only in the channel group whose mask is live there.
        """
        g = self.geometry
        expected = g.symbols_per_stage
        if squeezed.size != expected:
            raise ValueError(f"expected {expected} elements, got {squeezed.size}")
        src = np.ascontiguousarray(squeezed, dtype=np.float32).reshape(
            g.squeezed_channels, g.y_height, g.y_width
        )
        gmap = self.live_group_map(stage)
        out = np.zeros((4, g.squeezed_channels, g.y_height, g.y_width), np.float32)
        for group in range(4):
            sel = gmap == group
            out[group][:, sel] = src[:, sel]
        return out.reshape(g.y_shape)

    def apply_mask(self, full: np.ndarray, stage: int) -> np.ndarray:
        """``t * mask_i``, elementwise, into a fresh tensor."""
        g = self.geometry
        v = self._grouped(np.ascontiguousarray(full, dtype=np.float32))
        gmap = self.live_group_map(stage)
        out = np.zeros_like(v)
        for group in range(4):
            sel = gmap == group
            out[group][:, sel] = v[group][:, sel]
        return out.reshape(g.y_shape)

    def merge_context(self, base: np.ndarray, stage_latent: np.ndarray, stage: int) -> np.ndarray:
        """``base * (1 - mask_i) + stageLatent``, the context update between stages.

        ``stage_latent`` is already masked (it is :meth:`unsqueeze` output), so
        this is a select, not an add -- which is what keeps it exact in float32.
        """
        g = self.geometry
        out = self._grouped(np.array(base, dtype=np.float32, copy=True).reshape(g.y_shape))
        src = self._grouped(np.ascontiguousarray(stage_latent, dtype=np.float32))
        gmap = self.live_group_map(stage)
        for group in range(4):
            sel = gmap == group
            out[group][:, sel] = src[group][:, sel]
        return out.reshape(g.y_shape)


def build_indexes(scales: np.ndarray) -> np.ndarray:
    """``my_build_indexes`` -- scale -> CDF row selector, in float32.

    ::

        s   = max(scale, 1e-5)
        q   = (ln(s) - log_scale_min) / log_scale_step        # all float32
        idx = (s < 0.08) ? -1 : trunc(clamp(q, 0, 63))        # trunc, not round

    ``np.log`` on a float32 array is not guaranteed bit-identical to ORT's
    float32 ``Log``, and the measured boundary margin here is only 1.12x -- the
    tightest number in the whole system. Both ends of a RemoteTerm-to-RemoteTerm
    conversation run this same code, so they always agree with each other;
    against a peer on a different libm a scale sitting within one ULP of a
    bucket edge is the one place interop could diverge.
    """
    s = np.asarray(scales, dtype=np.float32).reshape(-1)
    # `raw > floor ? raw : floor`, not np.maximum: that keeps a NaN as NaN,
    # whereas the reference's comparison sends it to the floor.
    s = np.where(s > SCALE_FLOOR, s, SCALE_FLOOR).astype(np.float32)
    skip = s < SCALE_THRESHOLD
    q = ((np.log(s) - LOG_SCALE_MIN) / LOG_SCALE_STEP).astype(np.float32)
    q = np.clip(q, np.float32(0.0), np.float32(SCALES_LEVELS - 1))
    # astype truncates toward zero, matching torch's `Tensor.int()`.
    out = q.astype(np.int16)
    out[skip] = -1
    return out


def z_indexes(geometry: AeicGeometry) -> np.ndarray:
    """The z index array.

    ``_build_indexes`` broadcasts ``arange(C)`` over ``H x W``, so this is
    ``H*W`` copies of each channel index. Always >= 0.
    """
    plane = geometry.z_height * geometry.z_width
    return np.repeat(np.arange(geometry.z_channels, dtype=np.int16), plane)


def to_symbols(values: np.ndarray) -> np.ndarray:
    """float32 tensor of integers -> int16 symbols, asserting the cast is lossless.

    ``py_rans`` force-casts through ``py::array_t<int16_t>`` and the golden
    vectors store the post-cast values. Anything that does not survive the cast
    means the entropy model has gone somewhere the format cannot represent, and
    truncating quietly would desynchronise the decoder.
    """
    flat = np.asarray(values, dtype=np.float32).reshape(-1)
    out = flat.astype(np.int16)
    if not np.array_equal(out.astype(np.float32), flat):
        bad = int(np.argmax(out.astype(np.float32) != flat))
        raise ValueError(
            f"entropy symbol {flat[bad]!r} at index {bad} is not a lossless int16; "
            "the entropy model produced a value the bitstream format cannot carry"
        )
    return out


def rgb_to_chw(rgb: bytes | np.ndarray, resolution: int) -> np.ndarray:
    """Packed 8-bit RGB -> the graph's ``image`` input.

    Reproduces torchvision's ``ToTensor()`` + ``Normalize([0.5]*3, [0.5]*3)``:
    ``x = (b / 255 - 0.5) / 0.5``. The division by 0.5 is a power of two and so
    exact; it is written as ``* 2`` for that reason.
    """
    pixels = resolution * resolution
    flat = (
        np.frombuffer(rgb, dtype=np.uint8)
        if isinstance(rgb, bytes | bytearray)
        else np.asarray(rgb, dtype=np.uint8).reshape(-1)
    )
    if flat.size != pixels * 3:
        raise ValueError(
            f"expected {pixels * 3} bytes for {resolution}x{resolution} RGB, got {flat.size}"
        )
    # float64 divide then narrow, which is what a float32 store of `b / 255.0`
    # does; a direct float32 divide could double-round differently.
    normalized = (flat.astype(np.float64) / 255.0).astype(np.float32)
    scaled = (normalized - np.float32(0.5)).astype(np.float32) * np.float32(2.0)
    return np.ascontiguousarray(scaled.reshape(pixels, 3).T.reshape(1, 3, resolution, resolution))


def chw_to_rgb(chw: np.ndarray, resolution: int) -> bytes:
    """``[1, 3, H, W]`` floats in [-1, 1] -> packed 8-bit RGB, ``H * W * 3`` bytes.

    Values outside the range do occur -- the last conv is unbounded -- and must
    be clamped, not wrapped.
    """
    pixels = resolution * resolution
    flat = np.asarray(chw, dtype=np.float32).reshape(3, pixels)
    scaled = (flat.astype(np.float64) + 1.0) * 127.5
    # `.round()` on the reference is half-away-from-zero on a non-negative value
    # after clamping, which is what np.floor(x + 0.5) gives.
    clamped = np.clip(np.floor(np.clip(scaled, 0.0, 255.0) + 0.5), 0.0, 255.0)
    return clamped.astype(np.uint8).T.reshape(-1).tobytes()


@dataclass(frozen=True)
class EncodeSideTensors:
    """Everything the send side needs out of one forward pass of the entropy graph.

    Matches the outputs of ``aeic_entropy_side_fp32_op17.onnx``, which runs
    ``g_a``, ``h_a``, ``h_s``, ``g_c`` and all four adapters in one shot -- the
    encoder never has to be incremental, only the decoder does.
    """

    z_q: np.ndarray
    """``round(z - z_offset)``, ``[1, M/2, z_h, z_w]``, integral floats."""

    y_q: tuple[np.ndarray, ...]
    """``round(y * mask_i - means_i)`` per stage, ``[1, M, y_h, y_w]``, integral."""

    scales: tuple[np.ndarray, ...]
    """``scales_supp_i * mask_i`` per stage, ``[1, M, y_h, y_w]``."""


@dataclass(frozen=True)
class StageParams:
    """One decode stage's entropy parameters.

    ``adapter_out[i](g_c(adapter_in[i](base)))`` split in half. The shipped
    decode graph already multiplies these by that stage's mask; the entropy loop
    masks again, which is exactly idempotent because the mask is a select.
    """

    means_supp: np.ndarray
    scales_supp: np.ndarray


class AeicEntropyNetwork(ABC):
    """The neural half of the entropy path, as this module needs it.

    **Send side** (``aeic_entropy_side_fp32_op17.onnx``, 64 MB): input
    ``image [1,3,512,512]`` -> outputs ``z_q``, ``yq0..3``, ``sc0..3``.

    **Decode side** (``aeic_entropy_decode_fp32_op17.onnx``, 58 MB): the decoder
    cannot use the send-side graph, because it must interleave network
    evaluation with symbol decoding -- stage ``i``'s indexes are unknown until
    stages ``< i`` have been decoded and fed back through ``g_c``. Inputs
    ``z_q``, ``base``, ``stage``; outputs ``base0``, ``means``, ``scales``.
    """

    @property
    @abstractmethod
    def supports_decode_side(self) -> bool:
        """False for a graph that only carries the send-side path."""

    @abstractmethod
    def run_encode_side(self, image_chw: np.ndarray) -> EncodeSideTensors: ...

    @abstractmethod
    def run_hyper_synthesis(self, z_q: np.ndarray) -> np.ndarray:
        """``h_s(z_q + z_offset)[:, :, :y_h, :y_w]``."""

    @abstractmethod
    def run_stage(self, stage: int, base: np.ndarray) -> StageParams:
        """``adapter_out[stage](g_c(adapter_in[stage](base)))``."""


class AeicEntropyCodec:
    """The four-stage masked encode and decode.

    Orchestrates an :class:`AeicEntropyNetwork` and the rANS coder. Everything
    about the call order here is format, not preference: z, then y0..y3, in that
    order, on both sides.
    """

    def __init__(
        self,
        geometry: AeicGeometry,
        network: AeicEntropyNetwork,
        tables,
    ) -> None:
        self.geometry = geometry
        self.network = network
        self.tables = tables
        self.masks = AeicMaskSet(geometry)

    def encode(
        self,
        rgb_bytes: bytes,
        *,
        on_progress: Callable[[float], None] | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> bytes:
        """Packed 8-bit RGB -> rANS bitstream (payload only, no framing).

        One forward pass, then z, y0, y1, y2, y3 into the coder in that order.
        """
        _progress(on_progress, 0.02)
        image = rgb_to_chw(rgb_bytes, self.geometry.resolution)
        _check_cancel(should_cancel)

        tensors = self.network.run_encode_side(image)
        _progress(on_progress, 0.80)
        _check_cancel(should_cancel)

        if tensors.z_q.size != self.geometry.z_elements:
            raise ValueError(
                f"entropy graph returned {tensors.z_q.size} z values, expected "
                f"{self.geometry.z_elements} ({self.geometry.z_shape})"
            )
        if len(tensors.y_q) != 4 or len(tensors.scales) != 4:
            raise ValueError(
                "entropy graph must return four y_q and four scales tensors, got "
                f"{len(tensors.y_q)} / {len(tensors.scales)}"
            )

        encoder = RansEncoder(self.tables)
        encoder.encode_with_indexes(
            to_symbols(tensors.z_q).tolist(),
            z_indexes(self.geometry).tolist(),
            Z_CDF_GROUP,
        )
        for stage in range(4):
            symbols = to_symbols(self.masks.squeeze(tensors.y_q[stage]))
            indexes = build_indexes(self.masks.squeeze(tensors.scales[stage]))
            encoder.encode_with_indexes(symbols.tolist(), indexes.tolist(), Y_CDF_GROUP)
            _progress(on_progress, 0.80 + 0.04 * (stage + 1))
            _check_cancel(should_cancel)

        stream = encoder.finish()
        _progress(on_progress, 1.0)
        return stream

    def decode_to_latent(
        self,
        bitstream: bytes,
        *,
        on_progress: Callable[[float], None] | None = None,
        should_cancel: CancelCheck | None = None,
    ) -> np.ndarray:
        """rANS bitstream -> ``y_hat [1, M, y_h, y_w]``, ready for synthesis.

        The mirror of :meth:`encode`, and necessarily incremental: each stage's
        indexes come from scales that only exist once the previous stage's
        symbols have been decoded and pushed back through the context model.
        """
        if not self.network.supports_decode_side:
            raise AeicEntropyUnavailable(
                "the installed entropy graph is send-side only: it maps image -> "
                "symbols and has no z_q -> base0 / base -> means,scales entry "
                "points, which decoding requires"
            )
        _progress(on_progress, 0.02)
        decoder = RansDecoder(self.tables, bitstream)

        z_symbols = decoder.decode_stream(z_indexes(self.geometry).tolist(), Z_CDF_GROUP)
        z_q = np.asarray(z_symbols, dtype=np.float32).reshape(self.geometry.z_shape)
        _check_cancel(should_cancel)

        base = self.network.run_hyper_synthesis(z_q)
        if base.size != self.geometry.y_elements:
            raise ValueError(
                f"hyper synthesis returned {base.size} values, expected "
                f"{self.geometry.y_elements} ({self.geometry.y_shape})"
            )
        base = base.reshape(self.geometry.y_shape)
        _progress(on_progress, 0.20)

        stage_latent: np.ndarray | None = None
        for stage in range(4):
            _check_cancel(should_cancel)
            params = self.network.run_stage(stage, base)
            scales = self.masks.apply_mask(params.scales_supp, stage)
            means = self.masks.apply_mask(params.means_supp, stage)
            indexes = build_indexes(self.masks.squeeze(scales))
            symbols = decoder.decode_stream(indexes.tolist(), Y_CDF_GROUP)
            means_squeezed = self.masks.squeeze(means)
            if len(symbols) != means_squeezed.size:
                raise ValueError(
                    f"stage {stage} decoded {len(symbols)} symbols but the context "
                    f"model produced {means_squeezed.size} means"
                )
            latent_squeezed = (
                np.asarray(symbols, dtype=np.float32).reshape(means_squeezed.shape) + means_squeezed
            ).astype(np.float32)
            stage_latent = self.masks.unsqueeze(latent_squeezed, stage)
            if stage < 3:
                base = self.masks.merge_context(base, stage_latent, stage)
            _progress(on_progress, 0.20 + 0.19 * (stage + 1))

        assert stage_latent is not None
        # y_hat = base * (1 - mask_3) + y_hat_3
        return self.masks.merge_context(base, stage_latent, 3)


def _progress(callback: Callable[[float], None] | None, value: float) -> None:
    if callback is not None:
        callback(value)


def _check_cancel(should_cancel: CancelCheck | None) -> None:
    if should_cancel is not None and should_cancel():
        raise AeicEntropyCancelled("Image processing stopped.")


def sequence_to_int16(values: Sequence[int]) -> np.ndarray:
    """Small helper for tests and callers holding plain lists of symbols."""
    return np.asarray(values, dtype=np.int16)
