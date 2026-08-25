"""Clock drift measured from advert timestamps.

Covers the math module, the bucket-folding write path, the per-contact summary
behind the info pane, and the repeater aggregate behind the statistics view.
"""

from unittest.mock import patch

import pytest

from app import clock_drift
from app.clock_drift import (
    DAY_SECONDS,
    DRIFT_BUCKET_SECONDS,
    DRIFT_FULL_RESOLUTION_SECONDS,
    classify_drift,
    day_start,
    drift_rate_per_day,
    histogram_bin,
    histogram_labels,
    is_unset_clock,
)
from app.models import ContactUpsert
from app.repository.contacts import (
    ContactAdvertPathRepository,
    ContactClockDriftRepository,
    ContactRepository,
)
from app.repository.settings import StatisticsRepository

NOW = 1_800_000_000
HOUR = 3600
REPEATER = 2


def _key(prefix: str) -> str:
    return (prefix * 32)[:64]


async def _add_repeater(name: str, key: str, *, last_seen: int = NOW) -> None:
    await ContactRepository.upsert(
        ContactUpsert(
            public_key=key,
            name=name,
            type=REPEATER,
            last_seen=last_seen,
            last_advert=last_seen,
            first_seen=last_seen,
        )
    )


class TestDriftMath:
    def test_bands_are_keyed_on_magnitude_not_direction(self):
        assert classify_drift(0) == "in_sync"
        assert classify_drift(-60) == "in_sync"
        assert classify_drift(61) == "minor"
        assert classify_drift(-300) == "minor"
        assert classify_drift(301) == "major"
        assert classify_drift(-3600) == "major"
        assert classify_drift(3601) == "severe"

    def test_a_clock_that_was_never_set_is_not_a_drifted_clock(self):
        assert is_unset_clock(0) is True
        assert is_unset_clock(60_000) is True
        assert is_unset_clock(NOW) is False

    def test_rate_needs_both_samples_and_a_lever_arm(self):
        # Two points prove nothing, however far apart.
        assert drift_rate_per_day([(0, 0), (86400, 100)]) is None
        # Three points inside an hour prove nothing either: a single reading's
        # noise floor (propagation delay, bucket edges) is seconds.
        assert drift_rate_per_day([(0, 0), (1800, 5), (3000, 9)]) is None

    def test_a_constant_offset_has_no_trend(self):
        points = [(i * HOUR, -400) for i in range(24)]
        assert drift_rate_per_day(points) == 0.0

    def test_a_walking_clock_reports_seconds_per_day(self):
        # Gains 10 seconds an hour == 240 seconds a day.
        points = [(i * HOUR, i * 10) for i in range(24)]
        assert drift_rate_per_day(points) == pytest.approx(240.0, abs=0.5)

    def test_histogram_bins_span_the_signed_range(self):
        labels = histogram_labels()
        assert histogram_bin(-90000) == 0
        assert histogram_bin(0) == labels.index("±1m")
        assert histogram_bin(-61) == labels.index("-5m…-1m")
        assert histogram_bin(600) == labels.index("5m…1h")
        assert histogram_bin(90000) == len(labels) - 1

    def test_median_survives_an_outlier_the_mean_would_not(self):
        values = [1.0, 2.0, 3.0, 1_000_000.0]
        assert clock_drift.median(values) == 2.5


