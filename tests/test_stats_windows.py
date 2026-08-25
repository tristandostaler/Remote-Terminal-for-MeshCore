"""Tests for the statistics time-window helpers."""

from app.stats_windows import (
    DEFAULT_STATS_WINDOW,
    STATS_WINDOWS,
    TARGET_CHART_BUCKETS,
    bucket_seconds_for_span,
    is_valid_window,
    window_cutoff,
    window_seconds,
)


class TestWindowKeys:
    def test_default_window_is_a_known_key(self):
        assert is_valid_window(DEFAULT_STATS_WINDOW)

    def test_unknown_keys_are_rejected(self):
        assert not is_valid_window("1decade")
        assert not is_valid_window("")

    def test_windows_are_ordered_narrowest_first(self):
        spans = [seconds for seconds in STATS_WINDOWS.values() if seconds is not None]
        assert spans == sorted(spans)

    def test_all_is_unbounded(self):
        assert window_seconds("all") is None
        assert window_cutoff("all", 1_700_000_000) is None

    def test_cutoff_is_now_minus_the_span(self):
        assert window_cutoff("1d", 1_700_000_000) == 1_700_000_000 - 86400
        assert window_cutoff("1w", 1_700_000_000) == 1_700_000_000 - 604800

    def test_unknown_key_falls_back_to_the_default(self):
        """Internal callers must not blow up the whole snapshot over a typo."""
        assert window_seconds("nonsense") == window_seconds(DEFAULT_STATS_WINDOW)


class TestBucketSizing:
    def test_keeps_series_near_the_target_size(self):
        for seconds in STATS_WINDOWS.values():
            if seconds is None:
                continue
            bucket = bucket_seconds_for_span(seconds)
            assert seconds / bucket <= TARGET_CHART_BUCKETS * 1.5

    def test_never_goes_below_the_sample_interval(self):
        """A minute-resolution series gains nothing from 5-second slots."""
        assert bucket_seconds_for_span(600, minimum=60) == 60

    def test_wider_spans_get_wider_buckets(self):
        hour = bucket_seconds_for_span(3600)
        week = bucket_seconds_for_span(604800)
        year = bucket_seconds_for_span(31536000)
        assert hour < week < year

    def test_degenerate_span_returns_the_minimum(self):
        assert bucket_seconds_for_span(0) == 60
        assert bucket_seconds_for_span(-5, minimum=300) == 300

    def test_absurd_span_is_clamped_to_the_widest_step(self):
        """A decade of data still resolves to a bucket, not an exception."""
        assert bucket_seconds_for_span(10 * 31536000) == 2592000
