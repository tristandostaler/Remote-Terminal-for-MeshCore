"""MeshCore Mailbox (store-and-forward) — RemoteTerm bot version.

DM-only commands (prefix configurable via MAILBOX_PREFIX, default "mbx"):

  mbx msg <key> <text>   One-shot store. <key> = pubkey prefix (>=8 hex) or full key
  mbx to <key|name>      Compose: set recipient by key, or by NAME if the node
                         has been heard before (resolved key echoed back for
                         confirmation — names are spoofable, check the key!)
  mbx add <text>         Append text to the pending draft (repeatable)
  mbx send [text]        Store the draft (+ optional final text)
  mbx inbox              Message counts
  mbx play [N]           Play message #1 (oldest); with N, jump to #N
  mbx next [N]           Play the message after the cursor; with N, jump to #N
  mbx del                Delete the message you just heard
  mbx clear              Delete all heard (kept) messages
  mbx accept             Accept the privacy notice (once, stored per key)
  mbx ?                  Quick one-message reference
  mbx help / mbx         Full command guide
  mbx debug <password>   Unlock debug once (per key), then `mbx debug` works
  mbx debug              Dump MAILBOX_* env (debug-gated)
  mbx test <N>           Size probe of exactly N bytes (debug-gated, raw)

Converted from a legacy `def bot(**kwargs)` fanout script:
- the bot()/_bot_inner() hook is one @bot.on_message() catch-all; blocking
  SQLite work remains in _bot_inner and is dispatched with asyncio.to_thread;
- the SQLite handle cache moved from globals()["_bot_globals"] to a module-level
  _cache (persists per load, resets on code edit + save);
- config still comes from MAILBOX_* env vars, unchanged — nothing to reconfigure;
- error details still go only to debug-unlocked senders; everyone else gets the
  generic notice, and the operator sees the error in Bots > Logs (ctx.log).

Runs on ALL messages. Normal Mailbox commands still execute only in DMs. If an
`mbx ...` command is sent on a channel, the bot sends a flood advert, waits
about 30 seconds, then DMs the sender to carry on there when the contact
resolves uniquely. Keep scope at "All channels" so passive directory learning works.

Replies are NEVER truncated and this bot never sizes them: command handlers
return one plain string and the async entrypoint hands it to ``ctx.reply_split``,
which owns delivery entirely. ``mbx test`` is the one exception: its size probe
must go out verbatim, so it is tagged ``_Raw``.

Body size: hard ceiling HARD_MAX_BODY (2000B) per stored message. MAILBOX_MAX_BODY
can lower it (0 = ceiling), never raise it. Retention: unread 7d, kept 30d.
Identity is KEY-ONLY (full 64-char sender_key). Stdlib only.
"""

import asyncio
import hmac
import os
import re
import sqlite3
import time

from remoteterm import bot

HARD_MAX_BODY = 2000

BOT_META = {
    "key": "mailbox",
    "name": "mailbox",
    "category": "Custom",
    "description": "Store-and-forward mailbox for offline nodes (DM 'mbx help')",
    "version": "4.4.0",
    "respond_to_dms": True,
    "settings_schema": [
        {
            "key": "db_path",
            "label": "MailBox database path",
            "type": "text",
            "default": "data/mailbox.db",
        },
        {
            "key": "prefix",
            "label": "The mailbox command prefix",
            "type": "text",
            "default": "mbx",
        },
        {
            "key": "max_body",
            "label": "Maximum allowed chars per inbox message",
            "type": "int",
            "default": 2000,
            "min": 100,
            "max": HARD_MAX_BODY,
        },
        {
            "key": "debug_password",
            "label": "The debug password",
            "type": "password",
            "default": "CHANGE-ME-s3cret",
        },
    ],
    "settings": {
        "db_path": "data/mailbox.db",
        "prefix": "mbx",
        "max_body": 2000,
        "debug_password": "CHANGE-ME-s3cret",
    },
}

MAX_PER_RECIPIENT = 20
MAX_PER_SENDER = 20
TTL_SECONDS = 7 * 24 * 3600
KEPT_TTL_SECONDS = 30 * 24 * 3600
PENDING_TTL = 600
DIRECTORY_TTL = 90 * 86400
MIN_PUBKEY_PREFIX = 8
NAME_TRUNC = 12
SCHEMA_VERSION = 4  # bump any time tables/columns change