class TestRecording:
    @pytest.mark.asyncio
    async def test_an_hour_of_arrivals_folds_into_one_bucket(self, test_db):
        key = _key("a")
        await _add_repeater("Alpha", key)

        # Same advert flooding in over three paths, plus a later one in the
        # same hour. The hour keeps one row.
        for observed, advert_ts, hops in (
            (NOW, NOW - 30, 3),
            (NOW + 2, NOW - 30, 2),
            (NOW + 5, NOW - 30, 1),
            (NOW + 900, NOW + 880, 0),
        ):
            await ContactClockDriftRepository.record(
                key, advert_timestamp=advert_ts, observed_at=observed, path_len=hops
            )

        async with test_db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contact_clock_drift WHERE public_key = ?", (key,)
            ) as cursor:
                rows = await cursor.fetchall()

        assert len(rows) == 1
        row = rows[0]
        assert row["sample_count"] == 4
        # -20 (the 900s-later arrival) beats -30/-32/-35: propagation delay only
        # ever pushes a reading negative, so the largest is the truest.
        assert row["drift_seconds"] == -20
        assert row["path_len"] == 0

    @pytest.mark.asyncio
    async def test_separate_hours_are_separate_rows(self, test_db):
        key = _key("b")
        await _add_repeater("Bravo", key)

        for i in range(5):
            observed = NOW + i * HOUR
            await ContactClockDriftRepository.record(
                key, advert_timestamp=observed - 100, observed_at=observed
            )

        async with test_db.readonly() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS cnt FROM contact_clock_drift WHERE public_key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        assert row["cnt"] == 5

    @pytest.mark.asyncio
    async def test_compaction_folds_old_hours_into_days_without_losing_them(self, test_db):
        """History is never deleted -- old hours merge into one row per day."""
        key = _key("c")
        await _add_repeater("Charlie", key)

        # Aligned to midnight so the 24 readings land in exactly one day; an
        # unaligned start straddles two, which is correct but not what this
        # test is checking.
        old_day = day_start(NOW - DRIFT_FULL_RESOLUTION_SECONDS - 10 * DAY_SECONDS)
        # A full day of hourly readings, well past the horizon...
        for hour in range(24):
            observed = old_day + hour * HOUR
            await ContactClockDriftRepository.record(
                key, advert_timestamp=observed - 100 - hour, observed_at=observed
            )
        # ...and a recent one that must keep its hourly resolution.
        for hour in range(5):
            observed = NOW - hour * HOUR
            await ContactClockDriftRepository.record(
                key, advert_timestamp=observed - 7, observed_at=observed
            )

        merged = await ContactClockDriftRepository.compact(now=NOW)
        assert merged == 23

        async with test_db.readonly() as conn:
            async with conn.execute(
                "SELECT bucket_start, drift_seconds, sample_count FROM contact_clock_drift "
                "WHERE public_key = ? ORDER BY bucket_start",
                (key,),
            ) as cursor:
                rows = await cursor.fetchall()

        # One daily row plus the five untouched recent hours.
        assert len(rows) == 6
        daily = rows[0]
        assert daily["bucket_start"] % DAY_SECONDS == 0
        # The day keeps its largest drift (hour 0, least penalised) and every
        # arrival counted across the hours it replaced.
        assert daily["drift_seconds"] == -100
        assert daily["sample_count"] == 24

    @pytest.mark.asyncio
    async def test_compaction_is_a_no_op_once_history_is_folded(self, test_db):
        """Safe to call on a timer forever."""
        key = _key("f")
        await _add_repeater("Foxtrot", key)

        old_day = day_start(NOW - DRIFT_FULL_RESOLUTION_SECONDS - 30 * DAY_SECONDS)
        for hour in range(12):
            observed = old_day + hour * HOUR
            await ContactClockDriftRepository.record(
                key, advert_timestamp=observed - 60, observed_at=observed
            )

        assert await ContactClockDriftRepository.compact(now=NOW) == 11
        assert await ContactClockDriftRepository.compact(now=NOW) == 0
        assert await ContactClockDriftRepository.compact(now=NOW) == 0

    @pytest.mark.asyncio
    async def test_a_lone_reading_off_midnight_is_still_moved_to_its_day(self, test_db):
        """Otherwise one straggler row per day would never fold, forever."""
        key = _key("1")
        await _add_repeater("Straggler", key)

        observed = NOW - DRIFT_FULL_RESOLUTION_SECONDS - 5 * DAY_SECONDS
        observed = (observed // DAY_SECONDS) * DAY_SECONDS + 14 * HOUR
        await ContactClockDriftRepository.record(
            key, advert_timestamp=observed - 42, observed_at=observed
        )

        # Nothing merged away -- one row in, one row out -- but it is now
        # day-aligned, so the next pass has nothing left to consider.
        assert await ContactClockDriftRepository.compact(now=NOW) == 0
        assert await ContactClockDriftRepository.compact(now=NOW) == 0

        async with test_db.readonly() as conn:
            async with conn.execute(
                "SELECT bucket_start FROM contact_clock_drift WHERE public_key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        assert row["bucket_start"] % DAY_SECONDS == 0

    @pytest.mark.asyncio
    async def test_deleting_a_contact_takes_its_drift_history(self, test_db):
        key = _key("d")
        await _add_repeater("Delta", key)
        await ContactClockDriftRepository.record(key, advert_timestamp=NOW - 5, observed_at=NOW)

        await test_db.conn.execute("PRAGMA foreign_keys = ON")
        await ContactRepository.delete(key)

        async with test_db.readonly() as conn:
            async with conn.execute(
                "SELECT COUNT(*) AS cnt FROM contact_clock_drift WHERE public_key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        assert row["cnt"] == 0


class TestPipelineIntegration:
    """The advert pipeline, driven end-to-end past the drift write."""

    @staticmethod
    def _packet_info(path_hex: str = ""):
        from unittest.mock import MagicMock

        info = MagicMock()
        info.path = bytes.fromhex(path_hex)
        info.path_length = len(path_hex) // 2
        info.path_hash_size = 1
        info.payload = b""
        return info

    @staticmethod
    def _advert(key: str, advert_timestamp: int):
        from app.decoder import ParsedAdvertisement

        return ParsedAdvertisement(
            public_key=key,
            name="Pipeline Node",
            timestamp=advert_timestamp,
            lat=None,
            lon=None,
            device_role=REPEATER,
        )

    @pytest.mark.asyncio
    async def test_an_advert_arrival_records_its_drift(self, test_db, captured_broadcasts):
        from app.packet_processor import _process_advertisement

        key = _key("7")
        _, mock_broadcast = captured_broadcasts

        with (
            patch("app.packet_processor.broadcast_event", mock_broadcast),
            patch("app.packet_processor.parse_advertisement") as mock_parse,
            patch("app.packet_processor.verify_advert_signature", return_value=True),
        ):
            mock_parse.return_value = self._advert(key, NOW - 450)
            await _process_advertisement(b"", timestamp=NOW, packet_info=self._packet_info("aabb"))

        drift = await ContactClockDriftRepository.get_for_contact(key, now=NOW)
        assert drift is not None
        assert drift.latest_drift_seconds == -450
        assert drift.latest_path_len == 2
        assert drift.severity == "major"  # -450s is past the 5-minute band

    @pytest.mark.asyncio
    async def test_contact_freshness_still_ignores_the_sender_clock(
        self, test_db, captured_broadcasts
    ):
        """Drift is recorded from the sender clock; last_seen must not be.

        This is the invariant the whole feature is built around -- route
        selection cannot be allowed to depend on a node with a bad RTC.
        """
        from app.packet_processor import _process_advertisement

        key = _key("8")
        _, mock_broadcast = captured_broadcasts

        with (
            patch("app.packet_processor.broadcast_event", mock_broadcast),
            patch("app.packet_processor.parse_advertisement") as mock_parse,
            patch("app.packet_processor.verify_advert_signature", return_value=True),
        ):
            # A clock a full day ahead of ours.
            mock_parse.return_value = self._advert(key, NOW + 86400)
            await _process_advertisement(b"", timestamp=NOW, packet_info=self._packet_info())

        contact = await ContactRepository.get_by_key(key)
        assert contact is not None
        assert contact.last_advert == NOW
        assert contact.last_seen == NOW

        drift = await ContactClockDriftRepository.get_for_contact(key, now=NOW)
        assert drift is not None
        assert drift.latest_drift_seconds == 86400

    @pytest.mark.asyncio
    async def test_a_failed_drift_write_does_not_abort_advert_ingestion(
        self, test_db, captured_broadcasts
    ):
        """Contact ingestion is a primary feature; a drift bucket is not."""
        from app.packet_processor import _process_advertisement

        key = _key("9")
        _, mock_broadcast = captured_broadcasts

        async def boom(*args, **kwargs):
            raise RuntimeError("drift table is on fire")

        with (
            patch("app.packet_processor.broadcast_event", mock_broadcast),
            patch("app.packet_processor.parse_advertisement") as mock_parse,
            patch("app.packet_processor.verify_advert_signature", return_value=True),
            patch.object(ContactClockDriftRepository, "record", boom),
        ):
            mock_parse.return_value = self._advert(key, NOW - 30)
            await _process_advertisement(b"", timestamp=NOW, packet_info=self._packet_info("aa"))

        # The contact and its advert path -- written after the drift call -- survive.
        contact = await ContactRepository.get_by_key(key)
        assert contact is not None and contact.last_advert == NOW
        paths = await ContactAdvertPathRepository.get_recent_for_contact(key)
        assert [p.path for p in paths] == ["aa"]


class TestContactSummary:
    @pytest.mark.asyncio
    async def test_returns_none_when_never_measured(self, test_db):
        key = _key("e")
        await _add_repeater("Echo", key)
        assert await ContactClockDriftRepository.get_for_contact(key, now=NOW) is None

    @pytest.mark.asyncio
    async def test_summarises_a_walking_clock(self, test_db):
        key = _key("f")
        await _add_repeater("Foxtrot", key)

        # 48 hourly readings, gaining 5 seconds an hour == 120 s/day.
        for i in range(48):
            observed = NOW - (47 - i) * HOUR
            await ContactClockDriftRepository.record(
                key,
                advert_timestamp=observed + i * 5,
                observed_at=observed,
                path_len=0 if i % 2 else 2,
            )

        drift = await ContactClockDriftRepository.get_for_contact(key, now=NOW)
        assert drift is not None
        assert drift.latest_drift_seconds == 47 * 5
        assert drift.min_drift_seconds == 0
        assert drift.max_drift_seconds == 47 * 5
        assert drift.sample_count == 48
        assert drift.bucket_count == 48
        assert drift.direct_sample_count == 24
        # 120 s/day is the true slope. The 30-day window rolls the hourly rows
        # into wider buckets that keep MAX(drift), which pulls each point toward
        # the end of its bucket while the timestamp stays at the start -- so the
        # fitted slope lands near, not exactly on, the truth.
        assert drift.drift_rate_seconds_per_day == pytest.approx(120.0, abs=8.0)
        assert drift.severity == "minor"
        assert drift.clock_unset is False
        assert drift.samples
        # A 30-day window buckets wider than an hour, so the series is
        # chart-sized rather than one point per stored row.
        assert drift.bucket_seconds >= DRIFT_BUCKET_SECONDS
        assert len(drift.samples) <= 48

    @pytest.mark.asyncio
    async def test_flags_a_clock_that_was_never_set(self, test_db):
        key = _key("1")
        await _add_repeater("Never Set", key)
        await ContactClockDriftRepository.record(key, advert_timestamp=42, observed_at=NOW)

        drift = await ContactClockDriftRepository.get_for_contact(key, now=NOW)
        assert drift is not None
        assert drift.clock_unset is True
        assert drift.severity == "severe"

    @pytest.mark.asyncio
    async def test_readings_outside_the_window_do_not_count(self, test_db):
        key = _key("2")
        await _add_repeater("Old News", key)
        await ContactClockDriftRepository.record(
            key, advert_timestamp=NOW - 40 * 86400 - 9999, observed_at=NOW - 40 * 86400
        )

        assert await ContactClockDriftRepository.get_for_contact(key, now=NOW) is None


class TestRepeaterAggregate:
    @pytest.mark.asyncio
    async def test_empty_mesh_reports_zeroes_not_nulls(self, test_db):
        stats = await StatisticsRepository._repeater_clock_drift(NOW - 86400, NOW)
        assert stats["repeaters_with_samples"] == 0
        assert stats["oldest_sample_at"] is None
        assert stats["worst_offenders"] == []
        # The histogram keeps its bins so the chart has an x-axis either way.
        assert len(stats["histogram"]) == len(histogram_labels())

    @pytest.mark.asyncio
    async def test_ranks_and_bands_repeaters_by_their_latest_reading(self, test_db):
        good, behind, ahead = _key("3"), _key("4"), _key("5")
        await _add_repeater("In Sync", good)
        await _add_repeater("Way Behind", behind)
        await _add_repeater("A Bit Ahead", ahead)

        for i in range(12):
            observed = NOW - (11 - i) * HOUR
            await ContactClockDriftRepository.record(
                good, advert_timestamp=observed - 2, observed_at=observed
            )
            await ContactClockDriftRepository.record(
                behind, advert_timestamp=observed - 7200, observed_at=observed
            )
            # Gains 30 seconds an hour: the only one with a real trend.
            await ContactClockDriftRepository.record(
                ahead, advert_timestamp=observed + 100 + i * 30, observed_at=observed
            )

        stats = await StatisticsRepository._repeater_clock_drift(NOW - 7 * 86400, NOW)

        assert stats["repeaters_total"] == 3
        assert stats["repeaters_with_samples"] == 3
        # good ends at -2s, ahead at +430s, behind at -7200s.
        assert stats["in_sync"] == 1
        assert stats["minor"] == 0
        assert stats["major"] == 1
        assert stats["severe"] == 1

        assert stats["furthest_behind"]["public_key"] == behind
        assert stats["furthest_behind"]["drift_seconds"] == -7200
        assert stats["furthest_ahead"]["public_key"] == ahead

        # Largest magnitude first.
        assert stats["worst_offenders"][0]["public_key"] == behind

        # Only the walking clock has a provable trend, and it is reported in
        # seconds per day.
        rates = {e["public_key"]: e["drift_rate_seconds_per_day"] for e in stats["fastest_rates"]}
        assert rates[ahead] == pytest.approx(720.0, abs=5.0)
        assert stats["over_time"]
        assert stats["over_time"][-1]["repeater_count"] == 3

    @pytest.mark.asyncio
    async def test_signed_median_is_what_indicts_our_own_clock(self, test_db):
        # Every repeater reads the same way: that is this server, not the mesh.
        keys = [_key(c) for c in "6789"]
        for index, key in enumerate(keys):
            await _add_repeater(f"Node {index}", key)
            for i in range(4):
                observed = NOW - i * HOUR
                await ContactClockDriftRepository.record(
                    key, advert_timestamp=observed + 900, observed_at=observed
                )

        stats = await StatisticsRepository._repeater_clock_drift(NOW - 86400, NOW)
        assert stats["median_drift_seconds"] == pytest.approx(900, abs=1)
        assert stats["median_abs_drift_seconds"] == pytest.approx(900, abs=1)

    @pytest.mark.asyncio
    async def test_non_repeaters_stay_out_of_the_repeater_aggregate(self, test_db):
        client_key = _key("b")
        await ContactRepository.upsert(
            ContactUpsert(
                public_key=client_key,
                name="Just A Phone",
                type=1,
                last_seen=NOW,
                last_advert=NOW,
                first_seen=NOW,
            )
        )
        await ContactClockDriftRepository.record(
            client_key, advert_timestamp=NOW - 99999, observed_at=NOW
        )

        stats = await StatisticsRepository._repeater_clock_drift(NOW - 86400, NOW)
        assert stats["repeaters_with_samples"] == 0

    @pytest.mark.asyncio
    async def test_an_unset_clock_is_listed_apart_from_the_drift_rankings(self, test_db):
        """Decades of apparent drift must not monopolise the mean or the rankings."""
        unset, real = _key("c"), _key("d")
        await _add_repeater("Unset", unset)
        await _add_repeater("Genuinely Off", real)

        for i in range(6):
            observed = NOW - i * HOUR
            # Reports time since boot rather than a date.
            await ContactClockDriftRepository.record(
                unset, advert_timestamp=1200 + i * HOUR, observed_at=observed
            )
            await ContactClockDriftRepository.record(
                real, advert_timestamp=observed - 7200, observed_at=observed
            )

        stats = await StatisticsRepository._repeater_clock_drift(NOW - 86400, NOW)

        assert stats["repeaters_unset_clock"] == 1
        # Still severe, still in the histogram -- it is a real problem.
        assert stats["severe"] == 2
        # But the mean and the rankings describe the clock that can be resynced.
        assert stats["mean_abs_drift_seconds"] == pytest.approx(7200, abs=1)
        assert [e["public_key"] for e in stats["worst_offenders"]] == [real]
        assert stats["furthest_behind"]["public_key"] == real
        assert [e["public_key"] for e in stats["unset_clocks"]] == [unset]
        # ...and out of the mesh-wide series, which shares one axis: decades of
        # apparent drift would flatten every real repeater into the baseline.
        assert stats["over_time"]
        assert all(bucket["repeater_count"] == 1 for bucket in stats["over_time"])
        assert max(b["max_abs_drift_seconds"] for b in stats["over_time"]) == 7200

    @pytest.mark.asyncio
    async def test_a_mesh_of_only_unset_clocks_still_reports_cleanly(self, test_db):
        key = _key("e")
        await _add_repeater("Unset Only", key)
        await ContactClockDriftRepository.record(key, advert_timestamp=7, observed_at=NOW)

        stats = await StatisticsRepository._repeater_clock_drift(NOW - 86400, NOW)
        assert stats["repeaters_unset_clock"] == 1
        assert stats["mean_abs_drift_seconds"] == 0.0
        assert stats["furthest_behind"] is None
        assert stats["furthest_ahead"] is None
        assert stats["worst_offenders"] == []
