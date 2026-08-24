# Bots Workspace Architecture

The bots system replaces the fanout `bot` config type (migration 064) and merges
in meshcore-bot's command/service/scheduler/feed features as one engine. Bots
are Python scripts stored in the `bots` table, edited in the frontend Bots
workspace, and executed in-process — the same trust model the fanout bot editor
always had (`SECURITY.md` posture unchanged: trusted networks, trusted
operators).

## The pieces

- `api.py` — the authoring surface (`from remoteterm import bot` + `BotContext`).
  Decorators register handlers into a collector while `runtime.load_bot_code`
  exec()s the source. `BotContext` carries sends (`reply`/`send`/`send_dm`),
  image sends (`reply_image`/`send_image`/`send_dm_image`), `settings`,
  persistent `state`, `http` (httpx), `geocode`, i18n (`t`), `mesh_stats`,
  `get_enabled_bots`, logging. Test runs capture sends instead of transmitting.
  - **Image sends** take encoded bytes (anything Pillow opens — e.g. straight
    from `ctx.http`) or exactly 786,432 bytes of 512×512 packed RGB, and return
    how many messages it took. The image is stretched into a 512px square and
    AEIC-encoded to ~150 bytes, then framed as `aei1:` text chunks that go out
    through the ordinary `_dispatch_send`, so they obey the same TX spacing,
    moderation and test-capture rules as any reply. Needs the optional `aeic`
    extra and the downloaded model on this server; raises naming the missing
    piece otherwise. See `app/imaging/aeic/AGENTS_aeic.md`.
- `runtime.py` — load/validate source. Two styles: decorated handlers, or a
  legacy module-level `def bot(...)` (auto-wrapped; executed via the original
  `app/fanout/bot_exec.execute_bot_code`, so migrated bots behave identically).
- `engine.py` — the singleton `bot_engine`. Fed `message` and `contact` events
  from `websocket.broadcast_event` (same tap as the fanout bus). Keyword
  triggers pass banned/hops/scope/prefix/mention/admin gates plus global,
  per-user, and per-bot cooldown limiters (with a queue window); catch-all
  `on_message` handlers and legacy bots see every in-scope message and filter
  themselves. One 15s ticker drives bot cron triggers, the `bot_schedules`
  table, and `bot_feeds` polling. All sends serialize behind one TX-spacing
  lock (engine settings). Runs are recorded to `bot_runs`; logs go to a ring
  buffer + `bot_log` WS events.
- `cron.py` — dependency-free 5-field crontab (+`@presets`). **Day-of-week is
  0=Monday** (APScheduler numbering, meshcore-bot compatible), and
  dom+dow-both-restricted uses standard cron OR semantics.
- `feeds.py` — RSS 2.0/Atom + JSON API polling, `{field|filter:arg}` format
  templates, SSRF guard (private/loopback hosts refused), first-check-only
  marks position (no history flood).
- `translate.py` — 10 locales in `translations/` (ported from meshcore-bot),
  dotted-key lookup with locale fallback, keyword-based language detection.
- `moderation.py` — banned senders (prefix match) + outgoing profanity filter.
- `app/bot_scope.py` (top level, next to `channel_constants.py`) — the default
  channel scope: `#bot` / `#bots` plus DMs. Hashtag keys are
  `SHA256(name)[:16]`, so the default names those channels even on a node that
  has not joined them — joining `#bot` later brings every default-scoped bot to
  life there with no scope edit. Spelled out in four places that must agree:
  the derived keys, `DEFAULT_BOT_SCOPE_JSON` (the `bots.scope` column default in
  `app/database.py` — SQL cannot import Python), `default_bot_scope()`, and
  `frontend/src/utils/botScope.ts`. `tests/test_bot_default_scope.py` asserts
  they do.