# Cross-call cache for this load: the SQLite handle. Persists across handler
# calls; resets when the code is edited and saved.
_cache: dict = {}

# Keep strong references to delayed redirect tasks and avoid duplicate 30s
# timers when the same node sends several mbx commands on a channel.
_redirect_tasks: set[asyncio.Task] = set()
_redirect_pending: set[str] = set()


def _cfg(settings, key):
    """Setting value with the BOT_META default as fallback (blank == unset)."""
    default = BOT_META["settings"][key]
    value = settings.get(key, default)
    return value if value not in (None, "") else default


def _db_path(settings):
    return str(_cfg(settings, "db_path") or "").strip()


def _prefix(settings):
    return str(_cfg(settings, "prefix") or "").strip()


def _max_body(settings):
    return int(_cfg(settings, "max_body") or "")


def _debug_password(settings):
    return str(_cfg(settings, "debug_password") or "").strip()


def _privacy_notice(settings):
    prefix = _prefix(settings)
    return (
        f"Mailbox stores messages UNENCRYPTED, readable by the "
        f"operator. Send '{prefix} accept' once to agree."
    )


# ---------------------------------------------------------------- storage --


def _db(settings):
    cache_key = f"conn_v{SCHEMA_VERSION}"
    if cache_key not in _cache:
        for k in list(_cache):
            if k.startswith("conn_") and k != cache_key:
                try:
                    _cache[k].close()
                except Exception:
                    pass
                del _cache[k]
        os.makedirs(os.path.dirname(_db_path(settings)) or ".", exist_ok=True)
        conn = sqlite3.connect(_db_path(settings), timeout=5, check_same_thread=False)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL, recipient_is_key INTEGER NOT NULL,
            sender_key TEXT, sender_name TEXT, body TEXT NOT NULL,
            created_at INTEGER NOT NULL, played INTEGER NOT NULL DEFAULT 0)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS cursors (
            requester TEXT PRIMARY KEY, message_id INTEGER NOT NULL,
            updated_at INTEGER NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS consents (
            requester TEXT PRIMARY KEY, accepted_at INTEGER NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS debug_auth (
            requester TEXT PRIMARY KEY, authed_at INTEGER NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS pending (
            requester TEXT PRIMARY KEY, recipient TEXT NOT NULL,
            draft TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL)"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS directory (
            name TEXT NOT NULL, key TEXT NOT NULL,
            last_seen INTEGER NOT NULL,
            PRIMARY KEY (name, key))"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_recipient ON messages(recipient)")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(pending)")]
        if "draft" not in cols:
            conn.execute("ALTER TABLE pending ADD COLUMN draft TEXT NOT NULL DEFAULT ''")
        conn.commit()
        _cache[cache_key] = conn
    return _cache[cache_key]


def _purge_expired(conn):
    now = int(time.time())
    conn.execute("DELETE FROM messages WHERE played=0 AND created_at < ?", (now - TTL_SECONDS,))
    conn.execute(
        "DELETE FROM messages WHERE played=1 AND created_at < ?", (now - KEPT_TTL_SECONDS,)
    )
    conn.execute("DELETE FROM pending WHERE created_at < ?", (now - PENDING_TTL,))
    conn.execute("DELETE FROM directory WHERE last_seen < ?", (now - DIRECTORY_TTL,))
    conn.commit()


# -------------------------------------------------------------- directory --


def _learn(conn, name, key):
    name = (name or "").strip().lower()
    if not name or not key or _looks_like_key(name):
        return
    conn.execute(
        "INSERT INTO directory VALUES (?,?,?) ON CONFLICT(name, key) "
        "DO UPDATE SET last_seen=excluded.last_seen",
        (name, key, int(time.time())),
    )
    conn.commit()


def _resolve_name(conn, name):
    return conn.execute(
        "SELECT key, last_seen FROM directory WHERE name=? ORDER BY last_seen DESC",
        (name.strip().lower(),),
    ).fetchall()


# ---------------------------------------------------------------- helpers --


class _Raw(str):
    """A reply that must go out verbatim, in exactly one message.

    Everything else is handed to ``ctx.reply_split``; only the ``mbx test``
    size probe needs its exact byte count preserved, numbering prefix and all.
    """


def _looks_like_key(s):
    return bool(re.fullmatch(rf"!?[0-9a-fA-F]{{{MIN_PUBKEY_PREFIX},64}}", s))


def _sender_label(row_key, row_name):
    name = (row_name or "").strip()
    if name == "unknown":
        name = ""
    key8 = (row_key or "")[:MIN_PUBKEY_PREFIX]
    bare = name.lstrip("!").lower()
    if row_key and bare and (row_key.startswith(bare) or bare.startswith(row_key)):
        name = ""
    if name and key8:
        return f"{name[:NAME_TRUNC]} [{key8}]"
    return name[:NAME_TRUNC] or key8 or "unknown"


def _age_str(created_at):
    d = max(0, int(time.time()) - created_at)
    if d < 3600:
        return f"{d // 60}m"
    if d < 86400:
        return f"{d // 3600}h"
    return f"{d // 86400}d"


def _match_clause(key):
    if not key:
        return "0", []
    return ("(recipient_is_key=1 AND ? LIKE recipient || '%')", [key])


def _counts(conn, where, params):
    new = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE {where} AND played=0", params
    ).fetchone()[0]
    heard = conn.execute(
        f"SELECT COUNT(*) FROM messages WHERE {where} AND played=1", params
    ).fetchone()[0]
    return new, heard


