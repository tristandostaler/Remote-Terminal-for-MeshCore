"""Local mirror of a CoreScope (live.meshcore.ca) channel-message feed.

One row per remote packet hash. Rows are joined to ``messages`` on the exact
key the channel-echo dedup index uses -- ``(conversation_key, text,
COALESCE(sender_timestamp, 0))`` -- so a "both" verdict means the two sides
decrypted byte-identical plaintext with the same sender clock, never a fuzzy
text match. MeshCore channel encryption is deterministic (no IV), so that is
also the same packet on the air.

Every read builds the same three-way merge:

* ``live``  -- every mirrored row, LEFT JOINed to its local twin (``both`` or ``live``)
* ``node``  -- local channel messages on compared channels with no mirrored twin (``node``)

``seen_at`` is the earliest time *either* side saw the message; windows and
ordering use it so a message the mesh heard at 10:00 and this node only
decrypted at 10:05 lands in one place.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from typing import Any

from app.database import db
from app.stats_windows import bucket_seconds_for_span

logger = logging.getLogger(__name__)

# One upsert transaction per batch of this many rows; a first sync of a busy
# channel can mirror thousands of messages and should not hold the lock once.
UPSERT_BATCH = 500

_LIVE_COLUMNS = (
    "packet_hash",
    "channel_name",
    "channel_key",
    "sender",
    "text",
    "sender_timestamp",
    "first_seen",
    "last_seen",
    "repeats",
    "observers",
    "hops",
    "snr",
    "scope_name",
    "fetched_at",
)


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class LiveFeedRepository:
    """CRUD for ``live_feed_messages`` plus the node-vs-live merge queries."""

    # ------------------------------------------------------------------ writes

    @staticmethod
    async def upsert_many(rows: list[dict[str, Any]]) -> tuple[int, int]:
        """Insert new rows and refresh the ones whose observations moved.

        Returns ``(inserted, updated)``. A row already holding the same
        last-seen time and repeat count is left alone, so a poll that re-reads
        an unchanged window costs one indexed SELECT per batch and no writes.
        """
        if not rows:
            return 0, 0
        now = int(time.time())
        inserted = 0
        updated = 0
        for start in range(0, len(rows), UPSERT_BATCH):
            batch = rows[start : start + UPSERT_BATCH]
            hashes = [row["packet_hash"] for row in batch]
            async with db.tx() as conn:
                async with conn.execute(
                    f"SELECT packet_hash, last_seen, repeats, channel_key FROM live_feed_messages "
                    f"WHERE packet_hash IN ({_placeholders(len(hashes))})",
                    hashes,
                ) as cursor:
                    existing = {r["packet_hash"]: r for r in await cursor.fetchall()}
                to_write: list[dict[str, Any]] = []
                for row in batch:
                    current = existing.get(row["packet_hash"])
                    if current is None:
                        inserted += 1
                        to_write.append(row)
                        continue
                    last_seen = int(row.get("last_seen") or row["first_seen"])
                    repeats = int(row.get("repeats") or 1)
                    if (
                        last_seen > (current["last_seen"] or 0)
                        or repeats > (current["repeats"] or 0)
                        or (current["channel_key"] is None and row.get("channel_key"))
                    ):
                        updated += 1
                        to_write.append(row)
                if not to_write:
                    continue
                await conn.executemany(
                    f"""
                    INSERT INTO live_feed_messages ({", ".join(_LIVE_COLUMNS)})
                    VALUES ({_placeholders(len(_LIVE_COLUMNS))})
                    ON CONFLICT(packet_hash) DO UPDATE SET
                        first_seen = MIN(first_seen, excluded.first_seen),
                        last_seen = MAX(last_seen, excluded.last_seen),
                        repeats = MAX(repeats, excluded.repeats),
                        observers = excluded.observers,
                        hops = COALESCE(excluded.hops, hops),
                        snr = COALESCE(excluded.snr, snr),
                        scope_name = COALESCE(excluded.scope_name, scope_name),
                        channel_key = COALESCE(excluded.channel_key, channel_key),
                        fetched_at = excluded.fetched_at
                    """,
                    [
                        (
                            row["packet_hash"],
                            row["channel_name"],
                            row.get("channel_key"),
                            row.get("sender"),
                            row["text"],
                            row.get("sender_timestamp"),
                            int(row["first_seen"]),
                            int(row.get("last_seen") or row["first_seen"]),
                            int(row.get("repeats") or 1),
                            json.dumps(row.get("observers") or []),
                            row.get("hops"),
                            row.get("snr"),
                            row.get("scope_name"),
                            now,
                        )
                        for row in to_write
                    ],
                )
        return inserted, updated

    @staticmethod
    async def assign_channel_keys(mapping: dict[str, str | None]) -> None:
        """Resolve ``channel_key`` for rows mirrored before the channel was known locally.

        A channel joined *after* its messages were mirrored suddenly makes those
        rows comparable; this is what flips them from live-only to matchable.
        """
        updates = [(key, name) for name, key in mapping.items() if key]
        if not updates:
            return
        async with db.tx() as conn:
            await conn.executemany(
                "UPDATE live_feed_messages SET channel_key = ? "
                "WHERE channel_name = ? AND channel_key IS NULL",
                updates,
            )

    @staticmethod
    async def prune_older_than(cutoff: int) -> int:
        async with db.tx() as conn:
            async with conn.execute(
                "DELETE FROM live_feed_messages WHERE first_seen < ?", (cutoff,)
            ) as cursor:
                return cursor.rowcount or 0

    @staticmethod
    async def clear() -> None:
        async with db.tx() as conn:
            await conn.execute("DELETE FROM live_feed_messages")

    # ------------------------------------------------------------------- reads

    @staticmethod
    async def count() -> int:
        async with db.readonly() as conn:
            async with conn.execute("SELECT COUNT(*) AS n FROM live_feed_messages") as cursor:
                row = await cursor.fetchone()
                return int(row["n"]) if row else 0

    @staticmethod
    async def oldest_first_seen() -> int | None:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT MIN(first_seen) AS oldest FROM live_feed_messages"
            ) as cursor:
                row = await cursor.fetchone()
                return row["oldest"] if row and row["oldest"] is not None else None

    @staticmethod
    def _merged_cte(channel_keys: list[str], cutoff: int | None) -> tuple[str, list[Any]]:
        """The three-way merge as a ``WITH merged AS (...)`` prefix plus its params."""
        params: list[Any] = []
        live_where = ""
        node_time = ""
        if cutoff is not None:
            live_where = "WHERE lf.first_seen >= ?"
            params.append(cutoff)
        if channel_keys:
            node_channels = f"m.conversation_key IN ({_placeholders(len(channel_keys))})"
        else:
            node_channels = "0"
        node_params: list[Any] = list(channel_keys)
        if cutoff is not None:
            node_time = " AND m.received_at >= ?"
            node_params.append(cutoff)
        params.extend(node_params)

        sql = f"""
        WITH live AS (
            SELECT lf.packet_hash AS packet_hash,
                   lf.channel_key AS channel_key,
                   lf.channel_name AS live_channel_name,
                   lf.sender AS live_sender,
                   lf.text AS text,
                   lf.sender_timestamp AS sender_timestamp,
                   lf.first_seen AS live_first_seen,
                   lf.last_seen AS live_last_seen,
                   lf.repeats AS live_repeats,
                   lf.observers AS live_observers,
                   lf.hops AS live_hops,
                   lf.snr AS live_snr,
                   m.id AS message_id,
                   m.received_at AS node_received_at,
                   m.paths AS node_paths,
                   m.sender_name AS node_sender,
                   COALESCE(m.outgoing, 0) AS outgoing,
                   CASE
                       WHEN m.received_at IS NULL THEN lf.first_seen
                       WHEN m.received_at < lf.first_seen THEN m.received_at
                       ELSE lf.first_seen
                   END AS seen_at,
                   CASE WHEN m.id IS NULL THEN 'live' ELSE 'both' END AS source
            FROM live_feed_messages lf
            LEFT JOIN messages m
                   ON m.type = 'CHAN'
                  AND m.conversation_key = lf.channel_key
                  AND m.text = lf.text
                  AND COALESCE(m.sender_timestamp, 0) = COALESCE(lf.sender_timestamp, 0)
            {live_where}
        ),
        node AS (
            SELECT NULL AS packet_hash,
                   m.conversation_key AS channel_key,
                   NULL AS live_channel_name,
                   NULL AS live_sender,
                   m.text AS text,
                   m.sender_timestamp AS sender_timestamp,
                   NULL AS live_first_seen,
                   NULL AS live_last_seen,
                   NULL AS live_repeats,
                   NULL AS live_observers,
                   NULL AS live_hops,
                   NULL AS live_snr,
                   m.id AS message_id,
                   m.received_at AS node_received_at,
                   m.paths AS node_paths,
                   m.sender_name AS node_sender,
                   COALESCE(m.outgoing, 0) AS outgoing,
                   m.received_at AS seen_at,
                   'node' AS source
            FROM messages m
            WHERE m.type = 'CHAN'
              AND {node_channels}{node_time}
              AND NOT EXISTS (
                  SELECT 1 FROM live_feed_messages lf
                  WHERE lf.channel_key = m.conversation_key
                    AND lf.text = m.text
                    AND COALESCE(lf.sender_timestamp, 0) = COALESCE(m.sender_timestamp, 0)
              )
        ),
        merged AS (
            SELECT * FROM live
            UNION ALL
            SELECT * FROM node
        )
        """
        return sql, params

    @staticmethod
    async def _channel_names(conn) -> dict[str, str]:
        async with conn.execute("SELECT key, name FROM channels") as cursor:
            return {row["key"]: row["name"] for row in await cursor.fetchall()}

    @staticmethod
    async def get_compare_stats(
        channel_keys: list[str], cutoff: int | None, now: int
    ) -> dict[str, Any]:
        """Totals, per-channel counts and a time series for one window."""
        cte, params = LiveFeedRepository._merged_cte(channel_keys, cutoff)
        async with db.readonly() as conn:
            if cutoff is None:
                oldest = await LiveFeedRepository._oldest_in_conn(conn)
                span = 0 if oldest is None else max(0, now - oldest)
            else:
                span = max(0, now - cutoff)
            bucket = bucket_seconds_for_span(span, minimum=300)
            where = "WHERE seen_at >= ?" if cutoff is not None else ""
            query_params = [*params, bucket, bucket]
            if cutoff is not None:
                query_params.append(cutoff)
            async with conn.execute(
                f"""
                {cte}
                SELECT source, channel_key, live_channel_name,
                       (seen_at / ?) * ? AS bucket_ts, COUNT(*) AS n
                FROM merged
                {where}
                GROUP BY source, channel_key, live_channel_name, bucket_ts
                """,
                query_params,
            ) as cursor:
                rows = await cursor.fetchall()
            local_names = await LiveFeedRepository._channel_names(conn)

        totals = {"both": 0, "node": 0, "live": 0}
        per_channel: dict[str | None, dict[str, Any]] = {}
        buckets: dict[int, dict[str, int]] = defaultdict(lambda: {"both": 0, "node": 0, "live": 0})
        for row in rows:
            source = row["source"]
            n = int(row["n"])
            totals[source] += n
            key = row["channel_key"]
            entry = per_channel.setdefault(
                key,
                {
                    "channel_key": key,
                    "channel_name": local_names.get(key) or row["live_channel_name"] or key or "?",
                    "both": 0,
                    "node_only": 0,
                    "live_only": 0,
                },
            )
            if not local_names.get(key) and row["live_channel_name"]:
                entry["channel_name"] = row["live_channel_name"]
            entry[_source_field(source)] += n
            buckets[int(row["bucket_ts"])][source] += n

        channels = sorted(
            per_channel.values(),
            key=lambda c: -(c["both"] + c["node_only"] + c["live_only"]),
        )
        live_total = totals["both"] + totals["live"]
        node_total = totals["both"] + totals["node"]
        return {
            "both": totals["both"],
            "node_only": totals["node"],
            "live_only": totals["live"],
            "node_coverage_pct": (
                round(100.0 * totals["both"] / live_total, 1) if live_total else None
            ),
            "live_coverage_pct": (
                round(100.0 * totals["both"] / node_total, 1) if node_total else None
            ),
            "channels": channels,
            "bucket_seconds": bucket,
            "over_time": [
                {
                    "timestamp": ts,
                    "both": counts["both"],
                    "node_only": counts["node"],
                    "live_only": counts["live"],
                }
                for ts, counts in sorted(buckets.items())
            ],
        }

    @staticmethod
    async def _oldest_in_conn(conn) -> int | None:
        async with conn.execute(
            "SELECT MIN(first_seen) AS oldest FROM live_feed_messages"
        ) as cursor:
            row = await cursor.fetchone()
            return row["oldest"] if row and row["oldest"] is not None else None

    @staticmethod
    async def list_merged(
        channel_keys: list[str],
        cutoff: int | None,
        *,
        channel_key: str | None = None,
        source: str | None = None,
        q: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """One page of the merged view plus (total, per-source counts) for the filters.

        ``source`` narrows the page and the total; the per-source counts ignore it
        so the UI can show every filter chip with its number.
        """
        cte, params = LiveFeedRepository._merged_cte(channel_keys, cutoff)
        clauses: list[str] = []
        filter_params: list[Any] = []
        if cutoff is not None:
            clauses.append("seen_at >= ?")
            filter_params.append(cutoff)
        if channel_key:
            clauses.append("channel_key = ?")
            filter_params.append(channel_key.upper())
        if q:
            clauses.append("text LIKE ? ESCAPE '\\'")
            filter_params.append(f"%{_escape_like(q)}%")
        base_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        page_clauses = list(clauses)
        page_params = list(filter_params)
        if source in ("both", "node", "live"):
            page_clauses.append("source = ?")
            page_params.append(source)
        page_where = f"WHERE {' AND '.join(page_clauses)}" if page_clauses else ""

        async with db.readonly() as conn:
            async with conn.execute(
                f"{cte} SELECT source, COUNT(*) AS n FROM merged {base_where} GROUP BY source",
                [*params, *filter_params],
            ) as cursor:
                counts = {"both": 0, "node": 0, "live": 0}
                for row in await cursor.fetchall():
                    counts[row["source"]] = int(row["n"])
            async with conn.execute(
                f"""
                {cte}
                SELECT * FROM merged
                {page_where}
                ORDER BY seen_at DESC, COALESCE(sender_timestamp, 0) DESC, message_id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, *page_params, limit, offset],
            ) as cursor:
                rows = await cursor.fetchall()
            local_names = await LiveFeedRepository._channel_names(conn)

        total = counts[source] if source in counts else sum(counts.values())
        messages = [LiveFeedRepository._row_to_message(row, local_names) for row in rows]
        return (
            messages,
            total,
            {"both": counts["both"], "node_only": counts["node"], "live_only": counts["live"]},
        )

    @staticmethod
    def _row_to_message(row, local_names: dict[str, str]) -> dict[str, Any]:
        key = row["channel_key"]
        observers: list[str] = []
        if row["live_observers"]:
            try:
                parsed = json.loads(row["live_observers"])
                if isinstance(parsed, list):
                    observers = [str(o) for o in parsed]
            except (json.JSONDecodeError, TypeError):
                observers = []
        path_count: int | None = None
        if row["node_paths"]:
            try:
                parsed_paths = json.loads(row["node_paths"])
                path_count = len(parsed_paths) if isinstance(parsed_paths, list) else None
            except (json.JSONDecodeError, TypeError):
                path_count = None
        message_id = row["message_id"]
        return {
            "key": f"m{message_id}" if message_id is not None else f"h{row['packet_hash']}",
            "source": row["source"],
            "channel_key": key,
            "channel_name": local_names.get(key) or row["live_channel_name"],
            "sender": row["node_sender"] or row["live_sender"],
            "text": row["text"],
            "sender_timestamp": row["sender_timestamp"],
            "seen_at": int(row["seen_at"]),
            "outgoing": bool(row["outgoing"]),
            "message_id": message_id,
            "node_received_at": row["node_received_at"],
            "node_path_count": path_count,
            "live_first_seen": row["live_first_seen"],
            "live_last_seen": row["live_last_seen"],
            "live_repeats": row["live_repeats"],
            "live_observers": observers,
            "live_hops": row["live_hops"],
            "live_snr": row["live_snr"],
        }


def _source_field(source: str) -> str:
    return {"both": "both", "node": "node_only", "live": "live_only"}[source]
