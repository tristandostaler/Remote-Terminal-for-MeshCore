"""The AEIC entropy layer: masks, the channel fold, and the scale quantizer.

Every detail here is load-bearing in a way that fails SILENTLY. A wrong symbol
position -- an off-by-one in a mask, the wrong channel-group permutation,
``squeeze`` folding in the wrong order -- does not raise. It desynchronises rANS
and produces a sharp, plausible, wrong image.

The strongest tests replay upstream's recorded ONNX tensors through this code and
compare the symbol and index arrays element for element; those live in
``test_aeic_onnx.py`` because they need the recordings. What is here is the
arithmetic that can be checked in isolation.

Needs numpy (the optional ``aeic`` extra).
"""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy", reason="the AEIC entropy layer needs numpy")

from app.imaging.aeic.entropy import (  # noqa: E402
    LOG_SCALE_MIN,
    LOG_SCALE_STEP,
    SCALE_FLOOR,
    SCALE_THRESHOLD,
    SCALES_LEVELS,
    STAGE_PERMUTATION,
    AeicGeometry,
    AeicMaskSet,
    build_indexes,
    chw_to_rgb,
    rgb_to_chw,
    to_symbols,
    z_indexes,
)


@pytest.fixture(scope="module")
def geometry() -> AeicGeometry:
    return AeicGeometry.for_resolution(512)


@pytest.fixture(scope="module")
def masks(geometry) -> AeicMaskSet:
    return AeicMaskSet(geometry)


class TestGeometry:
    def test_derives_the_shipping_shapes(self, geometry):
        assert geometry.y_shape == (1, 256, 16, 16)
        assert geometry.z_shape == (1, 128, 4, 4)
        assert geometry.squeezed_channels == 64
        assert geometry.symbols_per_stage == 16384
        assert geometry.z_elements == 2048

    def test_z_side_rounds_up(self):
        """``compress()`` reflect-pads y to a multiple of 4 before h_a."""
        # y side 3 -> z side 1, not 0.
        assert AeicGeometry.for_resolution(96).z_height == 1
        assert AeicGeometry.for_resolution(96).y_height == 3

    @pytest.mark.parametrize("resolution", [0, -32, 100, 511])
    def test_rejects_a_resolution_g_a_cannot_downsample(self, resolution):
        with pytest.raises(ValueError, match="multiple of 32"):
            AeicGeometry.for_resolution(resolution)

    def test_rejects_a_channel_count_the_four_part_mask_cannot_split(self):
        with pytest.raises(ValueError, match="4 equal groups"):
            AeicGeometry.for_resolution(512, y_channels=254)