# --------------------------------------------------------------- commands --


def _cmd_store(settings, conn, recipient, body, key, name):
    recipient = recipient.strip().lower().lstrip("!")
    if not _looks_like_key(recipient):
        return (
            f"Address by key only ({MIN_PUBKEY_PREFIX}+ hex chars). "
            f"To send by name, use: {_prefix(settings)} to <name>"
        )

    body_bytes = body.encode("utf-8")
    truncated = len(body_bytes) > _max_body(settings)
    if truncated:
        body = body_bytes[: _max_body(settings)].decode("utf-8", errors="ignore")

    n_recip = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE recipient=? AND played=0", (recipient,)
    ).fetchone()[0]
    if n_recip >= MAX_PER_RECIPIENT:
        return f"Mailbox for '{recipient}' is full ({MAX_PER_RECIPIENT} pending)."

    n_sender = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE COALESCE(sender_key, sender_name)=? AND played=0",
        (key or (name or "").lower(),),
    ).fetchone()[0]
    if n_sender >= MAX_PER_SENDER:
        return f"You have {MAX_PER_SENDER} undelivered messages pending. Limit reached."

    conn.execute(
        "INSERT INTO messages (recipient, recipient_is_key, sender_key, sender_name, "
        "body, created_at) VALUES (?,1,?,?,?,?)",
        (recipient, key or None, (name or "").lower() or None, body, int(time.time())),
    )
    conn.commit()
    size = len(body.encode("utf-8"))
    extra = f" NOTE: cut at {_max_body(settings)}B limit." if truncated else ""
    return (
        f"Saved for {recipient[:20]} ({size}B). "
        f"Tell them to DM me '{_prefix(settings)} inbox'.{extra}"
    )


def _cmd_to(settings, conn, arg, rid):
    target = arg.strip().lower().lstrip("!")
    if not target:
        return f"Usage: {_prefix(settings)} to <key or name>"

    if _looks_like_key(target):
        recipient = target
        label = recipient[:12] + "…"
    else:
        matches = _resolve_name(conn, target)
        if not matches:
            return (
                f"Unknown name '{target[:NAME_TRUNC]}'. I only know nodes "
                f"I've heard. Use their key: {_prefix(settings)} to <{MIN_PUBKEY_PREFIX}+ hex>"
            )
        if len(matches) > 1:
            opts = " / ".join(k[:MIN_PUBKEY_PREFIX] for k, _ in matches[:4])
            return (
                f"'{target[:NAME_TRUNC]}' matches {len(matches)} keys: "
                f"{opts}. Names aren't unique — pick one: {_prefix(settings)} to <key>"
            )
        recipient, seen = matches[0]
        label = (
            f"{target[:NAME_TRUNC]} [{recipient[:MIN_PUBKEY_PREFIX]}], heard {_age_str(seen)} ago"
        )

    conn.execute(
        "INSERT INTO pending (requester, recipient, draft, created_at) "
        "VALUES (?,?,'',?) ON CONFLICT(requester) DO UPDATE SET "
        "recipient=excluded.recipient, draft='', created_at=excluded.created_at",
        (rid, recipient, int(time.time())),
    )
    conn.commit()
    return (
        f"Composing to {label} (10m). CHECK THE KEY, names can be faked. "
        f"'{_prefix(settings)} send <text>' to send, '{_prefix(settings)} add <text>' for "
        f"longer messages."
    )


