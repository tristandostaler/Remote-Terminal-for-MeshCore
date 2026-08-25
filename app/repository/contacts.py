import logging
import time
from collections.abc import Mapping
from typing import Any

from app.clock_drift import (
    DAY_SECONDS,
    DRIFT_BUCKET_SECONDS,
    DRIFT_FULL_RESOLUTION_SECONDS,
    bucket_start,
    classify_drift,
    day_start,
    drift_trend,
    histogram_bin,
    histogram_labels,
    is_unset_clock,
)
from app.database import db
from app.models import (
    ClockDriftBand,
    ClockDriftHistogramBin,
    ClockDriftHopBucket,
    ClockDriftSample,
    ClockDriftStep,
    Contact,
    ContactAdvertPath,
    ContactAdvertPathSummary,
    ContactClockDrift,
    ContactNameHistory,
    ContactUpsert,
    NodeClockDriftStats,
)
from app.path_utils import first_hop_hex, normalize_contact_route, normalize_route_override
from app.stats_windows import bucket_seconds_for_span

# How far back the contact info pane's drift section looks. Wide enough that a
# slow oscillator shows a slope, narrow enough that a clock fixed last month
# does not keep dominating the min/max of a node that is fine now.
CONTACT_DRIFT_WINDOW_SECONDS = 30 * 86400

# The node stats page lists step changes largest-first. Past a screenful the
# tail is not something anyone reads, and a clock that produced hundreds of
# them has one finding ("this clock is being reset constantly"), not hundreds.
MAX_CLOCK_STEPS = 20

logger = logging.getLogger(__name__)


class AmbiguousPublicKeyPrefixError(ValueError):
    """Raised when a public key prefix matches multiple contacts."""

    def __init__(self, prefix: str, matches: list[str]):
        self.prefix = prefix.lower()
        self.matches = matches
        super().__init__(f"Ambiguous public key prefix '{self.prefix}'")


