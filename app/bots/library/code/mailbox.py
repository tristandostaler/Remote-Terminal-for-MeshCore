"""MeshCore Mailbox (store-and-forward) — RemoteTerm bot version.

Two handlers, one job each:

* ``mailbox_learn`` (``@bot.on_message``) sees every in-scope message and only
  learns ``name -> pubkey``, so ``mbx to <name>`` can resolve a node this
  companion has heard. It never replies and never runs a command.
* ``mailbox_command`` (``@bot.on_keyword``) is the command itself. A real
  keyword trigger is what makes the node's ``help`` bot list ``mbx`` alongside
  every other command, and the bare decorator beside it carries whatever the
  operator adds on the Triggers tab. Replies quote back the word that matched,
  so both an alias declared here and an operator's own word read correctly.

Commands (DM-only; ``mbx`` below stands for the trigger word that was used):

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
  mbx debug              Dump the resolved config (debug-gated)
  mbx test <N>           Size probe of exactly N bytes (debug-gated, raw)

Used on a channel instead of in a DM, a command sends a flood advert, waits
about 30 seconds for the sender's node to learn this companion, then carries on
in a DM. Keep the scope wide enough that passive learning sees traffic.

Replies are NEVER truncated and this bot never sizes them: every command
returns one plain string and the handler hands it to ``ctx.reply_split``, which
owns delivery entirely. ``mbx test`` is the one exception — its probe must go
out verbatim, so it is tagged ``_Raw``.

Blocking SQLite work runs through ``asyncio.to_thread``; the handle lives in the
module-level ``_cache`` (persists per load, resets on code edit + save). Error
details reach debug-unlocked senders only; everyone else gets the generic
notice and the operator sees the cause in Bots > Logs (``ctx.log``).

Body size: hard ceiling HARD_MAX_BODY (2000B) per stored message; the max_body
setting can lower it, never raise it. Retention: unread 7d, kept 30d. Identity
is KEY-ONLY (full 64-char sender_key). Stdlib only.
"""

import asyncio
import hmac
import os
import re
import sqlite3
import time

from remoteterm import bot

HARD_MAX_BODY = 2000

# The word this bot declares. Pass more to @bot.on_keyword below to add
# aliases; it also stands in as the prefix quoted back in replies when a caller
# leaves no matched keyword to echo.
DEFAULT_PREFIX = "mbx"