def _get_pending(conn, rid):
    row = conn.execute(
        "SELECT recipient, draft, created_at FROM pending WHERE requester=?", (rid,)
    ).fetchone()
    if not row or int(time.time()) - row[2] > PENDING_TTL:
        conn.execute("DELETE FROM pending WHERE requester=?", (rid,))
        conn.commit()
        return None
    return row


def _cmd_add(settings, conn, text, rid):
    row = _get_pending(conn, rid)
    if not row:
        return f"No pending recipient. Start with '{_prefix(settings)} to <key or name>'."
    if not text.strip():
        return f"Nothing to add. Usage: {_prefix(settings)} add <text>"
    draft = (row[1] + " " + text).strip() if row[1] else text
    size = len(draft.encode("utf-8"))
    if size > _max_body(settings):
        return (
            f"Draft would exceed the {_max_body(settings)}B limit "
            f"({size}B). Not added. '{_prefix(settings)} send' to deliver as-is."
        )
    conn.execute(
        "UPDATE pending SET draft=?, created_at=? WHERE requester=?",
        (draft, int(time.time()), rid),
    )
    conn.commit()
    return (
        f"Draft: {size}/{_max_body(settings)}B. "
        f"'{_prefix(settings)} add <more>' or '{_prefix(settings)} send' to deliver."
    )


def _cmd_send(settings, conn, text, key, name, rid):
    row = _get_pending(conn, rid)
    if not row:
        return f"No pending recipient. Start with '{_prefix(settings)} to <key or name>'."
    body = (row[1] + " " + text).strip() if (row[1] and text.strip()) else (text.strip() or row[1])
    if not body:
        return (
            f"Empty message. '{_prefix(settings)} add <text>' or '{_prefix(settings)} send <text>'."
        )
    conn.execute("DELETE FROM pending WHERE requester=?", (rid,))
    conn.commit()
    return _cmd_store(settings, conn, row[0], body, key, name)


def _cmd_inbox(settings, conn, key):
    where, params = _match_clause(key)
    new, heard = _counts(conn, where, params)
    if not new and not heard:
        return "No messages."
    if new and heard:
        first_new = conn.execute(
            f"SELECT MIN(id) FROM messages WHERE {where} AND played=0", params
        ).fetchone()[0]
        pos = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE {where} AND id <= ?", params + [first_new]
        ).fetchone()[0]
        return f"{new} new, {heard} kept. New start at #{pos}: '{_prefix(settings)} play {pos}'."
    if new:
        return f"{new} new message(s). '{_prefix(settings)} play' to read."
    return f"{heard} kept. '{_prefix(settings)} play' replays, '{_prefix(settings)} clear' deletes."


def _play_one(settings, conn, key, rid, index=None, advance=False):
    where, params = _match_clause(key)
    total = conn.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0]
    if not total:
        return "No messages."

    if index is not None:
        n = index
    elif advance:
        n = 1
        cur = conn.execute("SELECT message_id FROM cursors WHERE requester=?", (rid,)).fetchone()
        if cur:
            n = (
                conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {where} AND id <= ?", params + [cur[0]]
                ).fetchone()[0]
                + 1
            )
    else:
        n = 1

    n = max(1, n)
    if n > total:
        return (
            f"End of messages ({total} total). "
            f"'{_prefix(settings)} play' restarts, '{_prefix(settings)} clear' deletes kept."
        )

    row = conn.execute(
        f"SELECT id, sender_key, sender_name, body, created_at, played "
        f"FROM messages WHERE {where} ORDER BY id ASC LIMIT 1 OFFSET ?",
        params + [n - 1],
    ).fetchone()
    mid, skey, sname, body, created, was_kept = row
    conn.execute("UPDATE messages SET played=1 WHERE id=?", (mid,))
    conn.execute(
        "INSERT INTO cursors (requester, message_id, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(requester) DO UPDATE SET message_id=excluded.message_id, "
        "updated_at=excluded.updated_at",
        (rid, mid, int(time.time())),
    )
    conn.commit()

    header = f"From {_sender_label(skey, sname)} ({_age_str(created)} ago): "
    tag = ",kept" if was_kept else ""
    if n < total:
        hint = f"\n>{n}/{total}{tag} | {_prefix(settings)} del | {_prefix(settings)} next"
    else:
        hint = f"\n>{n}/{total}{tag} | {_prefix(settings)} del, no more"

    return header + body + hint