class ContactRepository:
    @staticmethod
    def _coerce_contact_upsert(
        contact: ContactUpsert | Contact | Mapping[str, Any],
    ) -> ContactUpsert:
        if isinstance(contact, ContactUpsert):
            return contact
        if isinstance(contact, Contact):
            return contact.to_upsert()
        return ContactUpsert.model_validate(contact)

    @staticmethod
    async def upsert(contact: ContactUpsert | Contact | Mapping[str, Any]) -> None:
        contact_row = ContactRepository._coerce_contact_upsert(contact)
        if (
            contact_row.direct_path is None
            and contact_row.direct_path_len is None
            and contact_row.direct_path_hash_mode is None
        ):
            direct_path = None
            direct_path_len = None
            direct_path_hash_mode = None
        else:
            direct_path, direct_path_len, direct_path_hash_mode = normalize_contact_route(
                contact_row.direct_path,
                contact_row.direct_path_len,
                contact_row.direct_path_hash_mode,
            )
        route_override_path, route_override_len, route_override_hash_mode = (
            normalize_route_override(
                contact_row.route_override_path,
                contact_row.route_override_len,
                contact_row.route_override_hash_mode,
            )
        )

        async with db.tx() as conn:
            async with conn.execute(
                """
                INSERT INTO contacts (public_key, name, type, flags, direct_path, direct_path_len,
                                      direct_path_hash_mode, direct_path_updated_at,
                                      route_override_path, route_override_len,
                                      route_override_hash_mode,
                                      last_advert, lat, lon, last_seen,
                                      on_radio, last_contacted, first_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(public_key) DO UPDATE SET
                    name = COALESCE(excluded.name, contacts.name),
                    type = CASE WHEN excluded.type = 0 THEN contacts.type ELSE excluded.type END,
                    flags = excluded.flags,
                    direct_path = COALESCE(excluded.direct_path, contacts.direct_path),
                    direct_path_len = COALESCE(excluded.direct_path_len, contacts.direct_path_len),
                    direct_path_hash_mode = COALESCE(
                        excluded.direct_path_hash_mode, contacts.direct_path_hash_mode
                    ),
                    direct_path_updated_at = COALESCE(
                        excluded.direct_path_updated_at, contacts.direct_path_updated_at
                    ),
                    route_override_path = COALESCE(
                        excluded.route_override_path, contacts.route_override_path
                    ),
                    route_override_len = COALESCE(
                        excluded.route_override_len, contacts.route_override_len
                    ),
                    route_override_hash_mode = COALESCE(
                        excluded.route_override_hash_mode, contacts.route_override_hash_mode
                    ),
                    last_advert = COALESCE(excluded.last_advert, contacts.last_advert),
                    lat = COALESCE(excluded.lat, contacts.lat),
                    lon = COALESCE(excluded.lon, contacts.lon),
                    last_seen = CASE
                        WHEN excluded.last_seen IS NULL THEN contacts.last_seen
                        WHEN contacts.last_seen IS NULL THEN excluded.last_seen
                        WHEN excluded.last_seen > contacts.last_seen THEN excluded.last_seen
                        ELSE contacts.last_seen
                    END,
                    on_radio = COALESCE(excluded.on_radio, contacts.on_radio),
                    last_contacted = COALESCE(excluded.last_contacted, contacts.last_contacted),
                    first_seen = COALESCE(contacts.first_seen, excluded.first_seen)
                """,
                (
                    contact_row.public_key.lower(),
                    contact_row.name,
                    contact_row.type,
                    contact_row.flags,
                    direct_path,
                    direct_path_len,
                    direct_path_hash_mode,
                    contact_row.direct_path_updated_at,
                    route_override_path,
                    route_override_len,
                    route_override_hash_mode,
                    contact_row.last_advert,
                    contact_row.lat,
                    contact_row.lon,
                    contact_row.last_seen,
                    contact_row.on_radio,
                    contact_row.last_contacted,
                    contact_row.first_seen,
                ),
            ):
                pass

    @staticmethod
    def _row_to_contact(row) -> Contact:
        """Convert a database row to a Contact model."""
        available_columns = set(row.keys())
        direct_path, direct_path_len, direct_path_hash_mode = normalize_contact_route(
            row["direct_path"] if "direct_path" in available_columns else None,
            row["direct_path_len"] if "direct_path_len" in available_columns else None,
            row["direct_path_hash_mode"] if "direct_path_hash_mode" in available_columns else None,
        )
        route_override_path = (
            row["route_override_path"] if "route_override_path" in available_columns else None
        )
        route_override_len = (
            row["route_override_len"] if "route_override_len" in available_columns else None
        )
        route_override_hash_mode = (
            row["route_override_hash_mode"]
            if "route_override_hash_mode" in available_columns
            else None
        )
        route_override_path, route_override_len, route_override_hash_mode = (
            normalize_route_override(
                route_override_path,
                route_override_len,
                route_override_hash_mode,
            )
        )
        return Contact(
            public_key=row["public_key"],
            name=row["name"],
            type=row["type"],
            flags=row["flags"],
            direct_path=direct_path,
            direct_path_len=direct_path_len,
            direct_path_hash_mode=direct_path_hash_mode,
            direct_path_updated_at=(
                row["direct_path_updated_at"]
                if "direct_path_updated_at" in available_columns
                else None
            ),
            route_override_path=route_override_path,
            route_override_len=route_override_len,
            route_override_hash_mode=route_override_hash_mode,
            last_advert=row["last_advert"],
            lat=row["lat"],
            lon=row["lon"],
            last_seen=row["last_seen"],
            on_radio=bool(row["on_radio"]),
            favorite=bool(row["favorite"]) if "favorite" in available_columns else False,
            mcmp_enabled=(
                bool(row["mcmp_enabled"]) if "mcmp_enabled" in available_columns else False
            ),
            mcmp_version=(row["mcmp_version"] if "mcmp_version" in available_columns else 2),
            image_codec=(
                row["image_codec"]
                if "image_codec" in available_columns and row["image_codec"]
                else "ie4"
            ),
            raw_media_text_transport=(
                bool(row["raw_media_text_transport"])
                if "raw_media_text_transport" in available_columns
                else True
            ),
            last_contacted=row["last_contacted"],
            last_read_at=row["last_read_at"],
            first_seen=row["first_seen"],
        )

    @staticmethod
    async def get_by_key(public_key: str) -> Contact | None:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contacts WHERE public_key = ?", (public_key.lower(),)
            ) as cursor:
                row = await cursor.fetchone()
        return ContactRepository._row_to_contact(row) if row else None

    @staticmethod
    async def get_by_key_prefix(prefix: str) -> Contact | None:
        """Get a contact by key prefix only if it resolves uniquely.

        Returns None when no contacts match OR when multiple contacts match
        the prefix (to avoid silently selecting the wrong contact).
        """
        normalized_prefix = prefix.lower()
        exact = await ContactRepository.get_by_key(normalized_prefix)
        if exact:
            return exact
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contacts WHERE public_key LIKE ? ORDER BY public_key LIMIT 2",
                (f"{normalized_prefix}%",),
            ) as cursor:
                rows = list(await cursor.fetchall())
        if len(rows) != 1:
            return None
        return ContactRepository._row_to_contact(rows[0])

    @staticmethod
    async def _get_prefix_matches(prefix: str, limit: int = 2) -> list[Contact]:
        """Get contacts matching a key prefix, up to limit."""
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contacts WHERE public_key LIKE ? ORDER BY public_key LIMIT ?",
                (f"{prefix.lower()}%", limit),
            ) as cursor:
                rows = list(await cursor.fetchall())
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def get_by_key_or_prefix(key_or_prefix: str) -> Contact | None:
        """Get a contact by exact key match, falling back to prefix match.

        Useful when the input might be a full 64-char public key or a shorter prefix.
        """
        contact = await ContactRepository.get_by_key(key_or_prefix)
        if contact:
            return contact

        matches = await ContactRepository._get_prefix_matches(key_or_prefix, limit=2)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousPublicKeyPrefixError(
                key_or_prefix,
                [m.public_key for m in matches],
            )
        return None

    @staticmethod
    async def get_by_name(name: str) -> list[Contact]:
        """Get all contacts with the given exact name."""
        async with db.readonly() as conn:
            async with conn.execute("SELECT * FROM contacts WHERE name = ?", (name,)) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def resolve_prefixes(prefixes: list[str]) -> dict[str, Contact]:
        """Resolve multiple key prefixes to contacts in a single query.

        Returns a dict mapping each prefix to its Contact, only for prefixes
        that resolve uniquely (exactly one match). Ambiguous or unmatched
        prefixes are omitted.
        """
        if not prefixes:
            return {}
        normalized = [p.lower() for p in prefixes]
        conditions = " OR ".join(["public_key LIKE ?"] * len(normalized))
        params = [f"{p}%" for p in normalized]
        async with db.readonly() as conn:
            async with conn.execute(f"SELECT * FROM contacts WHERE {conditions}", params) as cursor:
                rows = await cursor.fetchall()
        # Group by which prefix each row matches
        prefix_to_rows: dict[str, list] = {p: [] for p in normalized}
        for row in rows:
            pk = row["public_key"]
            for p in normalized:
                if pk.startswith(p):
                    prefix_to_rows[p].append(row)
        # Only include uniquely-resolved prefixes
        result: dict[str, Contact] = {}
        for p in normalized:
            if len(prefix_to_rows[p]) == 1:
                result[p] = ContactRepository._row_to_contact(prefix_to_rows[p][0])
        return result

    @staticmethod
    async def get_all(limit: int = 100, offset: int = 0) -> list[Contact]:
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contacts ORDER BY COALESCE(name, public_key) LIMIT ? OFFSET ?",
                (limit, offset),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def get_repeaters_by_recent(limit: int = 8) -> list[Contact]:
        """Get repeater contacts ordered by most recently seen.

        Used by the region discovery sweep, which prefers recently-heard
        repeaters since the anon regions request is direct-routed and only
        in-range repeaters will answer.
        """
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT * FROM contacts
                WHERE type = 2 AND length(public_key) = 64
                ORDER BY COALESCE(last_seen, 0) DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def get_recently_contacted_non_repeaters(limit: int = 200) -> list[Contact]:
        """Get recently interacted-with non-repeater contacts."""
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT * FROM contacts
                WHERE type != 2 AND last_contacted IS NOT NULL AND length(public_key) = 64
                ORDER BY last_contacted DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def get_recently_dm_active_non_repeaters(limit: int = 200) -> list[Contact]:
        """Get non-repeater contacts with the most recent DM activity (sent or received)."""
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT c.*
                FROM contacts c
                INNER JOIN (
                    SELECT conversation_key, MAX(received_at) AS last_dm
                    FROM messages
                    WHERE type = 'PRIV'
                    GROUP BY conversation_key
                ) m ON c.public_key = m.conversation_key
                WHERE c.type != 2 AND length(c.public_key) = 64
                ORDER BY m.last_dm DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def get_recently_advertised_non_repeaters(limit: int = 200) -> list[Contact]:
        """Get recently advert-heard non-repeater contacts."""
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT * FROM contacts
                WHERE type != 2 AND last_advert IS NOT NULL AND length(public_key) = 64
                ORDER BY last_advert DESC
                LIMIT ?
                """,
                (limit,),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def update_direct_path(
        public_key: str,
        path: str,
        path_len: int,
        path_hash_mode: int | None = None,
        updated_at: int | None = None,
    ) -> None:
        """Persist a learned direct route for a contact.

        Both callers (the RF PATH packet processor and the firmware PATH_UPDATE
        event handler) are RF-backed: firmware ``onContactPathUpdated`` only
        fires from ``onContactPathRecv`` during RF PATH packet reception. So
        this method also advances ``last_seen`` monotonically. Never moves
        ``last_seen`` backwards if an out-of-order arrival lands with an older
        timestamp.
        """
        normalized_path, normalized_path_len, normalized_hash_mode = normalize_contact_route(
            path,
            path_len,
            path_hash_mode,
        )
        ts = updated_at if updated_at is not None else int(time.time())
        async with db.tx() as conn:
            async with conn.execute(
                """UPDATE contacts SET direct_path = ?, direct_path_len = ?,
                   direct_path_hash_mode = COALESCE(?, direct_path_hash_mode),
                   direct_path_updated_at = ?,
                   last_seen = CASE
                       WHEN last_seen IS NULL THEN ?
                       WHEN ? > last_seen THEN ?
                       ELSE last_seen
                   END
                   WHERE public_key = ?""",
                (
                    normalized_path,
                    normalized_path_len,
                    normalized_hash_mode,
                    ts,
                    ts,
                    ts,
                    ts,
                    public_key.lower(),
                ),
            ):
                pass

    @staticmethod
    async def set_routing_override(
        public_key: str,
        path: str | None,
        path_len: int | None,
        path_hash_mode: int | None = None,
    ) -> None:
        normalized_path, normalized_len, normalized_hash_mode = normalize_route_override(
            path,
            path_len,
            path_hash_mode,
        )
        async with db.tx() as conn:
            async with conn.execute(
                """
                UPDATE contacts
                SET route_override_path = ?, route_override_len = ?, route_override_hash_mode = ?
                WHERE public_key = ?
                """,
                (
                    normalized_path,
                    normalized_len,
                    normalized_hash_mode,
                    public_key.lower(),
                ),
            ):
                pass

    @staticmethod
    async def clear_routing_override(public_key: str) -> None:
        async with db.tx() as conn:
            async with conn.execute(
                """
                UPDATE contacts
                SET route_override_path = NULL,
                    route_override_len = NULL,
                    route_override_hash_mode = NULL
                WHERE public_key = ?
                """,
                (public_key.lower(),),
            ):
                pass

    @staticmethod
    async def clear_on_radio_except(keep_keys: list[str]) -> None:
        """Set on_radio=False for all contacts NOT in keep_keys."""
        async with db.tx() as conn:
            if not keep_keys:
                async with conn.execute("UPDATE contacts SET on_radio = 0 WHERE on_radio = 1"):
                    pass
            else:
                placeholders = ",".join("?" * len(keep_keys))
                async with conn.execute(
                    f"UPDATE contacts SET on_radio = 0 WHERE on_radio = 1 AND public_key NOT IN ({placeholders})",
                    keep_keys,
                ):
                    pass

    @staticmethod
    async def get_favorites() -> list[Contact]:
        """Return all contacts marked as favorite."""
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contacts WHERE favorite = 1 AND LENGTH(public_key) = 64"
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]

    @staticmethod
    async def set_favorite(public_key: str, value: bool) -> None:
        """Set or clear the favorite flag for a contact."""
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET favorite = ? WHERE public_key = ?",
                (1 if value else 0, public_key.lower()),
            ):
                pass

    @staticmethod
    async def set_mcmp_enabled(public_key: str, value: bool) -> bool:
        """Enable/disable MCMP compression for a contact. True if a row was found."""
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET mcmp_enabled = ? WHERE public_key = ?",
                (1 if value else 0, public_key.lower()),
            ) as cursor:
                rowcount = cursor.rowcount
        return rowcount > 0

    @staticmethod
    async def set_mcmp_version(public_key: str, version: int) -> bool:
        """Set the MCMP transport version (2 or 3) for a contact."""
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET mcmp_version = ? WHERE public_key = ?",
                (version, public_key.lower()),
            ) as cursor:
                rowcount = cursor.rowcount
        return rowcount > 0

    @staticmethod
    async def set_image_codec(public_key: str, codec: str) -> bool:
        """Set the outbound image codec ("ie4" or "aeic") for a contact."""
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET image_codec = ? WHERE public_key = ?",
                (codec, public_key.lower()),
            ) as cursor:
                rowcount = cursor.rowcount
        return rowcount > 0

    @staticmethod
    async def set_raw_media_text_transport(public_key: str, enabled: bool) -> bool:
        """Turn the rmt1: text transport on or off for this contact's media."""
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET raw_media_text_transport = ? WHERE public_key = ?",
                (1 if enabled else 0, public_key.lower()),
            ) as cursor:
                rowcount = cursor.rowcount
        return rowcount > 0

    @staticmethod
    async def delete(public_key: str) -> None:
        normalized = public_key.lower()
        # contact_name_history and contact_advert_paths cascade via FK.
        # Messages are intentionally preserved so history re-surfaces
        # if the contact is re-added later.
        async with db.tx() as conn:
            async with conn.execute("DELETE FROM contacts WHERE public_key = ?", (normalized,)):
                pass

    @staticmethod
    async def update_last_contacted(public_key: str, timestamp: int | None = None) -> None:
        """Update the last_contacted timestamp for a contact.

        ``last_contacted`` tracks the most recent direct-conversation activity
        with this contact in either direction (incoming or outgoing DM). It is
        the field that powers "recent conversations" ordering on the frontend.

        It deliberately does not touch ``last_seen``: ``last_seen`` is reserved
        for actual RF reception from the contact, and outgoing sends are not
        evidence that we heard from them. RF observations from DM ingest update
        ``last_seen`` via :meth:`touch_last_seen` on incoming DMs only.
        """
        ts = timestamp if timestamp is not None else int(time.time())
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET last_contacted = ? WHERE public_key = ?",
                (ts, public_key.lower()),
            ):
                pass

    @staticmethod
    async def touch_last_seen(public_key: str, timestamp: int) -> None:
        """Monotonically bump last_seen for a contact from an RF observation.

        Never moves last_seen backwards; a no-op if the contact row does not
        exist. Use this from packet-ingest paths that have attributed a packet
        to a specific contact pubkey (advert, incoming DM, decrypted PATH, etc.).
        """
        async with db.tx() as conn:
            async with conn.execute(
                """
                UPDATE contacts
                SET last_seen = CASE
                    WHEN last_seen IS NULL THEN ?
                    WHEN ? > last_seen THEN ?
                    ELSE last_seen
                END
                WHERE public_key = ?
                """,
                (timestamp, timestamp, timestamp, public_key.lower()),
            ):
                pass

    @staticmethod
    async def update_last_read_at(public_key: str, timestamp: int | None = None) -> bool:
        """Update the last_read_at timestamp for a contact.

        Returns True if a row was updated, False if contact not found.
        """
        ts = timestamp if timestamp is not None else int(time.time())
        async with db.tx() as conn:
            async with conn.execute(
                "UPDATE contacts SET last_read_at = ? WHERE public_key = ?",
                (ts, public_key.lower()),
            ) as cursor:
                rowcount = cursor.rowcount
        return rowcount > 0

    @staticmethod
    async def promote_prefix_placeholders(full_key: str) -> list[str]:
        """Promote prefix-only placeholder contacts to a resolved full key.

        Returns the placeholder public keys that were merged into the full key.
        All operations for the promotion happen inside one ``db.tx()`` so
        partial promotions never leak to readers between steps.
        """

        async def migrate_child_rows(conn, old_key: str, new_key: str) -> None:
            async with conn.execute(
                """
                INSERT INTO contact_name_history (public_key, name, first_seen, last_seen)
                SELECT ?, name, first_seen, last_seen
                FROM contact_name_history
                WHERE public_key = ?
                ON CONFLICT(public_key, name) DO UPDATE SET
                    first_seen = MIN(contact_name_history.first_seen, excluded.first_seen),
                    last_seen = MAX(contact_name_history.last_seen, excluded.last_seen)
                """,
                (new_key, old_key),
            ):
                pass
            async with conn.execute(
                """
                INSERT INTO contact_advert_paths
                    (public_key, path_hex, path_len, first_seen, last_seen, heard_count)
                SELECT ?, path_hex, path_len, first_seen, last_seen, heard_count
                FROM contact_advert_paths
                WHERE public_key = ?
                ON CONFLICT(public_key, path_hex, path_len) DO UPDATE SET
                    first_seen = MIN(contact_advert_paths.first_seen, excluded.first_seen),
                    last_seen = MAX(contact_advert_paths.last_seen, excluded.last_seen),
                    heard_count = contact_advert_paths.heard_count + excluded.heard_count
                """,
                (new_key, old_key),
            ):
                pass
            async with conn.execute(
                "DELETE FROM contact_name_history WHERE public_key = ?",
                (old_key,),
            ):
                pass
            async with conn.execute(
                "DELETE FROM contact_advert_paths WHERE public_key = ?",
                (old_key,),
            ):
                pass

        normalized_full_key = full_key.lower()
        promoted_keys: list[str] = []
        async with db.tx() as conn:
            async with conn.execute(
                """
                SELECT public_key, last_seen, last_contacted, first_seen, last_read_at
                FROM contacts
                WHERE length(public_key) < 64
                  AND ? LIKE public_key || '%'
                ORDER BY length(public_key) DESC, public_key
                """,
                (normalized_full_key,),
            ) as cursor:
                rows = list(await cursor.fetchall())
            if not rows:
                return []

            for row in rows:
                old_key = row["public_key"]
                if old_key == normalized_full_key:
                    continue

                async with conn.execute(
                    """
                    SELECT COUNT(*) AS match_count
                    FROM contacts
                    WHERE length(public_key) = 64
                      AND public_key LIKE ? || '%'
                    """,
                    (old_key,),
                ) as match_cursor:
                    match_row = await match_cursor.fetchone()
                match_count = match_row["match_count"] if match_row is not None else 0
                if match_count != 1:
                    logger.warning(
                        "Skipping prefix promotion for %s: %d full-key contacts match (expected 1)",
                        old_key,
                        match_count,
                    )
                    continue

                await migrate_child_rows(conn, old_key, normalized_full_key)

                # Merge timestamp metadata from the old prefix contact into the
                # full-key contact (which all callers guarantee already exists),
                # then delete the prefix placeholder.
                async with conn.execute(
                    """
                    UPDATE contacts
                    SET last_seen = CASE
                            WHEN contacts.last_seen IS NULL THEN ?
                            WHEN ? IS NULL THEN contacts.last_seen
                            WHEN ? > contacts.last_seen THEN ?
                            ELSE contacts.last_seen
                        END,
                        last_contacted = CASE
                            WHEN contacts.last_contacted IS NULL THEN ?
                            WHEN ? IS NULL THEN contacts.last_contacted
                            WHEN ? > contacts.last_contacted THEN ?
                            ELSE contacts.last_contacted
                        END,
                        first_seen = CASE
                            WHEN contacts.first_seen IS NULL THEN ?
                            WHEN ? IS NULL THEN contacts.first_seen
                            WHEN ? < contacts.first_seen THEN ?
                            ELSE contacts.first_seen
                        END,
                        last_read_at = CASE
                            WHEN contacts.last_read_at IS NULL THEN ?
                            WHEN ? IS NULL THEN contacts.last_read_at
                            WHEN ? > contacts.last_read_at THEN ?
                            ELSE contacts.last_read_at
                        END
                    WHERE public_key = ?
                    """,
                    (
                        row["last_seen"],
                        row["last_seen"],
                        row["last_seen"],
                        row["last_seen"],
                        row["last_contacted"],
                        row["last_contacted"],
                        row["last_contacted"],
                        row["last_contacted"],
                        row["first_seen"],
                        row["first_seen"],
                        row["first_seen"],
                        row["first_seen"],
                        row["last_read_at"],
                        row["last_read_at"],
                        row["last_read_at"],
                        row["last_read_at"],
                        normalized_full_key,
                    ),
                ):
                    pass
                async with conn.execute("DELETE FROM contacts WHERE public_key = ?", (old_key,)):
                    pass

                promoted_keys.append(old_key)

        return promoted_keys

    @staticmethod
    async def mark_all_read(timestamp: int) -> None:
        """Mark all contacts as read at the given timestamp."""
        async with db.tx() as conn:
            async with conn.execute("UPDATE contacts SET last_read_at = ?", (timestamp,)):
                pass

    @staticmethod
    async def get_by_pubkey_first_byte(hex_byte: str) -> list[Contact]:
        """Get contacts whose public key starts with the given hex byte (2 chars)."""
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contacts WHERE substr(public_key, 1, 2) = ?",
                (hex_byte.lower(),),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactRepository._row_to_contact(row) for row in rows]


