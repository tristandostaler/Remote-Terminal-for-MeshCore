import logging

import aiosqlite

logger = logging.getLogger(__name__)


async def migrate(conn: aiosqlite.Connection) -> None:
    """Persist the noise-floor series so it survives restarts and 24 hours.

    Samples used to live in an in-memory deque capped at 1500 entries, which
    made the chart both restart-amnesiac and hard-capped at a day. The
    statistics view now offers windows up to a year, so the series has to be
    on disk.

    ``timestamp`` is the INTEGER PRIMARY KEY (a rowid alias), so range scans
    over a window are index-ordered for free and a re-sampled second cannot
    produce a duplicate row.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS noise_floor_samples (
            timestamp INTEGER PRIMARY KEY,
            noise_floor_dbm INTEGER NOT NULL
        )
        """
    )

    await conn.commit()