def _cmd_del(settings, conn, key, rid):
    cur = conn.execute("SELECT message_id FROM cursors WHERE requester=?", (rid,)).fetchone()
    if not cur:
        return f"Nothing to delete. Send '{_prefix(settings)} play' first."
    where, params = _match_clause(key)
    pred = conn.execute(
        f"SELECT MAX(id) FROM messages WHERE {where} AND id < ?", params + [cur[0]]
    ).fetchone()[0]
    deleted = conn.execute(
        f"DELETE FROM messages WHERE id=? AND {where}", [cur[0]] + params
    ).rowcount
    if pred is not None:
        conn.execute(
            "UPDATE cursors SET message_id=?, updated_at=? WHERE requester=?",
            (pred, int(time.time()), rid),
        )
    else:
        conn.execute("DELETE FROM cursors WHERE requester=?", (rid,))
    conn.commit()
    if not deleted:
        return f"Nothing to delete. Send '{_prefix(settings)} play' first."
    new, _ = _counts(conn, where, params)
    if new:
        return f"Deleted. {new} left: '{_prefix(settings)} next'."
    return "Deleted. No new messages."


def _cmd_clear(conn, key):
    where, params = _match_clause(key)
    cur = conn.execute(f"DELETE FROM messages WHERE {where} AND played=1", params)
    conn.commit()
    return f"Deleted {cur.rowcount} kept message(s)."


def _cmd_help_short(settings):
    p = _prefix(settings)
    return (
        f"{p} msg <key> <txt> | {p} to <key|name> | {p} add | {p} send | "
        f"{p} inbox | {p} play [n] | {p} next | {p} del | {p} clear | "
        f"'{p} help' = full guide"
    )


def _cmd_help(settings):
    """Condensed guide. Newlines keep the wording readable as it goes out."""
    p = _prefix(settings)
    return (
        f"MAILBOX: Hold messages for offline nodes (unread 7d, read 30d).\n"
        f"Send: {p} msg <key> <text> (key = {MIN_PUBKEY_PREFIX}+ hex of their "
        f"pubkey), or {p} to <key|name> then {p} send <text>.\n"
        f"By name works if I've heard the node.\n"
        f"Long msgs (max {_max_body(settings)}B): {p} add <text> "
        f"repeatedly, then {p} send.\n"
        f"Read: {p} inbox, {p} play [n], {p} next, {p} del, {p} clear.\n"
        f"Stored UNENCRYPTED; {p} accept once to agree.\n"
        f"'{p} ?' = quick list."
    )


def _cmd_size_test(settings, arg):
    """Exactly-N-byte probe. _Raw so reply_split never renumbers or cuts it.

    N is required: the bot knows nothing about frame sizes, so there is no
    default to fall back on.
    """
    try:
        n = int(arg)
    except (TypeError, ValueError):
        return f"Usage: {_prefix(settings)} test <bytes> (20-500)"
    n = max(20, min(n, 500))
    prefix = f"TEST {n}B "
    ruler = "".join(f"{i:.>10}" for i in range(10, n + 20, 10))
    return _Raw((prefix + ruler[len(prefix) :])[:n])


# ------------------------------------------------------------- entrypoint --


def _is_mailbox_command(settings, text):
    """True when text is the configured mailbox prefix or starts with it."""
    clean = (text or "").strip().lower()
    prefix = _prefix(settings).lower()
    return clean == prefix or clean.startswith(prefix + " ")