class TestMasks:
    def test_the_four_masks_partition_the_tensor_exactly_once(self, masks):
        """Every latent must be coded in exactly one stage. If the masks
        overlapped, a symbol would be coded twice; if they left a gap, a latent
        would never be transmitted at all."""
        total = sum(masks.mask_tensor(stage) for stage in range(4))
        assert np.all(total == 1.0)

    def test_stage_permutation_matches_get_mask_four_parts(self, masks):
        """mask_0 = [m0,m1,m2,m3], mask_1 = [m3,m2,m1,m0],
        mask_2 = [m2,m3,m0,m1], mask_3 = [m1,m0,m3,m2].

        Getting this permutation wrong is the single easiest way to silently
        reorder every symbol in the stream, so it is asserted against the
        literal stacking rather than against itself.
        """
        expected = [[0, 1, 2, 3], [3, 2, 1, 0], [2, 3, 0, 1], [1, 0, 3, 2]]
        for stage, groups in enumerate(expected):
            for group, micro in enumerate(groups):
                assert AeicMaskSet.micro_for(stage, group) == micro
        assert STAGE_PERMUTATION == (0, 3, 2, 1)

    def test_live_group_map_is_the_inverse_of_micro_for(self, masks):
        gmap = masks.live_group_map(1)
        for y in range(4):
            for x in range(4):
                group = int(gmap[y, x])
                assert AeicMaskSet.micro_for(1, group) == ((y & 1) << 1) | (x & 1)

    def test_squeeze_folds_four_groups_with_the_reference_association(self, masks, geometry):
        """``(g0 + g1) + (g2 + g3)``. Float addition is not associative, so the
        grouping is reproduced exactly even though for a masked tensor three of
        the four terms are a hard zero."""
        rng = np.random.default_rng(7)
        full = rng.standard_normal(geometry.y_elements).astype(np.float32)
        grouped = full.reshape(4, 64, 16, 16)
        expected = (grouped[0] + grouped[1]) + (grouped[2] + grouped[3])
        assert np.array_equal(masks.squeeze(full), expected)

    def test_squeeze_then_unsqueeze_is_identity_on_a_masked_tensor(self, masks, geometry):
        """The pair is what moves a stage's latents between the coder's flat
        16,384 symbols and the network's [1,256,16,16] context."""
        rng = np.random.default_rng(11)
        for stage in range(4):
            full = rng.standard_normal(geometry.y_elements).astype(np.float32)
            masked = masks.apply_mask(full, stage)
            assert np.array_equal(masks.unsqueeze(masks.squeeze(masked), stage), masked)

    def test_apply_mask_is_a_select_and_so_is_idempotent(self, masks, geometry):
        """The shipped decode graph already masks its outputs; the entropy loop
        masks again. That is only safe because this is a select, not a multiply
        by a float."""
        rng = np.random.default_rng(13)
        full = rng.standard_normal(geometry.y_elements).astype(np.float32)
        once = masks.apply_mask(full, 2)
        assert np.array_equal(masks.apply_mask(once, 2), once)

    def test_merge_context_replaces_exactly_the_live_positions(self, masks, geometry):
        rng = np.random.default_rng(17)
        base = rng.standard_normal(geometry.y_elements).astype(np.float32)
        latent = masks.apply_mask(rng.standard_normal(geometry.y_elements).astype(np.float32), 0)
        merged = masks.merge_context(base, latent, 0)
        live = masks.mask_tensor(0).reshape(-1) == 1.0
        flat_merged, flat_base, flat_latent = (
            merged.reshape(-1),
            base.reshape(-1),
            latent.reshape(-1),
        )
        assert np.array_equal(flat_merged[live], flat_latent[live])
        assert np.array_equal(flat_merged[~live], flat_base[~live])

    def test_merge_context_does_not_mutate_its_input(self, masks, geometry):
        base = np.ones(geometry.y_elements, dtype=np.float32)
        before = base.copy()
        masks.merge_context(base, masks.apply_mask(base * 2, 0), 0)
        assert np.array_equal(base, before)


class TestBuildIndexes:
    """``my_build_indexes``: scale -> CDF row, in float32, truncating."""

    def test_a_scale_below_the_threshold_is_skipped(self):
        below = np.array([0.0, 1e-9, 0.0799], dtype=np.float32)
        assert np.all(build_indexes(below) == -1)

    def test_the_threshold_itself_is_coded(self):
        assert build_indexes(np.array([SCALE_THRESHOLD], dtype=np.float32))[0] == 0

    def test_the_bottom_of_the_scale_table_maps_to_row_zero(self):
        """exp(log_scale_min) is 0.11, which must land exactly on row 0."""
        assert build_indexes(np.array([0.11], dtype=np.float32))[0] == 0

    def test_the_top_of_the_scale_table_maps_to_the_last_row(self):
        assert build_indexes(np.array([256.0], dtype=np.float32))[0] == SCALES_LEVELS - 1

    def test_clamps_rather_than_wrapping_past_the_table(self):
        huge = np.array([1e9, 1e30], dtype=np.float32)
        assert np.all(build_indexes(huge) == SCALES_LEVELS - 1)

    def test_truncates_toward_zero_rather_than_rounding(self):
        """``Tensor.int()`` truncates. Rounding would shift a whole band of
        scales onto the neighbouring CDF row."""
        # Pick a scale whose quantized position sits just above an integer.
        step = float(LOG_SCALE_STEP)
        just_above = np.array([np.exp(float(LOG_SCALE_MIN) + 5 * step + step * 0.9)], np.float32)
        assert build_indexes(just_above)[0] == 5

    def test_is_monotonic_across_the_whole_range(self):
        scales = np.geomspace(0.08, 256.0, 4096).astype(np.float32)
        indexes = build_indexes(scales)
        assert np.all(np.diff(indexes.astype(np.int32)) >= 0)
        assert indexes.min() == 0
        assert indexes.max() == SCALES_LEVELS - 1

    def test_uses_every_row_of_the_table(self):
        scales = np.geomspace(0.11, 256.0, 100_000).astype(np.float32)
        assert set(build_indexes(scales).tolist()) == set(range(SCALES_LEVELS))

    def test_a_nan_scale_goes_to_the_floor_and_is_skipped(self):
        """The reference's comparison (`raw > floor ? raw : floor`) sends NaN to
        the floor, where np.maximum would have kept it as NaN."""
        assert build_indexes(np.array([np.nan], dtype=np.float32))[0] == -1

    def test_the_floor_is_applied_before_the_threshold(self):
        assert float(SCALE_FLOOR) < float(SCALE_THRESHOLD)
        assert build_indexes(np.array([0.0], dtype=np.float32))[0] == -1


