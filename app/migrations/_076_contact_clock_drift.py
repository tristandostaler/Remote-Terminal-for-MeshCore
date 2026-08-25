import logging
import time

import aiosqlite

logger = logging.getLogger(__name__)

# Header bytes whose payload type is ADVERT (0x04). The header packs
# ``route_type`` in bits 0-1, ``payload_type`` in bits 2-5, and
# ``payload_version`` in bits 6-7, so an advert is any byte matching
# ``header & 0x3C == 0x10`` — sixteen values once route and version vary.
# Enumerating them lets SQLite reject non-adverts without loading the blob.
_ADVERT_HEADER_BYTES = tuple(
    f"{(version << 6) | 0x10 | route:02X}" for version in range(4) for route in range(4)
)

# One-time backfills should not turn a first start after upgrade into a
# multi-minute stall on a database that has been hoarding packets for a year.
# Newest packets are processed first, so hitting the cap loses the oldest
# history rather than the part anyone is looking at.
_MAX_PACKETS_SCANNED = 500_000
_BATCH_SIZE = 2_000


async def migrate(conn: aiosqlite.Connection) -> None:
    """Store per-node clock drift measured from advert timestamps.

    Every advert carries the sender's own clock inside its signed payload. The
    packet pipeline parsed that field and threw it away on purpose — contact
    freshness has to use our receive clock so route selection cannot be skewed
    by a node with a bad RTC (see ``_process_advertisement``). Keeping it in a
    dedicated table gives the drift surfaces their own history without letting
    sender time anywhere near ``last_seen``.

    Samples are folded into hourly buckets keyed ``(public_key, bucket_start)``,
    and each bucket keeps its *largest* drift. Propagation delay only ever
    biases a reading negative, so the maximum within an hour is the arrival that
    suffered least of it.

    Stored raw adverts are then replayed so the feature starts with whatever
    history the database already holds instead of an empty chart.
    """
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contact_clock_drift (
            public_key TEXT NOT NULL,
            bucket_start INTEGER NOT NULL,
            drift_seconds INTEGER NOT NULL,
            observed_at INTEGER NOT NULL,
            advert_timestamp INTEGER NOT NULL,
            path_len INTEGER NOT NULL DEFAULT 0,
            sample_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (public_key, bucket_start),
            FOREIGN KEY (public_key) REFERENCES contacts(public_key) ON DELETE CASCADE
        ) WITHOUT ROWID
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_contact_clock_drift_bucket "
        "ON contact_clock_drift(bucket_start)"
    )
    await conn.commit()

    await _backfill_from_raw_packets(conn)


