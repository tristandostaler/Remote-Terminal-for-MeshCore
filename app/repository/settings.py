import json
import logging
import time
from typing import Any

import aiosqlite

from app import clock_drift
from app.clock_drift import DRIFT_BUCKET_SECONDS
from app.database import db
from app.models import AppSettings
from app.path_utils import bucket_path_hash_widths, bucket_region_scope, parse_packet_envelope
from app.send_attempts import clamp_message_retries
from app.stats_windows import DEFAULT_STATS_WINDOW, bucket_seconds_for_span, window_cutoff
from app.telemetry_interval import DEFAULT_TELEMETRY_INTERVAL_HOURS

logger = logging.getLogger(__name__)

SECONDS_1H = 3600
SECONDS_24H = 86400
SECONDS_7D = 604800

# Widening the window from a day to "all time" turns two of these queries into
# full-table scans that also parse every packet in Python. Both are capped at
# the most recent N rows in the window and report ``truncated`` so the UI can
# say the number is a sample rather than quietly showing a partial total.
MAX_SCAN_ROWS = 250_000


class AppSettingsRepository:
    """Repository for app_settings table (single-row pattern).

    Public methods acquire the DB lock exactly once. ``toggle_*`` helpers that
    need a read-modify-write do so inside a single ``db.tx()`` — the internal
    ``_get_in_conn`` / ``_apply_updates`` helpers run under the caller's
    already-held lock and must NEVER call ``db.tx()`` or ``db.readonly()``.
    """

    @staticmethod
    async def _get_in_conn(conn: aiosqlite.Connection) -> AppSettings:
        """Load settings using an already-acquired connection.

        Used by the public ``get()`` and by multi-step operations
        (``toggle_blocked_key``, ``toggle_blocked_name``) to avoid re-entering
        the non-reentrant DB lock.
        """
        async with conn.execute(
            """
            SELECT max_radio_contacts, auto_decrypt_dm_on_advert,
                   last_message_times,
                   advert_interval, last_advert_time, flood_scope, known_regions,
                   blocked_keys, blocked_names, discovery_blocked_types,
                   tracked_telemetry_repeaters, tracked_telemetry_contacts,
                   clock_sync_repeaters,
                   auto_resend_channel, max_message_retries,
                   telemetry_interval_hours, telemetry_routed_hourly,
                   virtual_node_allow_admin_commands
            FROM app_settings WHERE id = 1
            """
        ) as cursor:
            row = await cursor.fetchone()

        if not row:
            # Should not happen after migration, but handle gracefully
            return AppSettings()

        # Parse last_message_times JSON
        last_message_times: dict[str, int] = {}
        if row["last_message_times"]:
            try:
                last_message_times = json.loads(row["last_message_times"])
            except (json.JSONDecodeError, TypeError) as e:
                logger.warning(
                    "Failed to parse last_message_times JSON, using empty dict: %s",
                    e,
                )
                last_message_times = {}

        # Parse blocked_keys JSON
        blocked_keys: list[str] = []
        if row["blocked_keys"]:
            try:
                blocked_keys = json.loads(row["blocked_keys"])
            except (json.JSONDecodeError, TypeError):
                blocked_keys = []

        # Parse blocked_names JSON
        blocked_names: list[str] = []
        if row["blocked_names"]:
            try:
                blocked_names = json.loads(row["blocked_names"])
            except (json.JSONDecodeError, TypeError):
                blocked_names = []

        # Parse known_regions JSON
        known_regions: list[str] = []
        try:
            raw_regions = row["known_regions"]
            if raw_regions:
                known_regions = json.loads(raw_regions)
        except (json.JSONDecodeError, TypeError, KeyError):
            known_regions = []

        # Parse discovery_blocked_types JSON
        discovery_blocked_types: list[int] = []
        if row["discovery_blocked_types"]:
            try:
                discovery_blocked_types = json.loads(row["discovery_blocked_types"])
            except (json.JSONDecodeError, TypeError):
                discovery_blocked_types = []

        # Parse tracked_telemetry_repeaters JSON
        tracked_telemetry_repeaters: list[str] = []
        try:
            raw_tracked = row["tracked_telemetry_repeaters"]
            if raw_tracked:
                tracked_telemetry_repeaters = json.loads(raw_tracked)
        except (json.JSONDecodeError, TypeError, KeyError):
            tracked_telemetry_repeaters = []

        # Parse tracked_telemetry_contacts JSON
        tracked_telemetry_contacts: list[str] = []
        try:
            raw_tracked_contacts = row["tracked_telemetry_contacts"]
            if raw_tracked_contacts:
                tracked_telemetry_contacts = json.loads(raw_tracked_contacts)
        except (json.JSONDecodeError, TypeError, KeyError):
            tracked_telemetry_contacts = []

        # Parse clock_sync_repeaters JSON
        clock_sync_repeaters: list[str] = []
        try:
            raw_clock_sync = row["clock_sync_repeaters"]
            if raw_clock_sync:
                clock_sync_repeaters = json.loads(raw_clock_sync)
        except (json.JSONDecodeError, TypeError, KeyError):
            clock_sync_repeaters = []

        # Parse auto_resend_channel boolean
        try:
            auto_resend_channel = bool(row["auto_resend_channel"])
        except (KeyError, TypeError):
            auto_resend_channel = False

        # Parse max_message_retries. Clamped on read so a value written by a
        # future version (or by hand) cannot reach the retry loop out of range.
        try:
            max_message_retries = clamp_message_retries(row["max_message_retries"])
        except (KeyError, TypeError):
            max_message_retries = clamp_message_retries(None)

        # Parse telemetry_interval_hours (migration adds the column with
        # default=8, but guard against older rows / partial migrations).
        try:
            raw_interval = row["telemetry_interval_hours"]
            telemetry_interval_hours = (
                int(raw_interval) if raw_interval is not None else DEFAULT_TELEMETRY_INTERVAL_HOURS
            )
        except (KeyError, TypeError, ValueError):
            telemetry_interval_hours = DEFAULT_TELEMETRY_INTERVAL_HOURS

        # Parse telemetry_routed_hourly boolean
        try:
            telemetry_routed_hourly = bool(row["telemetry_routed_hourly"])
        except (KeyError, TypeError):
            telemetry_routed_hourly = False

        try:
            virtual_node_allow_admin_commands = bool(row["virtual_node_allow_admin_commands"])
        except (KeyError, TypeError, IndexError):
            virtual_node_allow_admin_commands = False

        return AppSettings(
            max_radio_contacts=row["max_radio_contacts"],
            auto_decrypt_dm_on_advert=bool(row["auto_decrypt_dm_on_advert"]),
            last_message_times=last_message_times,
            advert_interval=row["advert_interval"] or 0,
            last_advert_time=row["last_advert_time"] or 0,
            flood_scope=row["flood_scope"] or "",
            known_regions=known_regions,
            blocked_keys=blocked_keys,
            blocked_names=blocked_names,
            discovery_blocked_types=discovery_blocked_types,
            tracked_telemetry_repeaters=tracked_telemetry_repeaters,
            tracked_telemetry_contacts=tracked_telemetry_contacts,
            clock_sync_repeaters=clock_sync_repeaters,
            auto_resend_channel=auto_resend_channel,
            max_message_retries=max_message_retries,
            telemetry_interval_hours=telemetry_interval_hours,
            telemetry_routed_hourly=telemetry_routed_hourly,
            virtual_node_allow_admin_commands=virtual_node_allow_admin_commands,
        )

    @staticmethod
    async def _apply_updates(
        conn: aiosqlite.Connection,
        *,
        max_radio_contacts: int | None = None,
        auto_decrypt_dm_on_advert: bool | None = None,
        last_message_times: dict[str, int] | None = None,
        advert_interval: int | None = None,
        last_advert_time: int | None = None,
        flood_scope: str | None = None,
        known_regions: list[str] | None = None,
        blocked_keys: list[str] | None = None,
        blocked_names: list[str] | None = None,
        discovery_blocked_types: list[int] | None = None,
        tracked_telemetry_repeaters: list[str] | None = None,
        tracked_telemetry_contacts: list[str] | None = None,
        clock_sync_repeaters: list[str] | None = None,
        auto_resend_channel: bool | None = None,
        max_message_retries: int | None = None,
        telemetry_interval_hours: int | None = None,
        telemetry_routed_hourly: bool | None = None,
        virtual_node_allow_admin_commands: bool | None = None,
    ) -> None:
        """Apply field updates using an already-acquired connection.

        Emits a single UPDATE statement inside the caller's transaction. Does
        NOT commit — the caller's ``db.tx()`` handles that.
        """
        updates: list[str] = []
        params: list[Any] = []

        if max_radio_contacts is not None:
            updates.append("max_radio_contacts = ?")
            params.append(max_radio_contacts)

        if auto_decrypt_dm_on_advert is not None:
            updates.append("auto_decrypt_dm_on_advert = ?")
            params.append(1 if auto_decrypt_dm_on_advert else 0)

        if last_message_times is not None:
            updates.append("last_message_times = ?")
            params.append(json.dumps(last_message_times))

        if advert_interval is not None:
            updates.append("advert_interval = ?")
            params.append(advert_interval)

        if last_advert_time is not None:
            updates.append("last_advert_time = ?")
            params.append(last_advert_time)

        if flood_scope is not None:
            updates.append("flood_scope = ?")
            params.append(flood_scope)

        if known_regions is not None:
            updates.append("known_regions = ?")
            params.append(json.dumps(known_regions))

        if blocked_keys is not None:
            updates.append("blocked_keys = ?")
            params.append(json.dumps(blocked_keys))

        if blocked_names is not None:
            updates.append("blocked_names = ?")
            params.append(json.dumps(blocked_names))

        if discovery_blocked_types is not None:
            updates.append("discovery_blocked_types = ?")
            params.append(json.dumps(discovery_blocked_types))

        if tracked_telemetry_repeaters is not None:
            updates.append("tracked_telemetry_repeaters = ?")
            params.append(json.dumps(tracked_telemetry_repeaters))

        if tracked_telemetry_contacts is not None:
            updates.append("tracked_telemetry_contacts = ?")
            params.append(json.dumps(tracked_telemetry_contacts))

        if clock_sync_repeaters is not None:
            updates.append("clock_sync_repeaters = ?")
            params.append(json.dumps(clock_sync_repeaters))

        if auto_resend_channel is not None:
            updates.append("auto_resend_channel = ?")
            params.append(1 if auto_resend_channel else 0)

        if max_message_retries is not None:
            updates.append("max_message_retries = ?")
            params.append(clamp_message_retries(max_message_retries))

        if telemetry_interval_hours is not None:
            updates.append("telemetry_interval_hours = ?")
            params.append(telemetry_interval_hours)

        if telemetry_routed_hourly is not None:
            updates.append("telemetry_routed_hourly = ?")
            params.append(1 if telemetry_routed_hourly else 0)

        if virtual_node_allow_admin_commands is not None:
            updates.append("virtual_node_allow_admin_commands = ?")
            params.append(1 if virtual_node_allow_admin_commands else 0)

        if updates:
            query = f"UPDATE app_settings SET {', '.join(updates)} WHERE id = 1"
            async with conn.execute(query, params):
                pass

    @staticmethod
    async def get() -> AppSettings:
        """Get the current app settings.

        Always returns settings - creates default row if needed (migration handles initial row).
        """
        async with db.readonly() as conn:
            return await AppSettingsRepository._get_in_conn(conn)

    @staticmethod
    async def update(
        max_radio_contacts: int | None = None,
        auto_decrypt_dm_on_advert: bool | None = None,
        last_message_times: dict[str, int] | None = None,
        advert_interval: int | None = None,
        last_advert_time: int | None = None,
        flood_scope: str | None = None,
        known_regions: list[str] | None = None,
        blocked_keys: list[str] | None = None,
        blocked_names: list[str] | None = None,
        discovery_blocked_types: list[int] | None = None,
        tracked_telemetry_repeaters: list[str] | None = None,
        tracked_telemetry_contacts: list[str] | None = None,
        clock_sync_repeaters: list[str] | None = None,
        auto_resend_channel: bool | None = None,
        max_message_retries: int | None = None,
        telemetry_interval_hours: int | None = None,
        telemetry_routed_hourly: bool | None = None,
        virtual_node_allow_admin_commands: bool | None = None,
    ) -> AppSettings:
        """Update app settings. Only provided fields are updated."""
        async with db.tx() as conn:
            await AppSettingsRepository._apply_updates(
                conn,
                max_radio_contacts=max_radio_contacts,
                auto_decrypt_dm_on_advert=auto_decrypt_dm_on_advert,
                last_message_times=last_message_times,
                advert_interval=advert_interval,
                last_advert_time=last_advert_time,
                flood_scope=flood_scope,
                known_regions=known_regions,
                blocked_keys=blocked_keys,
                blocked_names=blocked_names,
                discovery_blocked_types=discovery_blocked_types,
                tracked_telemetry_repeaters=tracked_telemetry_repeaters,
                tracked_telemetry_contacts=tracked_telemetry_contacts,
                clock_sync_repeaters=clock_sync_repeaters,
                auto_resend_channel=auto_resend_channel,
                max_message_retries=max_message_retries,
                telemetry_interval_hours=telemetry_interval_hours,
                telemetry_routed_hourly=telemetry_routed_hourly,
                virtual_node_allow_admin_commands=virtual_node_allow_admin_commands,
            )
            return await AppSettingsRepository._get_in_conn(conn)

    @staticmethod
    async def toggle_blocked_key(key: str) -> AppSettings:
        """Toggle a public key in the blocked list. Keys are normalized to lowercase.

        Read-modify-write is atomic under a single ``db.tx()`` lock — two
        concurrent toggles for the same key cannot produce an inconsistent
        intermediate state.
        """
        normalized = key.lower()
        async with db.tx() as conn:
            settings = await AppSettingsRepository._get_in_conn(conn)
            if normalized in settings.blocked_keys:
                new_keys = [k for k in settings.blocked_keys if k != normalized]
            else:
                new_keys = settings.blocked_keys + [normalized]
            await AppSettingsRepository._apply_updates(conn, blocked_keys=new_keys)
            return await AppSettingsRepository._get_in_conn(conn)

    @staticmethod
    async def toggle_blocked_name(name: str) -> AppSettings:
        """Toggle a display name in the blocked list.

        Same atomicity guarantee as ``toggle_blocked_key``.
        """
        async with db.tx() as conn:
            settings = await AppSettingsRepository._get_in_conn(conn)
            if name in settings.blocked_names:
                new_names = [n for n in settings.blocked_names if n != name]
            else:
                new_names = settings.blocked_names + [name]
            await AppSettingsRepository._apply_updates(conn, blocked_names=new_names)
            return await AppSettingsRepository._get_in_conn(conn)

    @staticmethod
    async def get_vapid_keys() -> tuple[str, str]:
        """Return (private_key_pem, public_key_b64url) from app_settings.

        These are internal-only columns not exposed via the AppSettings model.
        """
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT vapid_private_key, vapid_public_key FROM app_settings WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        if row and row["vapid_private_key"] and row["vapid_public_key"]:
            return row["vapid_private_key"], row["vapid_public_key"]
        return "", ""

    @staticmethod
    async def set_vapid_keys(private_key: str, public_key: str) -> None:
        """Persist auto-generated VAPID key pair to app_settings."""
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE app_settings SET vapid_private_key = ?, vapid_public_key = ? WHERE id = 1",
                (private_key, public_key),
            )

    @staticmethod
    async def get_push_conversations() -> list[str]:
        """Return the global list of push-enabled conversation state keys.

        Internal-only column, not exposed via the AppSettings model.
        """
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT push_conversations FROM app_settings WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
        if row and row["push_conversations"]:
            try:
                return json.loads(row["push_conversations"])
            except (json.JSONDecodeError, TypeError):
                return []
        return []

    @staticmethod
    async def set_push_conversations(conversations: list[str]) -> list[str]:
        """Replace the global push-enabled conversation list."""
        async with db.tx() as conn:
            await conn.execute(
                "UPDATE app_settings SET push_conversations = ? WHERE id = 1",
                (json.dumps(conversations),),
            )
        return conversations

    @staticmethod
    async def toggle_push_conversation(key: str) -> list[str]:
        """Add or remove a conversation state key from the global push list.

        Atomic read-modify-write under a single ``db.tx()`` lock.
        """
        async with db.tx() as conn:
            async with conn.execute(
                "SELECT push_conversations FROM app_settings WHERE id = 1"
            ) as cursor:
                row = await cursor.fetchone()
            current: list[str] = []
            if row and row["push_conversations"]:
                try:
                    current = json.loads(row["push_conversations"])
                except (json.JSONDecodeError, TypeError):
                    current = []
            if key in current:
                current = [k for k in current if k != key]
            else:
                current.append(key)
            await conn.execute(
                "UPDATE app_settings SET push_conversations = ? WHERE id = 1",
                (json.dumps(current),),
            )
        return current