BOT_META = {
    "key": "mailbox",
    "name": "mailbox",
    "category": "Custom",
    "description": "Store-and-forward mailbox for offline nodes (DM 'mbx help')",
    "version": "5.0.0",
    "respond_to_dms": True,
    "settings_schema": [
        {
            "key": "db_path",
            "label": "MailBox database path",
            "type": "text",
            "default": "data/mailbox.db",
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
REDIRECT_WAIT_SECONDS = 30
SCHEMA_VERSION = 4  # bump any time tables/columns change

# Cross-call cache for this load: the SQLite handle. Persists across handler
# calls; resets when the code is edited and saved.
_cache: dict = {}

# Keep strong references to delayed redirect tasks, and avoid duplicate timers
# when the same node sends several commands on a channel.
_redirect_tasks: set[asyncio.Task] = set()
_redirect_pending: set[str] = set()


# ----------------------------------------------------------------- config --


def _setting(settings, key):
    """Setting value with the BOT_META default as fallback (blank == unset)."""
    default = BOT_META["settings"][key]
    value = (settings or {}).get(key, default)
    return default if value in (None, "") else value


class _Cfg:
    """One call's resolved settings, plus the word that triggered the bot.

    Commands quote the trigger word back (``'mbx send <text>' to deliver``), so
    a node reached through an operator-added keyword reads instructions it can
    actually retype.
    """

    __slots__ = ("db_path", "debug_password", "max_body", "prefix")

    def __init__(self, settings, prefix=""):
        self.db_path = str(_setting(settings, "db_path") or "").strip()
        self.debug_password = str(_setting(settings, "debug_password") or "").strip()
        self.prefix = (prefix or "").strip().lower() or DEFAULT_PREFIX
        try:
            body = int(_setting(settings, "max_body"))
        except (TypeError, ValueError):
            body = 0
        # 0, blank or garbage means "no opinion" — the ceiling. A configured
        # value may lower the ceiling, never raise it.
        self.max_body = HARD_MAX_BODY if body <= 0 else min(body, HARD_MAX_BODY)


class _Sender:
    """Who is talking to us. Identity is the full pubkey whenever we have one."""

    __slots__ = ("key", "name", "rid")

    def __init__(self, key, name):
        self.key = (key or "").lower()
        self.name = (name or "").strip()
        self.rid = self.key or self.name.lower()


def _privacy_notice(cfg):
    return (
        f"Mailbox stores messages UNENCRYPTED, readable by the "
        f"operator. Send '{cfg.prefix} accept' once to agree."
    )


# ---------------------------------------------------------------- storage --


def _db(cfg):
    cache_key = f"conn_v{SCHEMA_VERSION}"
    if cache_key not in _cache:
        for k in list(_cache):
            if k.startswith("conn_") and k != cache_key:
                try:
                    _cache[k].close()
                except Exception:
                    pass
                del _cache[k]
        os.makedirs(os.path.dirname(cfg.db_path) or ".", exist_ok=True)
        conn = sqlite3.connect(cfg.db_path, timeout=5, check_same_thread=False)
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


def _index_arg(arg):
    """Leading integer of ``arg``, or None when there isn't one."""
    tokens = (arg or "").split()
    if not tokens:
        return None
    try:
        return int(tokens[0])
    except ValueError:
        return None


def _get_pending(conn, rid):
    row = conn.execute(
        "SELECT recipient, draft, created_at FROM pending WHERE requester=?", (rid,)
    ).fetchone()
    if not row or int(time.time()) - row[2] > PENDING_TTL:
        conn.execute("DELETE FROM pending WHERE requester=?", (rid,))
        conn.commit()
        return None
    return row


def _has_consent(conn, rid):
    return bool(conn.execute("SELECT 1 FROM consents WHERE requester=?", (rid,)).fetchone())


def _is_debug_authed(conn, rid):
    return bool(
        rid and conn.execute("SELECT 1 FROM debug_auth WHERE requester=?", (rid,)).fetchone()
    )


# --------------------------------------------------------------- commands --
#
# Every command takes (cfg, conn, arg, who) and returns one plain string. They
# run in a worker thread, so none of them may await or size a reply.


def _store(cfg, conn, recipient, body, who):
    """Shared tail of ``msg`` and ``send``: write one message to the mailbox."""
    recipient = recipient.strip().lower().lstrip("!")
    if not _looks_like_key(recipient):
        return (
            f"Address by key only ({MIN_PUBKEY_PREFIX}+ hex chars). "
            f"To send by name, use: {cfg.prefix} to <name>"
        )

    body_bytes = body.encode("utf-8")
    truncated = len(body_bytes) > cfg.max_body
    if truncated:
        body = body_bytes[: cfg.max_body].decode("utf-8", errors="ignore")

    n_recip = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE recipient=? AND played=0", (recipient,)
    ).fetchone()[0]
    if n_recip >= MAX_PER_RECIPIENT:
        return f"Mailbox for '{recipient}' is full ({MAX_PER_RECIPIENT} pending)."

    n_sender = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE COALESCE(sender_key, sender_name)=? AND played=0",
        (who.key or who.name.lower(),),
    ).fetchone()[0]
    if n_sender >= MAX_PER_SENDER:
        return f"You have {MAX_PER_SENDER} undelivered messages pending. Limit reached."

    conn.execute(
        "INSERT INTO messages (recipient, recipient_is_key, sender_key, sender_name, "
        "body, created_at) VALUES (?,1,?,?,?,?)",
        (recipient, who.key or None, who.name.lower() or None, body, int(time.time())),
    )
    conn.commit()
    size = len(body.encode("utf-8"))
    extra = f" NOTE: cut at {cfg.max_body}B limit." if truncated else ""
    return f"Saved for {recipient[:20]} ({size}B). Tell them to DM me '{cfg.prefix} inbox'.{extra}"


def _cmd_msg(cfg, conn, arg, who):
    parts = arg.split(None, 1)
    if len(parts) < 2:
        return f"Usage: {cfg.prefix} msg <key> <text>"
    return _store(cfg, conn, parts[0], parts[1], who)


def _cmd_to(cfg, conn, arg, who):
    target = arg.strip().lower().lstrip("!")
    if not target:
        return f"Usage: {cfg.prefix} to <key or name>"

    if _looks_like_key(target):
        recipient = target
        label = recipient[:12] + "…"
    else:
        matches = _resolve_name(conn, target)
        if not matches:
            return (
                f"Unknown name '{target[:NAME_TRUNC]}'. I only know nodes "
                f"I've heard. Use their key: {cfg.prefix} to <{MIN_PUBKEY_PREFIX}+ hex>"
            )
        if len(matches) > 1:
            opts = " / ".join(k[:MIN_PUBKEY_PREFIX] for k, _ in matches[:4])
            return (
                f"'{target[:NAME_TRUNC]}' matches {len(matches)} keys: "
                f"{opts}. Names aren't unique — pick one: {cfg.prefix} to <key>"
            )
        recipient, seen = matches[0]
        label = (
            f"{target[:NAME_TRUNC]} [{recipient[:MIN_PUBKEY_PREFIX]}], heard {_age_str(seen)} ago"
        )

    conn.execute(
        "INSERT INTO pending (requester, recipient, draft, created_at) "
        "VALUES (?,?,'',?) ON CONFLICT(requester) DO UPDATE SET "
        "recipient=excluded.recipient, draft='', created_at=excluded.created_at",
        (who.rid, recipient, int(time.time())),
    )
    conn.commit()
    return (
        f"Composing to {label} (10m). CHECK THE KEY, names can be faked. "
        f"'{cfg.prefix} send <text>' to send, '{cfg.prefix} add <text>' for "
        f"longer messages."
    )


def _cmd_add(cfg, conn, arg, who):
    row = _get_pending(conn, who.rid)
    if not row:
        return f"No pending recipient. Start with '{cfg.prefix} to <key or name>'."
    if not arg.strip():
        return f"Nothing to add. Usage: {cfg.prefix} add <text>"
    draft = (row[1] + " " + arg).strip() if row[1] else arg
    size = len(draft.encode("utf-8"))
    if size > cfg.max_body:
        return (
            f"Draft would exceed the {cfg.max_body}B limit "
            f"({size}B). Not added. '{cfg.prefix} send' to deliver as-is."
        )
    conn.execute(
        "UPDATE pending SET draft=?, created_at=? WHERE requester=?",
        (draft, int(time.time()), who.rid),
    )
    conn.commit()
    return (
        f"Draft: {size}/{cfg.max_body}B. "
        f"'{cfg.prefix} add <more>' or '{cfg.prefix} send' to deliver."
    )


def _cmd_send(cfg, conn, arg, who):
    row = _get_pending(conn, who.rid)
    if not row:
        return f"No pending recipient. Start with '{cfg.prefix} to <key or name>'."
    text = arg.strip()
    body = (row[1] + " " + text).strip() if (row[1] and text) else (text or row[1])
    if not body:
        return f"Empty message. '{cfg.prefix} add <text>' or '{cfg.prefix} send <text>'."
    conn.execute("DELETE FROM pending WHERE requester=?", (who.rid,))
    conn.commit()
    return _store(cfg, conn, row[0], body, who)


def _cmd_inbox(cfg, conn, arg, who):
    where, params = _match_clause(who.key)
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
        return f"{new} new, {heard} kept. New start at #{pos}: '{cfg.prefix} play {pos}'."
    if new:
        return f"{new} new message(s). '{cfg.prefix} play' to read."
    return f"{heard} kept. '{cfg.prefix} play' replays, '{cfg.prefix} clear' deletes."


def _play_one(cfg, conn, who, index=None, advance=False):
    where, params = _match_clause(who.key)
    total = conn.execute(f"SELECT COUNT(*) FROM messages WHERE {where}", params).fetchone()[0]
    if not total:
        return "No messages."

    n = 1
    if index is not None:
        n = index
    elif advance:
        cur = conn.execute(
            "SELECT message_id FROM cursors WHERE requester=?", (who.rid,)
        ).fetchone()
        if cur:
            n = (
                conn.execute(
                    f"SELECT COUNT(*) FROM messages WHERE {where} AND id <= ?", params + [cur[0]]
                ).fetchone()[0]
                + 1
            )

    n = max(1, n)
    if n > total:
        return (
            f"End of messages ({total} total). "
            f"'{cfg.prefix} play' restarts, '{cfg.prefix} clear' deletes kept."
        )

    mid, skey, sname, body, created, was_kept = conn.execute(
        f"SELECT id, sender_key, sender_name, body, created_at, played "
        f"FROM messages WHERE {where} ORDER BY id ASC LIMIT 1 OFFSET ?",
        params + [n - 1],
    ).fetchone()
    conn.execute("UPDATE messages SET played=1 WHERE id=?", (mid,))
    conn.execute(
        "INSERT INTO cursors (requester, message_id, updated_at) VALUES (?,?,?) "
        "ON CONFLICT(requester) DO UPDATE SET message_id=excluded.message_id, "
        "updated_at=excluded.updated_at",
        (who.rid, mid, int(time.time())),
    )
    conn.commit()

    header = f"From {_sender_label(skey, sname)} ({_age_str(created)} ago): "
    tag = ",kept" if was_kept else ""
    if n < total:
        hint = f"\n>{n}/{total}{tag} | {cfg.prefix} del | {cfg.prefix} next"
    else:
        hint = f"\n>{n}/{total}{tag} | {cfg.prefix} del, no more"
    return header + body + hint


def _cmd_play(cfg, conn, arg, who):
    return _play_one(cfg, conn, who, index=_index_arg(arg))


def _cmd_next(cfg, conn, arg, who):
    index = _index_arg(arg)
    return _play_one(cfg, conn, who, index=index, advance=index is None)


def _cmd_del(cfg, conn, arg, who):
    cur = conn.execute("SELECT message_id FROM cursors WHERE requester=?", (who.rid,)).fetchone()
    if not cur:
        return f"Nothing to delete. Send '{cfg.prefix} play' first."
    where, params = _match_clause(who.key)
    pred = conn.execute(
        f"SELECT MAX(id) FROM messages WHERE {where} AND id < ?", params + [cur[0]]
    ).fetchone()[0]
    deleted = conn.execute(
        f"DELETE FROM messages WHERE id=? AND {where}", [cur[0]] + params
    ).rowcount
    if pred is not None:
        conn.execute(
            "UPDATE cursors SET message_id=?, updated_at=? WHERE requester=?",
            (pred, int(time.time()), who.rid),
        )
    else:
        conn.execute("DELETE FROM cursors WHERE requester=?", (who.rid,))
    conn.commit()
    if not deleted:
        return f"Nothing to delete. Send '{cfg.prefix} play' first."
    new, _ = _counts(conn, where, params)
    if new:
        return f"Deleted. {new} left: '{cfg.prefix} next'."
    return "Deleted. No new messages."


def _cmd_clear(cfg, conn, arg, who):
    where, params = _match_clause(who.key)
    cur = conn.execute(f"DELETE FROM messages WHERE {where} AND played=1", params)
    conn.commit()
    return f"Deleted {cur.rowcount} kept message(s)."


def _cmd_accept(cfg, conn, arg, who):
    if _has_consent(conn, who.rid):
        return "Already accepted."
    conn.execute("INSERT INTO consents VALUES (?,?)", (who.rid, int(time.time())))
    conn.commit()
    return f"Accepted. '{cfg.prefix} help' for commands."


def _cmd_help_short(cfg, conn, arg, who):
    p = cfg.prefix
    return (
        f"{p} msg <key> <txt> | {p} to <key|name> | {p} add | {p} send | "
        f"{p} inbox | {p} play [n] | {p} next | {p} del | {p} clear | "
        f"'{p} help' = full guide"
    )


def _cmd_help(cfg, conn, arg, who):
    """Condensed guide. Newlines keep the wording readable as it goes out."""
    p = cfg.prefix
    return (
        f"MAILBOX: Hold messages for offline nodes (unread 7d, read 30d).\n"
        f"Send: {p} msg <key> <text> (key = {MIN_PUBKEY_PREFIX}+ hex of their "
        f"pubkey), or {p} to <key|name> then {p} send <text>.\n"
        f"By name works if I've heard the node.\n"
        f"Long msgs (max {cfg.max_body}B): {p} add <text> "
        f"repeatedly, then {p} send.\n"
        f"Read: {p} inbox, {p} play [n], {p} next, {p} del, {p} clear.\n"
        f"Stored UNENCRYPTED; {p} accept once to agree.\n"
        f"'{p} ?' = quick list."
    )


def _cmd_debug(cfg, conn, arg, who):
    """Unlock with the password, then report the resolved config."""
    if not _is_debug_authed(conn, who.rid):
        secret = cfg.debug_password
        if arg and secret and hmac.compare_digest(arg.encode("utf-8"), secret.encode("utf-8")):
            conn.execute(
                "INSERT OR REPLACE INTO debug_auth VALUES (?,?)", (who.rid, int(time.time()))
            )
            conn.commit()
            return f"Debug unlocked. Send '{cfg.prefix} debug' again."
        return "Debug is password-locked."
    n_dir = conn.execute("SELECT COUNT(*) FROM directory").fetchone()[0]
    return f"db={cfg.db_path} body={cfg.max_body} (hard {HARD_MAX_BODY}) dir={n_dir} names"


def _cmd_test(cfg, conn, arg, who):
    """Exactly-N-byte probe. _Raw so reply_split never renumbers or cuts it.

    N is required: the bot knows nothing about frame sizes, so there is no
    default to fall back on.
    """
    if not _is_debug_authed(conn, who.rid):
        return f"Size test is debug-gated. Unlock with '{cfg.prefix} debug <password>'."
    try:
        n = int(arg)
    except (TypeError, ValueError):
        return f"Usage: {cfg.prefix} test <bytes> (20-500)"
    n = max(20, min(n, 500))
    prefix = f"TEST {n}B "
    ruler = "".join(f"{i:.>10}" for i in range(10, n + 20, 10))
    return _Raw((prefix + ruler[len(prefix) :])[:n])


# The word after the trigger -> its command. Anything not listed here is
# answered with the "unknown command" pointer.
_COMMANDS = {
    "": _cmd_help_short,
    "?": _cmd_help_short,
    "help": _cmd_help,
    "debug": _cmd_debug,
    "test": _cmd_test,
    "accept": _cmd_accept,
    "msg": _cmd_msg,
    "to": _cmd_to,
    "add": _cmd_add,
    "send": _cmd_send,
    "inbox": _cmd_inbox,
    "play": _cmd_play,
    "next": _cmd_next,
    "del": _cmd_del,
    "clear": _cmd_clear,
}

# Reachable before the privacy notice is accepted: the guides (so a new node can
# read what it is agreeing to), the acceptance itself, and the debug tools.
_PRE_CONSENT = frozenset({"", "?", "help", "debug", "test", "accept"})


def _dispatch(cfg, rest, who):
    """Run one command. Blocking SQLite work — called via asyncio.to_thread."""
    parts = rest.strip().split(None, 1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    handler = _COMMANDS.get(cmd)
    if handler is None:
        return f"Unknown command. '{cfg.prefix} help'"

    conn = _db(cfg)
    _purge_expired(conn)
    if cmd not in _PRE_CONSENT and not _has_consent(conn, who.rid):
        return _privacy_notice(cfg)
    return handler(cfg, conn, arg, who)


# ------------------------------------------------------------- entrypoint --


def _remainder(msg):
    """Everything the sender typed after the trigger word, spacing intact.

    ``msg.arg_text`` re-joins ``msg.args`` on single spaces, which would rewrite
    a stored message body; the raw text keeps it exactly as it was sent.

    The engine matches on the text with any @-mention and command prefix already
    stripped, so the trigger word is found again here rather than assumed to be
    first: a whole word, so ``@[mbxnode] mbx help`` reads past the node's name.
    """
    text = (msg.text or "").strip()
    keyword = (msg.keyword or "").strip()
    if not keyword:
        return msg.arg_text
    found = re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.IGNORECASE)
    if not found:
        return msg.arg_text
    return text[found.end() :].strip()


async def _send(ctx, reply):
    """Put one command's reply on the air.

    Commands return one plain string and never size it: ``ctx.reply_split``
    owns delivery. A ``_Raw`` reply goes out verbatim so ``mbx test`` keeps its
    exact byte count.
    """
    if not reply:
        return
    if isinstance(reply, _Raw):
        await ctx.reply(str(reply))
        return
    await ctx.reply_split(reply)


async def _report_error(ctx, cfg, who, exc):
    """Details for debug-unlocked senders, the generic notice for everyone else."""
    ctx.log(f"mailbox error: {type(exc).__name__}: {exc}", level="WARNING")
    try:
        # The DB may be the very thing that broke, so this lookup must not
        # raise either — fall back to the generic notice.
        authed = await asyncio.to_thread(lambda: _is_debug_authed(_db(cfg), who.rid))
    except Exception:
        authed = False
    if authed:
        await _send(ctx, f"MBX ERR: {type(exc).__name__}: {exc}")
        return
    await _send(
        ctx,
        f"An error occurred. Authenticate with "
        f"'{cfg.prefix} debug <password>' to see error details.",
    )


async def _open_dm(ctx, cfg, who):
    """Advert, wait for the sender to learn us, then continue in a DM."""
    try:
        # RemoteTerm's built-in advert path. Deliberately a flood advert: the
        # requesting node needs a chance to learn this companion.
        from app.routers.radio import RadioAdvertiseRequest, send_advertisement

        try:
            await send_advertisement(RadioAdvertiseRequest(mode="flood"))
        except Exception as exc:
            ctx.log(
                f"mailbox DM redirect advert error: {type(exc).__name__}: {exc}",
                level="WARNING",
            )
            await ctx.reply(
                f"@[{who.name}] Mailbox is DM-only, but the advert failed. "
                "Send an advert and try again."
            )
            return

        await ctx.reply(
            f"@[{who.name}] Mailbox is private only. Advert sent; "
            f"I'll message you in about {REDIRECT_WAIT_SECONDS} seconds."
        )

        # Do not block RemoteTerm while the remote node learns our advert.
        await asyncio.sleep(REDIRECT_WAIT_SECONDS)

        from app.repository import ContactRepository

        contacts = await ContactRepository.get_by_name(who.name)

        # Never guess if duplicate names exist.
        if len(contacts) != 1:
            reason = (
                "I still can't find your contact"
                if not contacts
                else "more than one contact has that name"
            )
            await ctx.reply(
                f"@[{who.name}] {reason}. Send an advert, then try '{cfg.prefix} help' again."
            )
            return

        # One nudge, not a hand-rolled guide: the help command answers through
        # the normal DM path, where ctx.reply_split sizes the reply.
        await ctx.send_dm(
            contacts[0].public_key,
            "MAILBOX PRIVATE: Mailbox commands are used here in DM. "
            f"Send '{cfg.prefix} help' for the full guide.",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        ctx.log(f"mailbox DM redirect error: {type(exc).__name__}: {exc}", level="WARNING")
    finally:
        _redirect_pending.discard(who.rid)


async def _redirect_to_dm(ctx, cfg, who):
    """Start the channel -> DM handoff, at most one per sender at a time."""
    if not who.name:
        # The handoff resolves the contact by name; without one there is
        # nothing to resolve and nobody to @-address.
        return
    if ctx.is_test:
        await ctx.reply(
            f"(test run — would advert, wait {REDIRECT_WAIT_SECONDS}s, then DM {who.name})"
        )
        return
    if who.rid in _redirect_pending:
        await ctx.reply(f"@[{who.name}] Mailbox private redirect is already in progress.")
        return
    _redirect_pending.add(who.rid)
    task = asyncio.create_task(_open_dm(ctx, cfg, who))
    _redirect_tasks.add(task)
    task.add_done_callback(_redirect_tasks.discard)


@bot.on_message()
async def mailbox_learn(ctx, msg):
    """Passive directory learning — the only thing that runs on every message.

    Builds the name -> pubkey directory that ``mbx to <name>`` resolves against.
    Never replies, and never handles a command: that is ``mailbox_command``'s
    job. Commands are learned from too, which is harmless — the write is an
    upsert keyed on (name, key).
    """
    if msg.is_outgoing or not (msg.sender_key and msg.sender_name):
        return
    try:
        await asyncio.to_thread(
            _learn, _db(_Cfg(ctx.settings)), msg.sender_name, msg.sender_key.lower()
        )
    except Exception as exc:
        # This runs per message, so an unreachable database would fill the log
        # ring with one identical line per packet heard. Say it once per load.
        seen = _cache.setdefault("learn_errors", set())
        detail = f"{type(exc).__name__}: {exc}"
        if detail not in seen:
            seen.add(detail)
            ctx.log(f"mailbox learn error (logged once): {detail}", level="WARNING")


@bot.on_keyword()
@bot.on_keyword(DEFAULT_PREFIX)
async def mailbox_command(ctx, msg):
    """The mailbox command: ``mbx``, plus any keyword on the Triggers tab.

    Declaring the trigger as a keyword is what puts mailbox in the node's
    ``help`` command list. Replies quote back the word that was matched rather
    than the constant, so an alias declared here and an operator's own word
    both read correctly.

    Mailbox itself stays DM-only; on a channel the command starts the
    advert-then-DM handoff instead of answering there.
    """
    cfg = _Cfg(ctx.settings, msg.keyword)
    who = _Sender(msg.sender_key, msg.sender_name)

    if not msg.is_dm:
        await _redirect_to_dm(ctx, cfg, who)
        return

    if not who.rid:
        await ctx.reply("Can't identify you (no key). Try again.")
        return

    try:
        # _dispatch uses blocking sqlite3. Keep that work off the event loop.
        reply = await asyncio.to_thread(_dispatch, cfg, _remainder(msg), who)
    except Exception as exc:
        await _report_error(ctx, cfg, who, exc)
        return

    await _send(ctx, reply)
