"""Who runs a node: saved firmware ``owner.info``, operator notes, and outreach.

MeshCore adverts carry no owner field. The nearest thing is the free-text
``owner.info`` a repeater or room server hands out on a guest-accessible binary
request, which used to be fetched on demand and thrown away. This keeps it,
alongside notes the operator types in themselves (a callsign spotted on a
forum, "runs the club repeater", "told them 3 May"), and derives from both a
short list of ways to reach the owner.

The outreach list pairs that with the one problem the server can already prove
without anyone's help: a clock that is wrong (see ``app/clock_drift.py``).
"""

from __future__ import annotations

import re
import time
from typing import Any

from app.clock_drift import MINOR_SECONDS, classify_drift, is_unset_clock, median
from app.database import db
from app.models import (
    ContactOwnerHint,
    ContactOwnerInfo,
    OwnerOutreachItem,
    OwnerOutreachResponse,
)

# The background sweep refreshes each repeater/room this often by default.
DEFAULT_OWNER_INFO_REFRESH_DAYS = 7

NOTES_MAX_LENGTH = 2000

# Outreach: only readings this recent say anything about the clock *now*.
OUTREACH_LOOKBACK_SECONDS = 7 * 86400
# Beyond MINOR_SECONDS is the "major" band: five minutes out is past what a
# reboot or propagation delay explains, and worth a message to the owner.
OUTREACH_THRESHOLD_SECONDS = MINOR_SECONDS
# The newest few hourly readings must all agree, so one odd advert (a relayed
# old packet, a clock mid-resync) never puts a node on the list.
OUTREACH_RECENT_READINGS = 3
OUTREACH_MIN_READINGS = 2

# Amateur callsigns (ITU shape): a one- or two-character prefix, one digit,
# a one-to-four letter suffix. Upper case only -- in lower case this matches
# far too many ordinary words and node names.
_CALLSIGN_RE = re.compile(r"\b(?:[A-Z]{1,2}|[A-Z][0-9]|[0-9][A-Z])[0-9][A-Z]{1,4}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
_URL_RE = re.compile(r"\bhttps?://[^\s|]+", re.IGNORECASE)
_LABELLED_HANDLE_RE = re.compile(
    r"\b(discord|telegram|tg|signal|matrix|mastodon|insta(?:gram)?|twitter|x)\s*[:=]\s*(\S+)",
    re.IGNORECASE,
)
_AT_HANDLE_RE = re.compile(r"(?<![\w.])@[A-Za-z0-9_.]{2,32}\b")


def extract_contact_hints(
    *,
    name: str | None = None,
    owner_info: str | None = None,
    notes: str | None = None,
) -> list[ContactOwnerHint]:
    """Ways to reach an owner, found in the node's name, owner.info and notes.

    Pure pattern matching, deliberately conservative: a missed hint costs a
    glance at the raw text sitting right beside it, while a false one sends
    someone looking for a callsign that does not exist.
    """
    hints: list[ContactOwnerHint] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str, source: str) -> None:
        value = value.strip().rstrip(".,;)")
        key = (kind, value.lower())
        if not value or key in seen:
            return
        seen.add(key)
        hints.append(ContactOwnerHint(kind=kind, value=value, source=source))  # type: ignore[arg-type]

    for source, text in (("owner_info", owner_info), ("notes", notes), ("name", name)):
        if not text:
            continue
        # Firmware turns "|" into a line break, so treat it as whitespace.
        text = text.replace("|", " ")
        emails = _EMAIL_RE.findall(text)
        for email in emails:
            add("email", email, source)
        for url in _URL_RE.findall(text):
            add("url", url, source)
        for label, handle in _LABELLED_HANDLE_RE.findall(text):
            add("handle", f"{label.lower()}: {handle}", source)
        without_emails = _EMAIL_RE.sub(" ", _URL_RE.sub(" ", text))
        for handle in _AT_HANDLE_RE.findall(without_emails):
            add("handle", handle, source)
        for callsign in _CALLSIGN_RE.findall(without_emails):
            add("callsign", callsign, source)
    return hints


def _row_to_info(public_key: str, row: Any | None, *, name: str | None) -> ContactOwnerInfo:
    if row is None:
        return ContactOwnerInfo(public_key=public_key, hints=extract_contact_hints(name=name))
    return ContactOwnerInfo(
        public_key=public_key,
        firmware_owner_info=row["firmware_owner_info"],
        firmware_version=row["firmware_version"],
        fetched_at=row["fetched_at"],
        attempted_at=row["attempted_at"],
        attempt_status=row["attempt_status"],
        notes=row["notes"] or "",
        notes_updated_at=row["notes_updated_at"],
        notified_at=row["notified_at"],
        hints=extract_contact_hints(
            name=name, owner_info=row["firmware_owner_info"], notes=row["notes"]
        ),
    )