async def _redirect_channel_mailbox_to_dm(ctx, sender_name):
    """Advertise, wait for contact learning, then open Mailbox help in a DM."""
    sender_name = (sender_name or "").strip()
    pending_key = sender_name.lower()

    try:
        # RemoteTerm's built-in advert path. This is intentionally a flood
        # advert so the requesting node has a chance to learn this companion.
        from app.routers.radio import RadioAdvertiseRequest, send_advertisement

        try:
            await send_advertisement(RadioAdvertiseRequest(mode="flood"))
        except Exception as exc:
            ctx.log(
                f"mailbox DM redirect advert error: {type(exc).__name__}: {exc}",
                level="WARNING",
            )
            await ctx.reply(
                f"@[{sender_name}] Mailbox is DM-only, but the advert failed. "
                "Send an advert and try again."
            )
            return

        await ctx.reply(
            f"@[{sender_name}] Mailbox is private only. Advert sent; "
            "I'll message you in about 30 seconds."
        )

        # Do not block RemoteTerm while the remote node learns our advert.
        await asyncio.sleep(30)

        from app.repository import ContactRepository

        contacts = await ContactRepository.get_by_name(sender_name)

        # Never guess if duplicate names exist.
        if len(contacts) != 1:
            if not contacts:
                reason = "I still can't find your contact"
            else:
                reason = "more than one contact has that name"

            await ctx.reply(
                f"@[{sender_name}] {reason}. Send an advert, then try 'mbx help' again."
            )
            return

        public_key = contacts[0].public_key

        # One nudge, not a hand-rolled guide: 'mbx help' here answers through
        # the normal DM path, which lets ctx.reply_split size the reply.
        await ctx.send_dm(
            public_key,
            "MAILBOX PRIVATE: Mailbox commands are used here in DM. "
            f"Send '{_prefix(ctx.settings)} help' for the full guide.",
        )

    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ctx.log(
            f"mailbox DM redirect error: {type(exc).__name__}: {exc}",
            level="WARNING",
        )
    finally:
        if pending_key:
            _redirect_pending.discard(pending_key)


async def _send(ctx, reply):
    """Put one command's reply on the air.

    ``_bot_inner`` and its ``_cmd_*`` helpers return one plain string (or None)
    and never size it: ``ctx.reply_split`` owns delivery. A ``_Raw`` reply is
    sent verbatim so ``mbx test`` keeps its exact byte count.
    """
    if reply is None:
        return
    if isinstance(reply, _Raw):
        await ctx.reply(str(reply))
        return
    await ctx.reply_split(reply)


@bot.on_message()
async def mailbox(ctx, msg):
    """Runs for every in-scope message.

    Normal Mailbox processing remains synchronous in _bot_inner and is moved to
    a worker thread. Channel-side ``mbx ...`` requests only start a delayed
    redirect task; they never block the RemoteTerm bot engine.
    """

    settings = ctx.settings

    # Preserve the original outgoing-message behavior.
    if msg.is_outgoing:
        return None

    # Mailbox is still functionally DM-only. The only channel behavior added
    # here is a UX redirect when a user explicitly types the mailbox prefix.
    if not msg.is_dm:
        text = (msg.text or "").strip()

        if not _is_mailbox_command(settings, text):
            # Preserve passive directory learning when a key is available.
            if msg.sender_key and msg.sender_name:
                try:
                    await asyncio.to_thread(
                        _learn,
                        _db(settings),
                        msg.sender_name,
                        msg.sender_key.lower(),
                    )
                except Exception:
                    pass
            return None

        sender_name = (msg.sender_name or "").strip()
        if not sender_name:
            return None

        pending_key = sender_name.lower()

        if pending_key in _redirect_pending:
            await ctx.reply(f"@[{sender_name}] Mailbox private redirect is already in progress.")
            return None

        _redirect_pending.add(pending_key)

        task = asyncio.create_task(_redirect_channel_mailbox_to_dm(ctx, sender_name))
        _redirect_tasks.add(task)
        task.add_done_callback(_redirect_tasks.discard)

        return None

    kwargs = {
        "sender_name": msg.sender_name,
        "sender_key": msg.sender_key,
        "message_text": msg.text,
        "is_dm": msg.is_dm,
        "is_outgoing": msg.is_outgoing,
    }

    try:
        # _bot_inner uses blocking sqlite3. Keep that work off the event loop.
        reply = await asyncio.to_thread(
            _bot_inner,
            settings,
            **kwargs,
        )
    except Exception as e:
        # Checking debug auth must itself never raise (the DB may be the very
        # thing that's broken) — fall back to the generic notice.
        try:
            rid = (msg.sender_key or "").lower() or (msg.sender_name or "").lower()
            authed = await asyncio.to_thread(
                lambda: bool(
                    rid
                    and _db(settings)
                    .execute(
                        "SELECT 1 FROM debug_auth WHERE requester=?",
                        (rid,),
                    )
                    .fetchone()
                )
            )
            if authed:
                await _send(ctx, f"MBX ERR: {type(e).__name__}: {e}")
                return None
        except Exception:
            pass

        ctx.log(f"mailbox error: {type(e).__name__}: {e}", level="WARNING")
        await _send(
            ctx,
            f"An error occurred. Authenticate with "
            f"'{_prefix(settings)} debug <password>' to see error details.",
        )
        return None

    await _send(ctx, reply)
    return None


