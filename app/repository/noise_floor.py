import logging
import time

from app.database import db
from app.stats_windows import bucket_seconds_for_span

logger = logging.getLogger(__name__)

# The sampling loop writes one row per minute. A year of that is ~525k rows at
# 16 bytes each — small enough to keep, large enough that it must be bucketed
# in SQL rather than shipped raw to the browser.
RETENTION_SECONDS = 365 * 86400

# Finest useful bucket: samples arrive once a minute, so anything narrower just
# produces empty slots.
MIN_BUCKET_SECONDS = 60


class NoiseFloorRepository:
    """Persistent noise-floor series behind the statistics chart.

    Rows are keyed by their sample timestamp (INTEGER PRIMARY KEY), so a second
    sample landing in the same second replaces the first instead of duplicating
    it.
    """

    @staticmethod
    async def record(timestamp: int, noise_floor_dbm: int) -> None:
        """Store one sample."""
        async with db.tx() as conn:
            await conn.execute(
                """
                INSERT INTO noise_floor_samples (timestamp, noise_floor_dbm)
                VALUES (?, ?)
                ON CONFLICT(timestamp) DO UPDATE SET noise_floor_dbm = excluded.noise_floor_dbm
                """,
                (timestamp, noise_floor_dbm),
            )

    @staticmethod
    async def prune(retention_seconds: int = RETENTION_SECONDS) -> int:
        """Drop samples older than the retention horizon. Returns rows deleted."""
        cutoff = int(time.time()) - retention_seconds
        async with db.tx() as conn:
            cursor = await conn.execute(
                "DELETE FROM noise_floor_samples WHERE timestamp < ?", (cutoff,)
            )
            return cursor.rowcount or 0

    @staticmethod
    async def history(cutoff: int | None, *, now: int | None = None) -> dict:
        """Return the series since ``cutoff`` (``None`` = everything), bucketed.

        Buckets are averaged, and each carries its min/max so the chart can draw
        the spread a long window hides. When the bucket width equals the sample
        interval, min == max == avg and the series is effectively raw.
        """
        now = int(time.time()) if now is None else now

        async with db.readonly() as conn:
            if cutoff is None:
                async with conn.execute(
                    "SELECT MIN(timestamp) AS oldest, MAX(timestamp) AS newest "
                    "FROM noise_floor_samples"
                ) as cursor:
                    bounds = await cursor.fetchone()
            else:
                async with conn.execute(
                    "SELECT MIN(timestamp) AS oldest, MAX(timestamp) AS newest "
                    "FROM noise_floor_samples WHERE timestamp >= ?",
                    (cutoff,),
                ) as cursor:
                    bounds = await cursor.fetchone()

            oldest = bounds["oldest"] if bounds else None
            newest = bounds["newest"] if bounds else None
            if oldest is None or newest is None:
                return {
                    "sample_interval_seconds": MIN_BUCKET_SECONDS,
                    "bucket_seconds": MIN_BUCKET_SECONDS,
                    "coverage_seconds": 0,
                    "latest_noise_floor_dbm": None,
                    "latest_timestamp": None,
                    "samples": [],
                }

            # Size buckets from the nominal window, not from how much data happens
            # to be in it, so the x-axis granularity depends only on what the user
            # picked. ``all`` has no nominal span, so it uses the real one.
            span = (now - oldest) if cutoff is None else (now - cutoff)
            bucket = bucket_seconds_for_span(span, minimum=MIN_BUCKET_SECONDS)

            params: tuple = (bucket, bucket)
            where = ""
            if cutoff is not None:
                where = "WHERE timestamp >= ?"
                params = (bucket, bucket, cutoff)

            async with conn.execute(
                f"""
                SELECT (timestamp / ?) * ? AS bucket_ts,
                       CAST(ROUND(AVG(noise_floor_dbm)) AS INTEGER) AS avg_dbm,
                       MIN(noise_floor_dbm) AS min_dbm,
                       MAX(noise_floor_dbm) AS max_dbm
                FROM noise_floor_samples
                {where}
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()

            async with conn.execute(
                "SELECT timestamp, noise_floor_dbm FROM noise_floor_samples "
                "ORDER BY timestamp DESC LIMIT 1"
            ) as cursor:
                latest_row = await cursor.fetchone()

        samples = [
            {
                "timestamp": row["bucket_ts"],
                "noise_floor_dbm": row["avg_dbm"],
                "min_dbm": row["min_dbm"],
                "max_dbm": row["max_dbm"],
            }
            for row in rows
        ]

        return {
            "sample_interval_seconds": MIN_BUCKET_SECONDS,
            "bucket_seconds": bucket,
            "coverage_seconds": max(0, now - oldest),
            "latest_noise_floor_dbm": latest_row["noise_floor_dbm"] if latest_row else None,
            "latest_timestamp": latest_row["timestamp"] if latest_row else None,
            "samples": samples,
        }
