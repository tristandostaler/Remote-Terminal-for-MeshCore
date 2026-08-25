"""Tests for the persisted noise-floor series."""

import time

import pytest

from app.repository import NoiseFloorRepository


async def _seed(conn, samples):
    for timestamp, dbm in samples:
        await conn.execute(
            "INSERT INTO noise_floor_samples (timestamp, noise_floor_dbm) VALUES (?, ?)",
            (timestamp, dbm),
        )
    await conn.commit()


class TestRecord:
    @pytest.mark.asyncio
    async def test_stores_a_sample(self, test_db):
        await NoiseFloorRepository.record(1_700_000_000, -118)

        history = await NoiseFloorRepository.history(None, now=1_700_000_060)
        assert history["latest_noise_floor_dbm"] == -118
        assert history["latest_timestamp"] == 1_700_000_000

    @pytest.mark.asyncio
    async def test_second_sample_in_the_same_second_replaces_the_first(self, test_db):
        """The timestamp is the primary key, so a re-sample updates in place."""
        await NoiseFloorRepository.record(1_700_000_000, -118)
        await NoiseFloorRepository.record(1_700_000_000, -101)

        history = await NoiseFloorRepository.history(None, now=1_700_000_060)
        assert len(history["samples"]) == 1
        assert history["latest_noise_floor_dbm"] == -101


class TestHistory:
    @pytest.mark.asyncio
    async def test_empty_series(self, test_db):
        history = await NoiseFloorRepository.history(None)

        assert history["samples"] == []
        assert history["coverage_seconds"] == 0
        assert history["latest_noise_floor_dbm"] is None
        assert history["latest_timestamp"] is None

    @pytest.mark.asyncio
    async def test_short_window_returns_samples_unaggregated(self, test_db):
        now = int(time.time())
        await _seed(test_db.conn, [(now - 180, -120), (now - 120, -118), (now - 60, -116)])

        history = await NoiseFloorRepository.history(now - 3600, now=now)

        assert history["bucket_seconds"] == 60
        assert [s["noise_floor_dbm"] for s in history["samples"]] == [-120, -118, -116]
        assert all(s["min_dbm"] == s["max_dbm"] for s in history["samples"])

    @pytest.mark.asyncio
    async def test_wide_window_averages_into_buckets_with_spread(self, test_db):
        """A month of minute samples arrives as a chart-sized series, not 43k rows."""
        now = int(time.time())
        bucket = 21600  # what a 30-day span resolves to
        start = ((now - 20 * 86400) // bucket) * bucket
        await _seed(
            test_db.conn,
            [(start + 60, -120), (start + 120, -110), (start + bucket + 60, -100)],
        )

        history = await NoiseFloorRepository.history(now - 30 * 86400, now=now)

        assert history["bucket_seconds"] == bucket
        by_ts = {s["timestamp"]: s for s in history["samples"]}
        assert by_ts[start]["noise_floor_dbm"] == -115  # mean of -120 and -110
        assert by_ts[start]["min_dbm"] == -120
        assert by_ts[start]["max_dbm"] == -110
        assert by_ts[start + bucket]["noise_floor_dbm"] == -100

    @pytest.mark.asyncio
    async def test_cutoff_excludes_older_samples(self, test_db):
        now = int(time.time())
        await _seed(test_db.conn, [(now - 10 * 86400, -130), (now - 120, -118)])

        history = await NoiseFloorRepository.history(now - 3600, now=now)

        assert [s["noise_floor_dbm"] for s in history["samples"]] == [-118]
        # The "latest reading" line is about the radio, not the window
        assert history["latest_noise_floor_dbm"] == -118

    @pytest.mark.asyncio
    async def test_all_window_reaches_everything(self, test_db):
        now = int(time.time())
        await _seed(test_db.conn, [(now - 300 * 86400, -130), (now - 120, -118)])

        history = await NoiseFloorRepository.history(None, now=now)

        assert len(history["samples"]) == 2
        assert history["coverage_seconds"] >= 300 * 86400

    @pytest.mark.asyncio
    async def test_coverage_reports_how_far_back_data_reaches(self, test_db):
        now = int(time.time())
        await _seed(test_db.conn, [(now - 7200, -120), (now - 60, -118)])

        history = await NoiseFloorRepository.history(now - 86400, now=now)

        assert 7200 <= history["coverage_seconds"] <= 7260


class TestPrune:
    @pytest.mark.asyncio
    async def test_drops_samples_past_the_retention_horizon(self, test_db):
        now = int(time.time())
        await _seed(test_db.conn, [(now - 400 * 86400, -130), (now - 60, -118)])

        deleted = await NoiseFloorRepository.prune()

        assert deleted == 1
        history = await NoiseFloorRepository.history(None, now=now)
        assert [s["noise_floor_dbm"] for s in history["samples"]] == [-118]

    @pytest.mark.asyncio
    async def test_keeps_everything_inside_the_horizon(self, test_db):
        now = int(time.time())
        await _seed(test_db.conn, [(now - 100 * 86400, -130), (now - 60, -118)])

        assert await NoiseFloorRepository.prune() == 0