class ContactOwnerRepository:
    """One row per node that has ever been asked about or annotated.

    No foreign key onto ``contacts``: notes are typed by hand and must survive
    a contact being deleted and heard again (see migration 091).
    """

    @staticmethod
    async def get(public_key: str, *, name: str | None = None) -> ContactOwnerInfo:
        """Owner info for a node; an empty record when nothing is known yet."""
        key = public_key.lower()
        async with db.readonly() as conn:
            async with conn.execute(
                "SELECT * FROM contact_owner WHERE public_key = ?", (key,)
            ) as cursor:
                row = await cursor.fetchone()
        return _row_to_info(key, row, name=name)

    @staticmethod
    async def record_fetch(
        public_key: str,
        *,
        status: str,
        owner_info: str | None = None,
        firmware_version: str | None = None,
        now: int | None = None,
    ) -> None:
        """Record one owner-info attempt.

        Only an answered request (``status == "ok"``) overwrites the saved
        firmware fields -- an empty note is a real answer and clears them. A
        miss only moves ``attempted_at``, so a node that is out of range this
        week keeps what it said last week.
        """
        now = int(time.time()) if now is None else now
        key = public_key.lower()
        async with db.tx() as conn:
            if status == "ok":
                await conn.execute(
                    """
                    INSERT INTO contact_owner
                        (public_key, firmware_owner_info, firmware_version,
                         fetched_at, attempted_at, attempt_status)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(public_key) DO UPDATE SET
                        firmware_owner_info = excluded.firmware_owner_info,
                        firmware_version = COALESCE(
                            excluded.firmware_version, contact_owner.firmware_version),
                        fetched_at = excluded.fetched_at,
                        attempted_at = excluded.attempted_at,
                        attempt_status = excluded.attempt_status
                    """,
                    (key, owner_info or None, firmware_version, now, now, status),
                )
            else:
                await conn.execute(
                    """
                    INSERT INTO contact_owner (public_key, attempted_at, attempt_status)
                    VALUES (?, ?, ?)
                    ON CONFLICT(public_key) DO UPDATE SET
                        attempted_at = excluded.attempted_at,
                        attempt_status = excluded.attempt_status
                    """,
                    (key, now, status),
                )

    @staticmethod
    async def update(
        public_key: str,
        *,
        notes: str | None = None,
        notified: bool | None = None,
        now: int | None = None,
    ) -> None:
        """Apply an operator edit. ``None`` leaves a field alone."""
        now = int(time.time()) if now is None else now
        key = public_key.lower()
        async with db.tx() as conn:
            await conn.execute(
                "INSERT OR IGNORE INTO contact_owner (public_key) VALUES (?)", (key,)
            )
            if notes is not None:
                await conn.execute(
                    "UPDATE contact_owner SET notes = ?, notes_updated_at = ? WHERE public_key = ?",
                    (notes.strip()[:NOTES_MAX_LENGTH], now, key),
                )
            if notified is not None:
                await conn.execute(
                    "UPDATE contact_owner SET notified_at = ? WHERE public_key = ?",
                    (now if notified else None, key),
                )

    @staticmethod
    async def next_sweep_target(
        *, stale_before: int, heard_since: int
    ) -> tuple[str, str | None] | None:
        """The repeater or room most overdue for an owner-info refresh.

        Only nodes heard since ``heard_since`` qualify: one that has gone quiet
        cannot answer, and asking it anyway spends a flood login on nobody.
        Never-attempted nodes go first, then the longest-waiting.

        Rooms qualify only with a stored credential. Logging in is what makes a
        room server push its posts, so a guest login to a room the operator
        never joined would quietly fill a new conversation with its history.
        Returns ``(public_key, credential)``; the credential is ``None`` for a
        repeater (guest login) and three-state for a room, like room polling.
        """
        async with db.readonly() as conn:
            async with conn.execute(
                """
                SELECT c.public_key, r.credential
                FROM contacts c
                LEFT JOIN contact_owner o ON o.public_key = c.public_key
                LEFT JOIN room_poll_subscriptions r ON r.room_key = c.public_key
                WHERE (c.type = 2 OR (c.type = 3 AND r.credential IS NOT NULL))
                  AND COALESCE(c.last_seen, 0) >= ?
                  AND (o.attempted_at IS NULL OR o.attempted_at < ?)
                ORDER BY o.attempted_at IS NOT NULL, o.attempted_at, c.last_seen DESC
                LIMIT 1
                """,
                (heard_since, stale_before),
            ) as cursor:
                row = await cursor.fetchone()
        return (row["public_key"], row["credential"]) if row else None

    @staticmethod
    async def list_outreach(*, now: int | None = None) -> OwnerOutreachResponse:
        """Nodes whose clock has been clearly wrong for their last few readings.

        Also reports the signed median across every node measured: independent
        clocks do not drift together, so a median beyond the threshold means the
        list is mostly describing *this server's* clock, and the page says so
        rather than suggesting a message to every owner on the mesh.
        """
        now = int(time.time()) if now is None else now
        cutoff = now - OUTREACH_LOOKBACK_SECONDS

        async with db.readonly() as conn:
            async with conn.execute(
                """
                WITH ranked AS (
                    SELECT public_key, drift_seconds, observed_at, advert_timestamp,
                           ROW_NUMBER() OVER (
                               PARTITION BY public_key ORDER BY bucket_start DESC
                           ) AS rn
                    FROM contact_clock_drift
                    WHERE bucket_start >= ?
                )
                SELECT r.public_key, r.drift_seconds, r.observed_at, r.advert_timestamp,
                       r.rn, c.name, c.type
                FROM ranked r
                JOIN contacts c ON c.public_key = r.public_key
                WHERE r.rn <= ?
                ORDER BY r.public_key, r.rn
                """,
                (cutoff, OUTREACH_RECENT_READINGS),
            ) as cursor:
                rows = await cursor.fetchall()

        by_node: dict[str, list[Any]] = {}
        for row in rows:
            by_node.setdefault(row["public_key"], []).append(row)

        newest_set_clocks = [
            float(readings[0]["drift_seconds"])
            for readings in by_node.values()
            if not is_unset_clock(readings[0]["advert_timestamp"])
        ]
        median_drift = median(newest_set_clocks) if newest_set_clocks else None
        server_suspect = (
            median_drift is not None
            and len(newest_set_clocks) >= OUTREACH_MIN_READINGS
            and abs(median_drift) > OUTREACH_THRESHOLD_SECONDS
        )

        flagged: list[tuple[list[Any], str]] = []
        for readings in by_node.values():
            if len(readings) < OUTREACH_MIN_READINGS:
                continue
            if not all(
                is_unset_clock(r["advert_timestamp"])
                or abs(r["drift_seconds"]) > OUTREACH_THRESHOLD_SECONDS
                for r in readings
            ):
                continue
            issue = (
                "unset_clock" if is_unset_clock(readings[0]["advert_timestamp"]) else "clock_drift"
            )
            flagged.append((readings, issue))

        owners: dict[str, Any] = {}
        if flagged:
            keys = [readings[0]["public_key"] for readings, _ in flagged]
            placeholders = ",".join("?" for _ in keys)
            async with db.readonly() as conn:
                async with conn.execute(
                    f"SELECT * FROM contact_owner WHERE public_key IN ({placeholders})", keys
                ) as cursor:
                    owners = {row["public_key"]: row for row in await cursor.fetchall()}

        items: list[OwnerOutreachItem] = []
        for readings, issue in flagged:
            newest = readings[0]
            key = newest["public_key"]
            owner = _row_to_info(key, owners.get(key), name=newest["name"])
            items.append(
                OwnerOutreachItem(
                    public_key=key,
                    name=newest["name"],
                    type=newest["type"] or 0,
                    issue=issue,  # type: ignore[arg-type]
                    drift_seconds=newest["drift_seconds"],
                    severity=classify_drift(newest["drift_seconds"]),
                    readings=len(readings),
                    last_observed_at=newest["observed_at"],
                    recently_notified=owner.notified_at is not None and owner.notified_at >= cutoff,
                    owner=owner,
                )
            )

        # Owners still waiting to hear first, real drift before never-set
        # clocks (those are usually a missing RTC, a different conversation),
        # then the worst offset.
        items.sort(
            key=lambda item: (
                item.recently_notified,
                item.issue == "unset_clock",
                -abs(item.drift_seconds),
            )
        )

        return OwnerOutreachResponse(
            generated_at=now,
            lookback_seconds=OUTREACH_LOOKBACK_SECONDS,
            threshold_seconds=OUTREACH_THRESHOLD_SECONDS,
            min_readings=OUTREACH_MIN_READINGS,
            nodes_measured=len(by_node),
            median_drift_seconds=median_drift,
            server_clock_suspect=server_suspect,
            items=items,
        )