async def _backfill_from_raw_packets(conn: aiosqlite.Connection) -> None:
    """Replay stored adverts into the drift table. Best-effort by design."""
    from app.clock_drift import DAY_SECONDS, DRIFT_FULL_RESOLUTION_SECONDS, bucket_start
    from app.decoder import parse_packet, verify_advert_signature

    for table in ("raw_packets", "contacts"):
        cursor = await conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        )
        if not await cursor.fetchone():
            return

    # Only contacts we still know about. A bit-flipped public key in a corrupt
    # capture will not match one, and the drift table has a foreign key onto
    # ``contacts`` that migrations run with enforcement switched off — so this
    # is both a noise filter and the thing keeping the table referentially sane.
    cursor = await conn.execute("SELECT public_key FROM contacts")
    known_keys = {row[0].lower() for row in await cursor.fetchall() if row[0]}
    if not known_keys:
        logger.debug("No contacts stored; skipping clock drift backfill")
        return

    # No lower bound on time: drift history is kept forever, so the backfill
    # reaches as far back as the packet table does. The row cap is what keeps
    # this bounded, and newest-first means hitting it costs the oldest history
    # rather than the part anyone is about to look at.
    placeholders = ",".join("?" * len(_ADVERT_HEADER_BYTES))
    cursor = await conn.execute(
        f"""
        SELECT timestamp, data FROM raw_packets
        WHERE hex(substr(data, 1, 1)) IN ({placeholders})
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (*_ADVERT_HEADER_BYTES, _MAX_PACKETS_SCANNED),
    )

    # Fold in memory before writing: a flooded advert reaches us once per path,
    # and an hour of them collapses to a single row. Doing that here turns
    # hundreds of thousands of upserts into one insert per bucket.
    buckets: dict[tuple[str, int], list[int]] = {}
    scanned = 0
    unverified = 0

    while True:
        rows = await cursor.fetchmany(_BATCH_SIZE)
        if not rows:
            break
        for row in rows:
            scanned += 1
            observed_at = row[0]
            packet_info = parse_packet(bytes(row[1]))
            if packet_info is None:
                continue
            # Only the two fixed-offset fields matter here, so the rest of the
            # advert (name, location, feature flags) is left unparsed — a full
            # parse would burn time and log warnings about historical packets
            # nobody is going to act on.
            payload = packet_info.payload
            if len(payload) < 36:
                continue
            key = payload[0:32].hex()
            if key not in known_keys:
                continue
            # Signature check comes last: it is the only expensive step here,
            # and the key filter above has already discarded the corrupt
            # captures that make up most of what would fail it.
            if not verify_advert_signature(payload):
                unverified += 1
                continue
            advert_timestamp = int.from_bytes(payload[32:36], "little")

            drift = advert_timestamp - observed_at
            slot = (key, bucket_start(observed_at))
            existing = buckets.get(slot)
            if existing is None:
                buckets[slot] = [drift, observed_at, advert_timestamp, packet_info.path_length, 1]
            else:
                existing[4] += 1
                if drift > existing[0]:
                    existing[0] = drift
                    existing[1] = observed_at
                    existing[2] = advert_timestamp
                    existing[3] = packet_info.path_length

        if scanned % 50_000 == 0:
            logger.info("Clock drift backfill: scanned %d advert packets...", scanned)

    if not buckets:
        logger.debug("Clock drift backfill found no usable adverts in %d packets", scanned)
        return

    await conn.executemany(
        """
        INSERT INTO contact_clock_drift
            (public_key, bucket_start, drift_seconds, observed_at,
             advert_timestamp, path_len, sample_count)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(public_key, bucket_start) DO UPDATE SET
            sample_count = contact_clock_drift.sample_count + excluded.sample_count,
            drift_seconds = MAX(contact_clock_drift.drift_seconds, excluded.drift_seconds),
            observed_at = CASE
                WHEN excluded.drift_seconds > contact_clock_drift.drift_seconds
                THEN excluded.observed_at ELSE contact_clock_drift.observed_at END,
            advert_timestamp = CASE
                WHEN excluded.drift_seconds > contact_clock_drift.drift_seconds
                THEN excluded.advert_timestamp ELSE contact_clock_drift.advert_timestamp END,
            path_len = CASE
                WHEN excluded.drift_seconds > contact_clock_drift.drift_seconds
                THEN excluded.path_len ELSE contact_clock_drift.path_len END
        """,
        [(key, slot, *values) for (key, slot), values in buckets.items()],
    )
    await conn.commit()

    logger.info(
        "Clock drift backfill: %d hourly buckets from %d advert packets "
        "(%d dropped on signature check)",
        len(buckets),
        scanned,
        unverified,
    )

    # Fold the recovered history straight away, rather than leaving the database
    # carrying years of hourly rows until the first maintenance tick six hours
    # from now. Same merge the periodic job does, inlined so the migration does
    # not have to import the repository (whose module-level ``db`` is not this
    # connection).
    horizon = ((int(time.time()) - DRIFT_FULL_RESOLUTION_SECONDS) // DAY_SECONDS) * DAY_SECONDS
    async with conn.execute(
        """
        SELECT public_key,
               (bucket_start / ?) * ? AS day,
               MAX(drift_seconds) AS drift_seconds,
               observed_at,
               advert_timestamp,
               path_len,
               SUM(sample_count) AS sample_count,
               COUNT(*) AS rows_in_day
        FROM contact_clock_drift
        WHERE bucket_start < ?
        GROUP BY public_key, day
        HAVING rows_in_day > 1 OR MIN(bucket_start) != day
        """,
        (DAY_SECONDS, DAY_SECONDS, horizon),
    ) as compact_cursor:
        days = list(await compact_cursor.fetchall())

    if days:
        await conn.executemany(
            "DELETE FROM contact_clock_drift WHERE public_key = ? "
            "AND bucket_start >= ? AND bucket_start < ?",
            [(row[0], row[1], row[1] + DAY_SECONDS) for row in days],
        )
        await conn.executemany(
            """
            INSERT INTO contact_clock_drift
                (public_key, bucket_start, drift_seconds, observed_at,
                 advert_timestamp, path_len, sample_count)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [tuple(row[0:7]) for row in days],
        )
        await conn.commit()
        logger.info("Clock drift backfill: folded old history into %d daily rows", len(days))