- `placeholders.py` — `{total_contacts}`-style tokens for scheduled messages.
- `library/` — built-in bots as real `.py` files under `library/code/`, each
  self-describing via a module-level `BOT_META` dict (metadata +
  `settings_schema`). Seeded at startup (`ensure_seeded`): inserts are
  **disabled by default**; unmodified built-ins refresh on version bumps;
  operator-modified ones are never touched. "Reset to default" restores from
  the shipped file.
  - **Deleting a library file is not enough to remove a bot.** Seeding never
    deletes, and keyword dispatch runs *every* enabled bot that matches, so a
    left-behind row answers alongside whatever replaced it — two replies to one
    command — and never updates again. Merging or dropping a built-in means
    adding its key to `MERGED_BOTS` so `retire_merged_bots()` (runs right after
    seeding, idempotent) folds it in: a pristine row is deleted and its
    `enabled` flag moves to the survivor; a row the operator edited, gave
    custom triggers, or configured is kept but disabled, renamed
    `(retired) …`, and has `builtin_key` cleared. Note `modified` is set only
    when the *code* changes, so triggers and settings are checked separately.

## Data model

`bots` (code, settings_schema, settings, scope, limits, ui_triggers, state,
builtin lineage), `bot_runs` (bounded history, feeds the dashboard),
`bot_schedules` (standalone cron messages), `bot_feeds`, and the singleton
`bot_engine_settings` (prefix, mention mode, rate limits, language, moderation,
admin users). Repository: `app/repository/bots.py`.

## API

`/api/bots` CRUD + `/library`, `/engine` (GET/PATCH),
`/engine/disable-until-restart`, `/logs`, `/stats?window=`, `/runs`,
`/{id}/test` (sandboxed run), `/{id}/reset`, `/schedules/*`, `/feeds/*`, and
`POST /api/hooks/{slug}` for `@bot.on_webhook` (gated on the bot's
`webhook_token` setting). Route-order gotcha: fixed paths that share the
`POST /<segment>/test` shape must be registered before `POST /{bot_id}/test`.

Password-typed settings are write-only: bot API responses contain a redaction
sentinel, and sending that sentinel back preserves the stored credential.
Generated callback URLs therefore require the operator to re-enter a secret
before copying it; the original value is never returned to the browser.

All `/api/hooks/*` routes bypass optional app-wide Basic Auth because providers
cannot supply it; each enabled hook still requires its bot-specific token.
SMS accepts a query token for VoIP.ms compatibility, and access/debug logging
redacts it. Twilio callbacks additionally require `X-Twilio-Signature`, checked
against the configured Auth Token and the exact public callback URL. Reverse
proxies must preserve the public scheme, host, path, and query string used by
Twilio or configure forwarding so FastAPI reconstructs that same URL. VoIP.ms
does not offer an equivalent callback-signature mechanism, so it retains the
token gate only.

## Invariants worth keeping

- Legacy `def bot(**kwargs)` sources must keep running unchanged (migration
  064 moved them here verbatim).
- Seeded bots ship disabled — enabling what a node answers is an operator act.
- New and seeded bots are scoped to `#bot` / `#bots` + DMs, never "all
  channels": a command bot on Public is noise for the whole mesh. A built-in may
  widen its own default via `BOT_META["scope"]`, but nothing may default to
  `{"channels": "all"}`. Migration 071 retargeted existing bots that were still
  at the old "all" default **and still disabled** — an enabled or hand-scoped
  bot is a decision and was left alone.
- Newly installed SMS bots are `admin_only`; existing installations retain
  their stored permission flag during version refreshes.
- `ui_triggers` only feed handlers declared with **no-argument** decorators
  (`@bot.on_keyword()` / `@bot.on_cron()`); code-declared triggers are derived
  at load time and never stored.
- The engine never raises into `broadcast_event` — every handler run is
  wrapped, recorded, and logged.
- Frontend: the Bots view lives at `#bots` / `#bots/{botId}`
  (`frontend/src/components/bots/`), live logs ride the module-level
  `stores/botLogStore.ts` exactly like raw packets (never lift into App state).