class StatisticsRepository:
    @staticmethod
    async def get_database_message_totals() -> dict[str, int]:
        """Return message totals needed by lightweight debug surfaces."""
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT
                    SUM(CASE WHEN type = 'PRIV' THEN 1 ELSE 0 END) AS total_dms,
                    SUM(CASE WHEN type = 'CHAN' THEN 1 ELSE 0 END) AS total_channel_messages,
                    SUM(CASE WHEN outgoing = 1 THEN 1 ELSE 0 END) AS total_outgoing
                FROM messages
                """
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        return {
            "total_dms": row["total_dms"] or 0,
            "total_channel_messages": row["total_channel_messages"] or 0,
            "total_outgoing": row["total_outgoing"] or 0,
        }

    @staticmethod
    async def _activity_counts(
        *, contact_type: int, exclude: bool = False, cutoff: int | None
    ) -> dict[str, int]:
        """Get time-windowed counts for contacts/repeaters heard.

        The 1h/24h/7d columns are fixed so the table always offers the same
        three reference points; ``window`` is the count over the selected
        statistics window, which may be wider than any of them (``cutoff``
        ``None`` means all time).
        """
        now = int(time.time())
        op = "!=" if exclude else "="
        window_expr = "1" if cutoff is None else "CASE WHEN last_seen >= ? THEN 1 ELSE 0 END"
        params: list[int] = [now - SECONDS_1H, now - SECONDS_24H, now - SECONDS_7D]
        if cutoff is not None:
            params.append(cutoff)
        params.append(contact_type)
        async with db.readonly() as conn:
            async with conn.execute(
                f"""
                SELECT
                    SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS last_hour,
                    SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS last_24_hours,
                    SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS last_week,
                    SUM({window_expr}) AS window_count
                FROM contacts
                WHERE type {op} ? AND last_seen IS NOT NULL
                """,
                tuple(params),
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None  # Aggregate query always returns a row
        return {
            "last_hour": row["last_hour"] or 0,
            "last_24_hours": row["last_24_hours"] or 0,
            "last_week": row["last_week"] or 0,
            "window": row["window_count"] or 0,
        }

    @staticmethod
    async def _known_channels_active(cutoff: int | None) -> dict[str, int]:
        """Count known channel keys with any traffic in each time window.

        Channel keys are stored canonically as uppercase hex, so we can avoid
        the old UPPER(...) join and aggregate per known channel directly.
        """
        now = int(time.time())
        window_expr = "1" if cutoff is None else "CASE WHEN last_received_at >= ? THEN 1 ELSE 0 END"
        params: list[int] = [now - SECONDS_1H, now - SECONDS_24H, now - SECONDS_7D]
        if cutoff is not None:
            params.append(cutoff)
        async with db.readonly() as conn:
            async with conn.execute(
                f"""
                WITH known AS (
                    SELECT conversation_key, MAX(received_at) AS last_received_at
                    FROM messages
                    WHERE type = 'CHAN'
                      AND conversation_key IN (SELECT key FROM channels)
                    GROUP BY conversation_key
                )
                SELECT
                    SUM(CASE WHEN last_received_at >= ? THEN 1 ELSE 0 END) AS last_hour,
                    SUM(CASE WHEN last_received_at >= ? THEN 1 ELSE 0 END) AS last_24_hours,
                    SUM(CASE WHEN last_received_at >= ? THEN 1 ELSE 0 END) AS last_week,
                    SUM({window_expr}) AS window_count
                FROM known
                """,
                tuple(params),
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        return {
            "last_hour": row["last_hour"] or 0,
            "last_24_hours": row["last_24_hours"] or 0,
            "last_week": row["last_week"] or 0,
            "window": row["window_count"] or 0,
        }

    @staticmethod
    async def _packets_over_time(cutoff: int | None, now: int) -> dict:
        """Bucket packet arrivals across the window for the activity chart.

        The bucket width scales with the window (hourly for a week, daily-ish
        for a year) so the series stays chart-sized instead of shipping one
        point per hour of a year. Counting is done in SQL, so a wide window
        costs an index scan rather than a fetch of every row.
        """
        async with db.readonly() as conn:
            if cutoff is None:
                async with conn.execute(
                    "SELECT MIN(timestamp) AS oldest FROM raw_packets"
                ) as cursor:
                    bound = await cursor.fetchone()
                oldest = bound["oldest"] if bound else None
                span = 0 if oldest is None else max(0, now - oldest)
            else:
                span = max(0, now - cutoff)

            bucket = bucket_seconds_for_span(span)
            params: tuple = (bucket, bucket)
            where = ""
            if cutoff is not None:
                where = "WHERE timestamp >= ?"
                params = (bucket, bucket, cutoff)

            async with conn.execute(
                f"""
                SELECT (timestamp / ?) * ? AS bucket_ts, COUNT(*) AS count
                FROM raw_packets
                {where}
                GROUP BY bucket_ts
                ORDER BY bucket_ts
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()

        return {
            "bucket_seconds": bucket,
            "buckets": [{"timestamp": row["bucket_ts"], "count": row["count"]} for row in rows],
        }

    @staticmethod
    async def _packet_shape(
        cutoff: int | None,
    ) -> tuple[dict[str, int | float], dict[str, int | float]]:
        """Bucket the window's raw packets by hop hash width and region scope.

        Both buckets come from one fetch so the snapshot only scans the packet
        table once. Every row is parsed in Python, so the fetch is capped at the
        most recent ``MAX_SCAN_ROWS`` packets in the window; past that the
        result is a recent sample and says so via ``truncated``. Returns
        ``(path_hash_width, region_scope)``.
        """
        async with db.readonly() as conn:
            if cutoff is None:
                async with conn.execute(
                    "SELECT data FROM raw_packets ORDER BY timestamp DESC LIMIT ?",
                    (MAX_SCAN_ROWS + 1,),
                ) as cursor:
                    rows = list(await cursor.fetchall())
            else:
                async with conn.execute(
                    "SELECT data FROM raw_packets WHERE timestamp >= ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (cutoff, MAX_SCAN_ROWS + 1),
                ) as cursor:
                    rows = list(await cursor.fetchall())

        truncated = len(rows) > MAX_SCAN_ROWS
        if truncated:
            rows = rows[:MAX_SCAN_ROWS]

        path_hash_width = bucket_path_hash_widths(rows)
        path_hash_width["truncated"] = truncated
        region_scope = bucket_region_scope(rows)
        region_scope["truncated"] = truncated
        return path_hash_width, region_scope

    @staticmethod
    async def get_mesh_summary() -> dict[str, int]:
        """Small mesh summary for bot ``ctx.mesh_stats()`` and schedule placeholders."""
        now = int(time.time())
        stats: dict[str, int] = {
            "total_contacts": 0,
            "total_repeaters": 0,
            "contacts_24h": 0,
            "repeaters_24h": 0,
            "new_contacts_7d": 0,
            "messages_24h": 0,
        }
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT
                    SUM(CASE WHEN type != 2 THEN 1 ELSE 0 END) AS contacts,
                    SUM(CASE WHEN type = 2 THEN 1 ELSE 0 END) AS repeaters,
                    SUM(CASE WHEN type != 2 AND last_seen >= ? THEN 1 ELSE 0 END) AS contacts_24h,
                    SUM(CASE WHEN type = 2 AND last_seen >= ? THEN 1 ELSE 0 END) AS repeaters_24h,
                    SUM(CASE WHEN first_seen >= ? THEN 1 ELSE 0 END) AS new_7d
                FROM contacts
                """,
                (now - SECONDS_24H, now - SECONDS_24H, now - SECONDS_7D),
            ) as cursor:
                row = await cursor.fetchone()
            async with conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE received_at >= ?",
                (now - SECONDS_24H,),
            ) as cursor:
                msg_row = await cursor.fetchone()
        if row is not None:
            stats["total_contacts"] = row["contacts"] or 0
            stats["total_repeaters"] = row["repeaters"] or 0
            stats["contacts_24h"] = row["contacts_24h"] or 0
            stats["repeaters_24h"] = row["repeaters_24h"] or 0
            stats["new_contacts_7d"] = row["new_7d"] or 0
        if msg_row is not None:
            stats["messages_24h"] = msg_row["cnt"] or 0
        return stats

    @staticmethod
    async def _multibyte_rollout() -> dict[str, int]:
        """Contact-level multibyte path adoption, by known direct-route hop width.

        Folded in from meshcore-bot's rollout monitor. Packet-level widths are
        reported separately (``path_hash_width``); this counts *nodes*, so
        an operator can see who has upgraded rather than how much traffic has.
        """
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT
                    SUM(CASE WHEN direct_path_hash_mode IN (1, 2, 3) THEN 1 ELSE 0 END)
                        AS with_route,
                    SUM(CASE WHEN direct_path_hash_mode IN (2, 3) THEN 1 ELSE 0 END)
                        AS multibyte,
                    SUM(CASE WHEN direct_path_hash_mode = 1 THEN 1 ELSE 0 END) AS single_byte,
                    SUM(CASE WHEN direct_path_hash_mode = 2 THEN 1 ELSE 0 END) AS double_byte,
                    SUM(CASE WHEN direct_path_hash_mode = 3 THEN 1 ELSE 0 END) AS triple_byte,
                    SUM(CASE WHEN type = 2 AND direct_path_hash_mode IN (1, 2, 3)
                             THEN 1 ELSE 0 END) AS repeaters_with_route,
                    SUM(CASE WHEN type = 2 AND direct_path_hash_mode IN (2, 3)
                             THEN 1 ELSE 0 END) AS repeaters_multibyte
                FROM contacts
                """
            ) as cursor:
                row = await cursor.fetchone()
        assert row is not None
        return {
            "contacts_with_route": row["with_route"] or 0,
            "contacts_multibyte": row["multibyte"] or 0,
            "single_byte": row["single_byte"] or 0,
            "double_byte": row["double_byte"] or 0,
            "triple_byte": row["triple_byte"] or 0,
            "repeaters_with_route": row["repeaters_with_route"] or 0,
            "repeaters_multibyte": row["repeaters_multibyte"] or 0,
        }

    @staticmethod
    async def _region_scope_senders(cutoff: int | None) -> dict[str, int | float]:
        """Count distinct window senders who scoped at least one channel send.

        Sender attribution requires having decrypted the message, so this only
        covers channels we hold keys for — a narrower population than the
        packet-level count, but a self-validating one: a message we decrypted is
        provably not a corrupt capture, so this number needs no noise floor.

        Scoping is read from ``messages.transport_code`` where present, falling
        back to the linked raw packet for rows stored before region tagging
        existed. Senders are keyed by ``sender_key`` where resolved, falling back
        to ``sender_name`` — one physical operator may run several nodes, hence
        "senders" rather than "users".
        """
        where = "WHERE m.type = 'CHAN' AND m.outgoing = 0"
        params: tuple = (MAX_SCAN_ROWS,)
        if cutoff is not None:
            where += " AND m.received_at >= ?"
            params = (cutoff, MAX_SCAN_ROWS)
        async with db.readonly() as conn:
            async with conn.execute(
                f"""
                SELECT m.id, m.sender_key, m.sender_name, m.transport_code, p.data
                FROM messages m
                LEFT JOIN raw_packets p ON p.message_id = m.id
                {where}
                ORDER BY m.received_at DESC
                LIMIT ?
                """,
                params,
            ) as cursor:
                rows = await cursor.fetchall()

        senders: set[str] = set()
        scoped_senders: set[str] = set()
        for row in rows:
            identity = row["sender_key"] or row["sender_name"]
            if not identity:
                continue
            senders.add(identity)

            if row["transport_code"] is not None:
                scoped_senders.add(identity)
                continue
            raw = row["data"]
            if raw is None:
                continue
            envelope = parse_packet_envelope(bytes(raw))
            if envelope is not None and envelope.transport_codes is not None:
                scoped_senders.add(identity)

        total = len(senders)
        scoped = len(scoped_senders)
        return {
            "total_senders": total,
            "scoped_senders": scoped,
            "scoped_senders_pct": (scoped / total) * 100 if total else 0.0,
        }

    @staticmethod
    async def _repeater_clock_drift(cutoff: int | None, now: int) -> dict:
        """Aggregate repeater clock drift over the window.

        Two shapes come out of one pass over ``contact_clock_drift``:

        - **Per repeater** -- its latest reading in the window, plus the buckets
          needed to fit a trend. This is what the rankings and the histogram are
          built from, one row per repeater so a chatty node cannot dominate.
        - **Over time** -- mean and worst |drift| per bucket across all
          repeaters, so a mesh-wide degradation is visible as a rising line
          rather than something you have to infer from a table.

        The median is reported signed as well as absolute on purpose. One
        repeater far off is that repeater's problem; all of them off by the same
        amount is this server's clock, and only the signed median distinguishes
        those two.
        """
        window = (now - cutoff) if cutoff is not None else None
        bucket = bucket_seconds_for_span(
            window if window is not None else SECONDS_7D, minimum=DRIFT_BUCKET_SECONDS
        )
        where = "" if cutoff is None else "AND d.bucket_start >= ?"
        window_params: tuple = () if cutoff is None else (cutoff,)

        async with db.readonly() as conn:
            # Denominator: repeaters heard in the window at all, so "12 of 30
            # measured" reads honestly instead of hiding the ones we have no
            # advert timestamps for.
            heard_where = "" if cutoff is None else "AND last_seen >= ?"
            async with conn.execute(
                f"SELECT COUNT(*) AS cnt FROM contacts "
                f"WHERE type = 2 AND last_seen IS NOT NULL {heard_where}",
                window_params,
            ) as cursor:
                row = await cursor.fetchone()
            repeaters_total = (row["cnt"] if row else 0) or 0

            # One row per repeater: its newest bucket in the window. The bare
            # columns beside MAX(bucket_start) come from that same input row,
            # which is the SQLite guarantee this relies on.
            async with conn.execute(
                f"""
                SELECT d.public_key,
                       c.name AS name,
                       MAX(d.bucket_start) AS latest_bucket,
                       d.drift_seconds AS drift_seconds,
                       d.observed_at AS observed_at,
                       d.advert_timestamp AS advert_timestamp,
                       SUM(d.sample_count) AS sample_count,
                       COUNT(*) AS bucket_count
                FROM contact_clock_drift d
                JOIN contacts c ON c.public_key = d.public_key
                WHERE c.type = 2 {where}
                GROUP BY d.public_key
                """,
                window_params,
            ) as cursor:
                latest_rows = await cursor.fetchall()

            # Series per repeater, only for the ones a trend could apply to.
            # Fitting a slope needs the points, so this is the one place a
            # per-bucket fetch is unavoidable -- it is bounded by
            # (repeaters x buckets in window) on a table that holds at most one
            # row per node per hour.
            async with conn.execute(
                f"""
                SELECT d.public_key, d.bucket_start, d.drift_seconds
                FROM contact_clock_drift d
                JOIN contacts c ON c.public_key = d.public_key
                WHERE c.type = 2 {where}
                ORDER BY d.public_key, d.bucket_start
                """,
                window_params,
            ) as cursor:
                series_rows = await cursor.fetchall()

            # Readings from a clock that was never set are decades out, which on a
            # shared axis flattens every real repeater into the baseline. They are
            # counted in the bands and the histogram instead, where a magnitude
            # bucket absorbs them without distorting anything.
            async with conn.execute(
                f"""
                SELECT (d.bucket_start / ?) * ? AS slot,
                       AVG(ABS(d.drift_seconds)) AS mean_abs,
                       MAX(ABS(d.drift_seconds)) AS max_abs,
                       COUNT(DISTINCT d.public_key) AS repeaters
                FROM contact_clock_drift d
                JOIN contacts c ON c.public_key = d.public_key
                WHERE c.type = 2 AND d.advert_timestamp >= ? {where}
                GROUP BY slot
                ORDER BY slot
                """,
                (bucket, bucket, clock_drift.UNSET_CLOCK_BEFORE, *window_params),
            ) as cursor:
                over_time_rows = await cursor.fetchall()

        if not latest_rows:
            return {
                "repeaters_total": repeaters_total,
                "repeaters_with_samples": 0,
                "repeaters_unset_clock": 0,
                "sample_count": 0,
                "oldest_sample_at": None,
                "newest_sample_at": None,
                "in_sync": 0,
                "minor": 0,
                "major": 0,
                "severe": 0,
                "mean_abs_drift_seconds": 0.0,
                "median_abs_drift_seconds": 0.0,
                "median_drift_seconds": 0.0,
                "furthest_behind": None,
                "furthest_ahead": None,
                "worst_offenders": [],
                "fastest_rates": [],
                "unset_clocks": [],
                "histogram": [
                    {"label": label, "count": 0} for label in clock_drift.histogram_labels()
                ],
                "over_time": [],
                "bucket_seconds": bucket,
            }

        rates: dict[str, float | None] = {}
        grouped: dict[str, list[tuple[int, int]]] = {}
        for row in series_rows:
            grouped.setdefault(row["public_key"], []).append(
                (row["bucket_start"], row["drift_seconds"])
            )
        for key, points in grouped.items():
            rates[key] = clock_drift.drift_rate_per_day(points)

        entries: list[dict] = []
        severity_counts = {"in_sync": 0, "minor": 0, "major": 0, "severe": 0}
        histogram = [0] * len(clock_drift.histogram_labels())
        unset_clocks = 0
        total_samples = 0

        for row in latest_rows:
            drift = row["drift_seconds"]
            severity = clock_drift.classify_drift(drift)
            severity_counts[severity] += 1
            histogram[clock_drift.histogram_bin(drift)] += 1
            unset = clock_drift.is_unset_clock(row["advert_timestamp"])
            if unset:
                unset_clocks += 1
            total_samples += row["sample_count"] or 0
            entries.append(
                {
                    "public_key": row["public_key"],
                    "name": row["name"],
                    "drift_seconds": drift,
                    "observed_at": row["observed_at"],
                    "sample_count": row["sample_count"] or 0,
                    "bucket_count": row["bucket_count"] or 0,
                    "drift_rate_seconds_per_day": rates.get(row["public_key"]),
                    "severity": severity,
                    "clock_unset": unset,
                }
            )

        # A clock that was never set reads as decades behind. Left in, one of
        # them turns the mean into a meaningless number and monopolises every
        # ranking, so the summary statistics and the rankings run over clocks
        # that are merely *wrong*, and the unset ones get their own list. They
        # still count toward the severity bands and the histogram: being decades
        # off is genuinely severe, it is just a different repair.
        set_entries = [entry for entry in entries if not entry["clock_unset"]]
        unset_entries = [entry for entry in entries if entry["clock_unset"]]

        drifts = [entry["drift_seconds"] for entry in set_entries]
        abs_drifts = [abs(value) for value in drifts]

        by_magnitude = sorted(set_entries, key=lambda e: abs(e["drift_seconds"]), reverse=True)
        # Only clocks actually going somewhere. A steady offset is a one-resync
        # fix and already appears in the ranking above; padding this list with
        # them would bury the ones that need the node looked at.
        by_rate = sorted(
            (
                e
                for e in set_entries
                if e["drift_rate_seconds_per_day"] is not None
                and abs(e["drift_rate_seconds_per_day"]) >= clock_drift.NOTABLE_RATE_SECONDS_PER_DAY
            ),
            key=lambda e: abs(e["drift_rate_seconds_per_day"]),
            reverse=True,
        )
        by_signed = sorted(set_entries, key=lambda e: e["drift_seconds"])

        observed_times = [row["observed_at"] for row in latest_rows]
        # Both queries share one WHERE, so a non-empty latest_rows guarantees a
        # non-empty series; the fallback only exists so this cannot raise.
        oldest_bucket = (
            min(row["bucket_start"] for row in series_rows)
            if series_rows
            else min(row["latest_bucket"] for row in latest_rows)
        )

        return {
            # A repeater cannot have a reading in the window without also having
            # been heard in it, so the max() never fires -- but "12 of 11" would
            # be a worse thing to render than a slightly redundant clamp.
            "repeaters_total": max(repeaters_total, len(entries)),
            "repeaters_with_samples": len(entries),
            "repeaters_unset_clock": unset_clocks,
            "sample_count": total_samples,
            "oldest_sample_at": oldest_bucket,
            "newest_sample_at": max(observed_times),
            **severity_counts,
            "mean_abs_drift_seconds": (
                round(sum(abs_drifts) / len(abs_drifts), 1) if abs_drifts else 0.0
            ),
            "median_abs_drift_seconds": round(clock_drift.median(abs_drifts), 1),
            "median_drift_seconds": round(clock_drift.median(drifts), 1),
            "furthest_behind": (
                by_signed[0] if by_signed and by_signed[0]["drift_seconds"] < 0 else None
            ),
            "furthest_ahead": (
                by_signed[-1] if by_signed and by_signed[-1]["drift_seconds"] > 0 else None
            ),
            "worst_offenders": by_magnitude[:10],
            "fastest_rates": by_rate[:10],
            "unset_clocks": unset_entries[:10],
            "histogram": [
                {"label": label, "count": count}
                for label, count in zip(clock_drift.histogram_labels(), histogram, strict=True)
            ],
            "over_time": [
                {
                    "timestamp": row["slot"],
                    "mean_abs_drift_seconds": round(row["mean_abs"] or 0.0, 1),
                    "max_abs_drift_seconds": int(row["max_abs"] or 0),
                    "repeater_count": row["repeaters"] or 0,
                }
                for row in over_time_rows
            ],
            "bucket_seconds": bucket,
        }

    @staticmethod
    async def get_all(window: str = DEFAULT_STATS_WINDOW) -> dict:
        """Aggregate all statistics from existing tables over ``window``.

        Every time-bounded metric honours the same window, so the whole
        snapshot describes one period rather than a mix of 24h and 72h slices.
        ``window`` ``"all"`` drops the lower bound entirely.

        Each helper acquires its own lock; there's no requirement that the
        whole snapshot be atomic. If we ever wanted a consistent snapshot
        we'd batch all queries into a single ``db.readonly()`` and use
        ``_in_conn`` helpers, but statistics are intentionally approximate.
        """
        now = int(time.time())
        cutoff = window_cutoff(window, now)

        async with db.readonly() as conn:
            # Top 5 busiest channels in the window
            channel_where = "WHERE m.type = 'CHAN'"
            channel_params: tuple = ()
            if cutoff is not None:
                channel_where += " AND m.received_at >= ?"
                channel_params = (cutoff,)
            async with conn.execute(
                f"""
                SELECT m.conversation_key, COALESCE(c.name, m.conversation_key) AS channel_name,
                       COUNT(*) AS message_count
                FROM messages m
                LEFT JOIN channels c ON m.conversation_key = c.key
                {channel_where}
                GROUP BY m.conversation_key
                ORDER BY COUNT(*) DESC
                LIMIT 5
                """,
                channel_params,
            ) as cursor:
                rows = await cursor.fetchall()
            busiest_channels = [
                {
                    "channel_key": row["conversation_key"],
                    "channel_name": row["channel_name"],
                    "message_count": row["message_count"],
                }
                for row in rows
            ]

            # Entity counts
            async with conn.execute(
                "SELECT COUNT(*) AS cnt FROM contacts WHERE type != 2"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            contact_count: int = row["cnt"]

            async with conn.execute(
                "SELECT COUNT(*) AS cnt FROM contacts WHERE type = 2"
            ) as cursor:
                row = await cursor.fetchone()
            assert row is not None
            repeater_count: int = row["cnt"]

            async with conn.execute("SELECT COUNT(*) AS cnt FROM channels") as cursor:
                row = await cursor.fetchone()
            assert row is not None
            channel_count: int = row["cnt"]

            # Packet split
            async with conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN message_id IS NOT NULL THEN 1 ELSE 0 END) AS decrypted
                FROM raw_packets
                """
            ) as cursor:
                pkt_row = await cursor.fetchone()
            assert pkt_row is not None
            total_packets = pkt_row["total"] or 0
            decrypted_packets = pkt_row["decrypted"] or 0
            undecrypted_packets = total_packets - decrypted_packets

        # These each acquire their own lock. The snapshot isn't atomic across
        # them — fine for stats, which are approximate by nature.
        message_totals = await StatisticsRepository.get_database_message_totals()
        contacts_heard = await StatisticsRepository._activity_counts(
            contact_type=2, exclude=True, cutoff=cutoff
        )
        repeaters_heard = await StatisticsRepository._activity_counts(contact_type=2, cutoff=cutoff)
        known_channels_active = await StatisticsRepository._known_channels_active(cutoff)
        path_hash_width, region_scope = await StatisticsRepository._packet_shape(cutoff)
        region_scope.update(await StatisticsRepository._region_scope_senders(cutoff))
        multibyte_rollout = await StatisticsRepository._multibyte_rollout()
        packets_over_time = await StatisticsRepository._packets_over_time(cutoff, now)
        repeater_clock_drift = await StatisticsRepository._repeater_clock_drift(cutoff, now)

        return {
            "window": window,
            "window_seconds": None if cutoff is None else now - cutoff,
            "busiest_channels": busiest_channels,
            "contact_count": contact_count,
            "repeater_count": repeater_count,
            "channel_count": channel_count,
            "total_packets": total_packets,
            "decrypted_packets": decrypted_packets,
            "undecrypted_packets": undecrypted_packets,
            "total_dms": message_totals["total_dms"],
            "total_channel_messages": message_totals["total_channel_messages"],
            "total_outgoing": message_totals["total_outgoing"],
            "contacts_heard": contacts_heard,
            "repeaters_heard": repeaters_heard,
            "known_channels_active": known_channels_active,
            "path_hash_width": path_hash_width,
            "region_scope": region_scope,
            "multibyte_rollout": multibyte_rollout,
            "packets_over_time": packets_over_time,
            "repeater_clock_drift": repeater_clock_drift,
        }