class ContactAdvertPathRepository:
    """Repository for recent unique advertisement paths per contact."""

    @staticmethod
    def _row_to_path(row) -> ContactAdvertPath:
        path = row["path_hex"] or ""
        path_len = row["path_len"]
        next_hop = first_hop_hex(path, path_len)
        return ContactAdvertPath(
            path=path,
            path_len=path_len,
            next_hop=next_hop,
            first_seen=row["first_seen"],
            last_seen=row["last_seen"],
            heard_count=row["heard_count"],
        )

    @staticmethod
    async def record_observation(
        public_key: str,
        path_hex: str,
        timestamp: int,
        max_paths: int = 10,
        hop_count: int | None = None,
    ) -> None:
        """
        Upsert a unique advert path observation for a contact and prune to N most recent.
        """
        if max_paths < 1:
            max_paths = 1

        normalized_key = public_key.lower()
        normalized_path = path_hex.lower()
        path_len = hop_count if hop_count is not None else len(normalized_path) // 2

        async with db.tx() as conn:
            async with conn.execute(
                """
                INSERT INTO contact_advert_paths
                    (public_key, path_hex, path_len, first_seen, last_seen, heard_count)
                VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(public_key, path_hex, path_len) DO UPDATE SET
                    last_seen = MAX(contact_advert_paths.last_seen, excluded.last_seen),
                    heard_count = contact_advert_paths.heard_count + 1
                """,
                (normalized_key, normalized_path, path_len, timestamp, timestamp),
            ):
                pass

            # Keep only the N most recent unique paths per contact.
            async with conn.execute(
                """
                DELETE FROM contact_advert_paths
                WHERE public_key = ?
                  AND id NOT IN (
                      SELECT id
                      FROM contact_advert_paths
                      WHERE public_key = ?
                      ORDER BY last_seen DESC, heard_count DESC, path_len ASC, path_hex ASC
                      LIMIT ?
                  )
                """,
                (normalized_key, normalized_key, max_paths),
            ):
                pass

    @staticmethod
    async def get_recent_for_contact(public_key: str, limit: int = 10) -> list[ContactAdvertPath]:
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT path_hex, path_len, first_seen, last_seen, heard_count
                FROM contact_advert_paths
                WHERE public_key = ?
                ORDER BY last_seen DESC, heard_count DESC, path_len ASC, path_hex ASC
                LIMIT ?
                """,
                (public_key.lower(), limit),
            ) as cursor:
                rows = await cursor.fetchall()
        return [ContactAdvertPathRepository._row_to_path(row) for row in rows]

    @staticmethod
    async def get_recent_for_all_contacts(
        limit_per_contact: int = 10,
    ) -> list[ContactAdvertPathSummary]:
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT public_key, path_hex, path_len, first_seen, last_seen, heard_count
                FROM (
                    SELECT *,
                           ROW_NUMBER() OVER (
                               PARTITION BY public_key
                               ORDER BY last_seen DESC, heard_count DESC, path_len ASC, path_hex ASC
                           ) AS rn
                    FROM contact_advert_paths
                )
                WHERE rn <= ?
                ORDER BY public_key ASC, last_seen DESC, heard_count DESC, path_len ASC, path_hex ASC
                """,
                (limit_per_contact,),
            ) as cursor:
                rows = await cursor.fetchall()

        grouped: dict[str, list[ContactAdvertPath]] = {}
        for row in rows:
            key = row["public_key"]
            paths = grouped.get(key)
            if paths is None:
                paths = []
                grouped[key] = paths
            paths.append(ContactAdvertPathRepository._row_to_path(row))

        return [
            ContactAdvertPathSummary(public_key=key, paths=paths) for key, paths in grouped.items()
        ]