class TestSymbolsAndPixels:
    def test_z_indexes_are_the_channel_arange_broadcast_over_the_plane(self, geometry):
        indexes = z_indexes(geometry)
        assert indexes.shape == (geometry.z_elements,)
        assert indexes[0] == 0
        assert indexes[-1] == geometry.z_channels - 1
        # 16 positions per channel for a 4x4 z plane.
        assert list(indexes[:17]) == [0] * 16 + [1]
        assert np.all(indexes >= 0)

    def test_to_symbols_accepts_integral_floats(self):
        values = np.array([-3.0, 0.0, 17.0], dtype=np.float32)
        assert list(to_symbols(values)) == [-3, 0, 17]

    @pytest.mark.parametrize("value", [0.5, 40000.0, -40000.0])
    def test_to_symbols_refuses_a_lossy_cast(self, value):
        """Truncating quietly would desynchronise the decoder, so a value the
        format cannot carry is an error, not a clamp."""
        with pytest.raises(ValueError, match="lossless int16"):
            to_symbols(np.array([value], dtype=np.float32))

    def test_rgb_to_chw_reproduces_totensor_plus_normalize(self):
        """``x = (b / 255 - 0.5) / 0.5``, planar, float32."""
        rgb = bytes([0, 128, 255] * 4)
        out = rgb_to_chw(rgb, 2)
        assert out.shape == (1, 3, 2, 2)
        assert out.dtype == np.float32
        # The expected values here are float64; the pipeline is float32 by
        # design, so the tolerance is float32-sized rather than exact.
        assert out[0, 0, 0, 0] == pytest.approx(-1.0, abs=1e-6)
        assert out[0, 1, 0, 0] == pytest.approx((128 / 255 - 0.5) * 2, abs=1e-6)
        assert out[0, 2, 0, 0] == pytest.approx(1.0, abs=1e-6)

    def test_rgb_to_chw_is_planar_not_interleaved(self):
        """A channel-order slip here would send the network a colour-swapped
        image, which decodes to a plausible picture in the wrong hues."""
        # Two pixels of real data followed by two black ones, as a 2x2 square.
        rgb = bytes([10, 20, 30, 40, 50, 60]) + bytes(6)
        out = rgb_to_chw(rgb, 2)
        red_plane = out[0, 0].reshape(-1)
        assert red_plane[0] == pytest.approx((10 / 255 - 0.5) * 2, abs=1e-6)
        assert red_plane[1] == pytest.approx((40 / 255 - 0.5) * 2, abs=1e-6)
        green_plane = out[0, 1].reshape(-1)
        assert green_plane[0] == pytest.approx((20 / 255 - 0.5) * 2, abs=1e-6)
        assert green_plane[1] == pytest.approx((50 / 255 - 0.5) * 2, abs=1e-6)

    def test_rgb_to_chw_rejects_the_wrong_byte_count(self):
        with pytest.raises(ValueError, match="expected"):
            rgb_to_chw(bytes(10), 512)

    def test_chw_to_rgb_clamps_the_unbounded_output(self):
        """The decoder's last conv is unbounded, so out-of-range values do occur
        and must be clamped rather than wrapped."""
        chw = np.array([[-5.0, 0.0, 5.0, 1.0]] * 3, dtype=np.float32).reshape(1, 3, 2, 2)
        out = chw_to_rgb(chw, 2)
        assert out[0:3] == bytes([0, 0, 0])
        assert out[6:9] == bytes([255, 255, 255])

    def test_pixel_round_trip_is_lossless_for_every_byte_value(self):
        rgb = bytes(range(256)) * 3
        side = 16
        assert len(rgb) == side * side * 3
        assert chw_to_rgb(rgb_to_chw(rgb, side), side) == rgb