def _bot_inner(settings, **kwargs) -> "str | None":
    sender_name = kwargs.get("sender_name") or ""
    sender_key = (kwargs.get("sender_key") or "").lower()
    message_text = kwargs.get("message_text", "") or ""
    is_dm = kwargs.get("is_dm", False)
    is_outgoing = kwargs.get("is_outgoing", False)

    if is_outgoing:
        return None

    if sender_key and sender_name:
        try:
            _learn(_db(settings), sender_name, sender_key)
        except Exception:
            pass

    if not is_dm:
        return None

    text = message_text.strip()
    low = text.lower()
    if low != _prefix(settings) and not low.startswith(_prefix(settings) + " "):
        return None

    rest = text[len(_prefix(settings)) :].strip()
    parts = rest.split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    rid = sender_key or sender_name.lower()
    if not rid:
        return "Can't identify you (no key). Try again."

    conn = _db(settings)
    _purge_expired(conn)

    # ---- help & debug: allowed before consent ----
    if cmd in ("", "?"):
        return _cmd_help_short(settings)

    if cmd in ("help"):
        return _cmd_help(settings)

    if cmd == "debug":
        authed = conn.execute("SELECT 1 FROM debug_auth WHERE requester=?", (rid,)).fetchone()
        if not authed:
            if arg and hmac.compare_digest(arg, _debug_password(settings)):
                conn.execute(
                    "INSERT OR REPLACE INTO debug_auth VALUES (?,?)", (rid, int(time.time()))
                )
                conn.commit()
                return f"Debug unlocked. Send '{_prefix(settings)} debug' again."
            return "Debug is password-locked."
        # env_dump = " ".join(
        #    f"{k}={v}"
        #    for k, v in sorted(os.environ.items())
        #    if k.startswith("MAILBOX_") and k != "fcffff"
        # )
        n_dir = conn.execute("SELECT COUNT(*) FROM directory").fetchone()[0]
        return (
            f"db={_db_path(settings)} "
            f"body={_max_body(settings)} "
            f"(hard {HARD_MAX_BODY}) dir={n_dir} names"
            # f"env: {env_dump or '(none)'}"
        )

    if cmd == "test":
        authed = conn.execute("SELECT 1 FROM debug_auth WHERE requester=?", (rid,)).fetchone()
        if not authed:
            return f"Size test is debug-gated. Unlock with '{_prefix(settings)} debug <password>'."
        return _cmd_size_test(settings, arg)

    # ---- consent gate for everything else ----
    consented = conn.execute("SELECT 1 FROM consents WHERE requester=?", (rid,)).fetchone()
    if cmd == "accept":
        if consented:
            return "Already accepted."
        conn.execute("INSERT INTO consents VALUES (?,?)", (rid, int(time.time())))
        conn.commit()
        return f"Accepted. '{_prefix(settings)} help' for commands."
    if not consented:
        return _privacy_notice(settings)

    # ---- mailbox commands ----
    if cmd == "msg":
        sub = arg.split(None, 1)
        if len(sub) < 2:
            return f"Usage: {_prefix(settings)} msg <key> <text>"
        return _cmd_store(settings, conn, sub[0], sub[1], sender_key, sender_name)

    if cmd == "to":
        return _cmd_to(settings, conn, arg, rid)

    if cmd == "add":
        return _cmd_add(settings, conn, arg, rid)

    if cmd == "send":
        return _cmd_send(settings, conn, arg, sender_key, sender_name, rid)

    if cmd == "inbox":
        return _cmd_inbox(settings, conn, sender_key)

    if cmd in ("play", "next"):
        idx = None
        if arg:
            try:
                idx = int(arg.split()[0])
            except ValueError:
                pass
        return _play_one(
            settings, conn, sender_key, rid, index=idx, advance=(cmd == "next" and idx is None)
        )

    if cmd == "del":
        return _cmd_del(settings, conn, sender_key, rid)

    if cmd == "clear":
        return _cmd_clear(conn, sender_key)

    return f"Unknown command. '{_prefix(settings)} help'"