class ContactClockDriftRepository:
    """Per-contact clock drift, measured from the timestamp inside each advert.

    Rows are hourly buckets keyed ``(public_key, bucket_start)``, and a bucket
    keeps its **largest** drift. Propagation delay only ever biases a reading
    negative -- the advert was built before it was heard -- so the maximum
    within an hour is the arrival that suffered least of it. ``sample_count``
    still counts every arrival, so a flood heard over six paths is visible as
    six samples in one bucket rather than six rows.

    See ``app/clock_drift.py`` for the sign convention and the biases.
    """

    @staticmethod
    async def record(
        public_key: str,
        *,
        advert_timestamp: int,
        observed_at: int,
        path_len: int = 0,
    ) -> None:
        """Fold one advert arrival into its bucket.

        Must be called after the contact upsert: the table has a foreign key
        onto ``contacts`` and enforcement is on for application queries.
        """
        drift = advert_timestamp - observed_at
        slot = bucket_start(observed_at)

        async with db.tx() as conn:
            await conn.execute(
                """
                INSERT INTO contact_clock_drift
                    (public_key, bucket_start, drift_seconds, observed_at,
                     advert_timestamp, path_len, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(public_key, bucket_start) DO UPDATE SET
                    sample_count = contact_clock_drift.sample_count + 1,
                    drift_seconds = MAX(contact_clock_drift.drift_seconds, excluded.drift_seconds),
                    observed_at = CASE
                        WHEN excluded.drift_seconds > contact_clock_drift.drift_seconds
                        THEN excluded.observed_at ELSE contact_clock_drift.observed_at END,
                    advert_timestamp = CASE
                        WHEN excluded.drift_seconds > contact_clock_drift.drift_seconds
                        THEN excluded.advert_timestamp
                        ELSE contact_clock_drift.advert_timestamp END,
                    path_len = CASE
                        WHEN excluded.drift_seconds > contact_clock_drift.drift_seconds
                        THEN excluded.path_len ELSE contact_clock_drift.path_len END
                """,
                (public_key.lower(), slot, drift, observed_at, advert_timestamp, path_len),
            )

    @staticmethod
    async def compact(
        full_resolution_seconds: int = DRIFT_FULL_RESOLUTION_SECONDS,
        *,
        now: int | None = None,
    ) -> int:
        """Fold buckets older than the horizon down to one per day.

        Nothing is deleted, because a clock's history is the interesting part of
        it. Old hours are merged instead: the day keeps its largest drift (same
        propagation-delay argument as the hourly rows) and the sum of every
        arrival counted across them, so totals stay honest.

        Returns the number of rows removed by merging -- zero once a stretch of
        history has already been folded, which makes this safe to call on a
        timer forever.

        No schema marker distinguishes a daily row from an hourly one, and none
        is needed: a day-aligned ``bucket_start`` *is* the marker, and every
        read path already re-buckets with integer division, so a coarser input
        needs no special case.
        """
        now = int(time.time()) if now is None else now
        cutoff = day_start(now - full_resolution_seconds)

        async with db.tx() as conn:
            # ``observed_at``/``advert_timestamp``/``path_len`` are bare columns
            # beside MAX(), so SQLite takes them from the row that produced the
            # maximum -- the day keeps a real reading, not a mix of several.
            async with conn.execute(
                """
                SELECT public_key,
                       (bucket_start / ?) * ? AS day,
                       MAX(drift_seconds) AS drift_seconds,
                       observed_at,
                       advert_timestamp,
                       path_len,
                       SUM(sample_count) AS sample_count,
                       COUNT(*) AS rows_in_day,
                       MIN(bucket_start) AS earliest
                FROM contact_clock_drift
                WHERE bucket_start < ?
                GROUP BY public_key, day
                """,
                (DAY_SECONDS, DAY_SECONDS, cutoff),
            ) as cursor:
                groups = await cursor.fetchall()

            # A day already reduced to its single midnight row is done. Skip it
            # rather than rewriting it, so a steady-state call costs one query.
            pending = [
                row for row in groups if row["rows_in_day"] > 1 or row["earliest"] != row["day"]
            ]
            if not pending:
                return 0

            merged_away = sum(row["rows_in_day"] for row in pending) - len(pending)

            for row in pending:
                await conn.execute(
                    "DELETE FROM contact_clock_drift WHERE public_key = ? "
                    "AND bucket_start >= ? AND bucket_start < ?",
                    (row["public_key"], row["day"], row["day"] + DAY_SECONDS),
                )
            await conn.executemany(
                """
                INSERT INTO contact_clock_drift
                    (public_key, bucket_start, drift_seconds, observed_at,
                     advert_timestamp, path_len, sample_count)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["public_key"],
                        row["day"],
                        row["drift_seconds"],
                        row["observed_at"],
                        row["advert_timestamp"],
                        row["path_len"],
                        row["sample_count"],
                    )
                    for row in pending
                ],
            )
            return merged_away

    @staticmethod
    async def get_for_contact(
        public_key: str,
        *,
        window_seconds: int = CONTACT_DRIFT_WINDOW_SECONDS,
        now: int | None = None,
    ) -> ContactClockDrift | None:
        """Drift summary plus a chart-sized series, or ``None`` if never measured.

        The series is re-bucketed in SQL to keep it near ``TARGET_CHART_BUCKETS``
        points, taking the max drift per bucket for the same
        propagation-delay reason the hourly rows do.
        """
        key = public_key.lower()
        now = int(time.time()) if now is None else now
        cutoff = now - window_seconds
        bucket = bucket_seconds_for_span(window_seconds, minimum=DRIFT_BUCKET_SECONDS)

        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT COUNT(*) AS buckets,
                       SUM(sample_count) AS samples,
                       SUM(CASE WHEN path_len = 0 THEN 1 ELSE 0 END) AS direct_buckets,
                       MIN(drift_seconds) AS min_drift,
                       MAX(drift_seconds) AS max_drift,
                       AVG(drift_seconds) AS mean_drift
                FROM contact_clock_drift
                WHERE public_key = ? AND bucket_start >= ?
                """,
                (key, cutoff),
            ) as cursor:
                agg = await cursor.fetchone()

            if agg is None or not agg["buckets"]:
                return None

            async with conn.execute(
                """
                SELECT drift_seconds, observed_at, advert_timestamp, path_len
                FROM contact_clock_drift
                WHERE public_key = ?
                ORDER BY bucket_start DESC
                LIMIT 1
                """,
                (key,),
            ) as cursor:
                latest = await cursor.fetchone()

            async with conn.execute(
                "SELECT MIN(bucket_start) AS oldest FROM contact_clock_drift WHERE public_key = ?",
                (key,),
            ) as cursor:
                oldest_row = await cursor.fetchone()

            # ``path_len`` is a bare column next to MAX(): SQLite guarantees it
            # comes from the same input row the maximum did, so each rolled-up
            # point reports the hop count of the arrival it actually kept.
            async with conn.execute(
                """
                SELECT (bucket_start / ?) * ? AS slot,
                       MAX(drift_seconds) AS drift_seconds,
                       path_len,
                       SUM(sample_count) AS sample_count
                FROM contact_clock_drift
                WHERE public_key = ? AND bucket_start >= ?
                GROUP BY slot
                ORDER BY slot
                """,
                (bucket, bucket, key, cutoff),
            ) as cursor:
                series_rows = await cursor.fetchall()

        if latest is None:
            return None

        samples = [
            ClockDriftSample(
                bucket_start=row["slot"],
                drift_seconds=row["drift_seconds"],
                sample_count=row["sample_count"] or 0,
                path_len=row["path_len"] or 0,
            )
            for row in series_rows
        ]

        # Step-aware: a fit across a clock that keeps being reset reports a badly
        # drifting node as steady, and the pane's headline reads off this number.
        rate, steps, rate_since_step = drift_trend(
            [(s.bucket_start, s.drift_seconds) for s in samples]
        )
        latest_drift = latest["drift_seconds"]
        latest_advert_timestamp = latest["advert_timestamp"]

        return ContactClockDrift(
            latest_drift_seconds=latest_drift,
            latest_observed_at=latest["observed_at"],
            latest_advert_timestamp=latest_advert_timestamp,
            latest_path_len=latest["path_len"],
            severity=classify_drift(latest_drift),
            clock_unset=is_unset_clock(latest_advert_timestamp),
            window_seconds=window_seconds,
            first_observed_at=(oldest_row["oldest"] if oldest_row else None) or cutoff,
            sample_count=agg["samples"] or 0,
            bucket_count=agg["buckets"] or 0,
            direct_sample_count=agg["direct_buckets"] or 0,
            min_drift_seconds=agg["min_drift"] or 0,
            max_drift_seconds=agg["max_drift"] or 0,
            mean_drift_seconds=round(agg["mean_drift"] or 0.0, 1),
            drift_rate_seconds_per_day=rate,
            rate_since_last_step=rate_since_step,
            step_count=len(steps),
            bucket_seconds=bucket,
            samples=samples,
        )

    @staticmethod
    async def get_detail(
        public_key: str,
        *,
        window_seconds: int | None,
        now: int | None = None,
    ) -> NodeClockDriftStats | None:
        """The node stats page's drift section, or ``None`` if never measured.

        Everything the info pane's summary carries, plus what only earns its
        place on a page of its own: a series with the spread inside each
        bucket, the discontinuities where the clock was *set* rather than
        drifting, this node's own distribution, and a breakdown by hop count
        that makes the propagation bias visible instead of asserted.

        ``window_seconds`` of ``None`` means the whole retained history.
        """
        key = public_key.lower()
        now = int(time.time()) if now is None else now
        cutoff = None if window_seconds is None else now - window_seconds

        where = "WHERE public_key = ?"
        params: tuple = (key,)
        if cutoff is not None:
            where += " AND bucket_start >= ?"
            params = (key, cutoff)

        async with db.readonly() as conn:
            async with conn.execute(
                f"""
                SELECT COUNT(*) AS readings,
                       SUM(sample_count) AS samples,
                       SUM(CASE WHEN path_len = 0 THEN 1 ELSE 0 END) AS direct_readings,
                       MIN(drift_seconds) AS min_drift,
                       MAX(drift_seconds) AS max_drift,
                       AVG(drift_seconds) AS mean_drift,
                       MIN(bucket_start) AS oldest
                FROM contact_clock_drift
                {where}
                """,
                params,
            ) as cursor:
                agg = await cursor.fetchone()

            if agg is None or not agg["readings"]:
                return None

            async with conn.execute(
                """
                SELECT drift_seconds, observed_at, advert_timestamp, path_len
                FROM contact_clock_drift
                WHERE public_key = ?
                ORDER BY bucket_start DESC
                LIMIT 1
                """,
                (key,),
            ) as cursor:
                latest = await cursor.fetchone()

            # Every stored reading in the window. Bounded by the table's own
            # shape -- at most one row per node per hour, folded to one per day
            # past the compaction horizon -- so even ``all`` stays small enough
            # to fit a trend and walk for steps in Python.
            async with conn.execute(
                f"""
                SELECT bucket_start, drift_seconds, sample_count, path_len
                FROM contact_clock_drift
                {where}
                ORDER BY bucket_start
                """,
                params,
            ) as cursor:
                readings = await cursor.fetchall()

        if latest is None:
            return None

        span = (now - agg["oldest"]) if window_seconds is None else window_seconds
        bucket = bucket_seconds_for_span(
            max(span, DRIFT_BUCKET_SECONDS), minimum=DRIFT_BUCKET_SECONDS
        )

        # Bucketed in Python rather than SQL: the same rows also feed the trend,
        # the steps and the hop breakdown, and fetching them once beats four
        # passes over the table for four shapes of the same data.
        bands: dict[int, dict] = {}
        hops: dict[int, list[int]] = {}
        for row in readings:
            slot = (row["bucket_start"] // bucket) * bucket
            drift = row["drift_seconds"]
            band = bands.get(slot)
            if band is None:
                bands[slot] = {
                    "bucket_start": slot,
                    "drift_seconds": drift,
                    "min_drift_seconds": drift,
                    "max_drift_seconds": drift,
                    "sample_count": row["sample_count"] or 0,
                    "reading_count": 1,
                    "direct_reading_count": 1 if row["path_len"] == 0 else 0,
                }
            else:
                band["drift_seconds"] = max(band["drift_seconds"], drift)
                band["min_drift_seconds"] = min(band["min_drift_seconds"], drift)
                band["max_drift_seconds"] = max(band["max_drift_seconds"], drift)
                band["sample_count"] += row["sample_count"] or 0
                band["reading_count"] += 1
                if row["path_len"] == 0:
                    band["direct_reading_count"] += 1
            hops.setdefault(row["path_len"], []).append(drift)

        points = [(row["bucket_start"], row["drift_seconds"]) for row in readings]
        rate, steps, rate_since_step = drift_trend(points)

        histogram = [0] * len(histogram_labels())
        for _, drift in points:
            histogram[histogram_bin(drift)] += 1

        latest_drift = latest["drift_seconds"]
        latest_advert_timestamp = latest["advert_timestamp"]

        return NodeClockDriftStats(
            latest_drift_seconds=latest_drift,
            latest_observed_at=latest["observed_at"],
            latest_advert_timestamp=latest_advert_timestamp,
            latest_path_len=latest["path_len"],
            severity=classify_drift(latest_drift),
            clock_unset=is_unset_clock(latest_advert_timestamp),
            window_seconds=span,
            first_observed_at=agg["oldest"],
            sample_count=agg["samples"] or 0,
            bucket_count=agg["readings"] or 0,
            direct_sample_count=agg["direct_readings"] or 0,
            min_drift_seconds=agg["min_drift"] or 0,
            max_drift_seconds=agg["max_drift"] or 0,
            mean_drift_seconds=round(agg["mean_drift"] or 0.0, 1),
            drift_rate_seconds_per_day=rate,
            rate_since_last_step=rate_since_step,
            step_count=len(steps),
            bucket_seconds=bucket,
            series=[
                ClockDriftBand(**band)
                for band in sorted(bands.values(), key=lambda b: b["bucket_start"])
            ],
            steps=[ClockDriftStep(**step) for step in steps[:MAX_CLOCK_STEPS]],
            histogram=[
                ClockDriftHistogramBin(label=label, count=count)
                for label, count in zip(histogram_labels(), histogram, strict=True)
            ],
            hop_breakdown=[
                ClockDriftHopBucket(
                    path_len=path_len,
                    reading_count=len(values),
                    mean_drift_seconds=round(sum(values) / len(values), 1),
                    min_drift_seconds=min(values),
                    max_drift_seconds=max(values),
                )
                for path_len, values in sorted(hops.items())
            ],
        )


class ContactNameHistoryRepository:
    """Repository for contact name change history."""

    @staticmethod
    async def record_name(public_key: str, name: str, timestamp: int) -> None:
        """Record a name observation. Upserts: updates last_seen if name already known."""
        async with db.tx() as conn:
            async with conn.execute(
                """
                INSERT INTO contact_name_history (public_key, name, first_seen, last_seen)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(public_key, name) DO UPDATE SET
                    last_seen = MAX(contact_name_history.last_seen, excluded.last_seen)
                """,
                (public_key.lower(), name, timestamp, timestamp),
            ):
                pass

    @staticmethod
    async def get_history(public_key: str) -> list[ContactNameHistory]:
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT name, first_seen, last_seen
                FROM contact_name_history
                WHERE public_key = ?
                ORDER BY last_seen DESC
                """,
                (public_key.lower(),),
            ) as cursor:
                rows = await cursor.fetchall()
        return [
            ContactNameHistory(
                name=row["name"], first_seen=row["first_seen"], last_seen=row["last_seen"]
            )
            for row in rows
        ]
