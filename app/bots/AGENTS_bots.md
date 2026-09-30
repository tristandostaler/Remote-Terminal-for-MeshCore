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
  exec()s the source. `BotContext` carries sends
  (`reply`/`send`/`send_dm`/`send_room`), image sends
  (`reply_image`/`send_image`/`send_dm_image`/`send_room_image`), `settings`,
  persistent `state`, `http` (httpx), `geocode`, i18n (`t`), `mesh_stats`,
  `get_enabled_bots`, logging, and `sender_is_admin` (the engine's Admin users
  check, the same one `admin_only` gates on) so a bot can keep raw diagnostics
  for admins in DMs, and `reply_budget()` (bytes one reply message can carry,
  as `reply_split` sizes it). Test runs capture sends instead of transmitting.
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
- **Rooms.** A room-server post reaches us as a DM *from the room's contact*,
  with the author carried in the signed sender prefix (`dm_ingest` resolves it
  into `sender_key`/`sender_name`). `_build_message` looks the conversation
  contact up and, when it is `CONTACT_TYPE_ROOM`, hands the handler
  `is_room=True` + `room_key`/`room_name` with **`is_dm` False and
  `channel_key` None** — a room is its own conversation kind, so a DM-only bot
  (`if not msg.is_dm: ...`) stays out of rooms and a channel-scoped one is not
  silenced by its allow-list. `ctx.reply` answers **into the room** (an ordinary
  DM send to the room contact, full 156-byte budget, no `"<name>: "` framing),
  never to the author: the room asked, the room gets the answer, and a poster we
  only know by key prefix would not be DM-able anyway. The gate is `scope.rooms` — the same
  `all` / `none` / `{only|except: [keys]}` shape as `scope.channels`, over room
  contact keys instead of channel keys, so an operator can answer in one room and
  ignore another. It is separate from `respond_to_dms` because a room reply is
  public to everyone logged in. A scope with **no `rooms` key means no room**
  (rooms are opt-in; scopes written before rooms existed say nothing about
  them, and `no_rooms()` is the fallback), and both halves
  go through one `_selection_allows` matcher, which compares case-insensitively:
  channel keys are stored upper-case and room contact keys lower. Posts whose `sender_key` is *our* node are
  dropped — a room relays every post to every member, us included, so reacting
  to our own reply is how two bots end up answering each other forever. Room
  posts arrive in **bursts**: the room poll logs in and drains everything posted
  since the last sync, so a first sync with a busy room feeds the engine a
  backlog all at once. Only the global/per-user limiters stand between that and
  a run of stale replies.
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
  they do. The same module holds `no_rooms()`, the empty room pick list every
  layer defaults to.
- `llm.py` — the `tinyllm` bot's tiny on-device LLM runtime (optional `llm` extra,
  `llama-cpp-python`). Owns the model catalog the bot's Settings dropdown is
  generated from (label + per-option `description` with download size and RAM;
  the editor shows the chosen option's `description` under any `select`), and
  the process-wide `llm_runtime` singleton — bot code is re-exec'd on every
  settings save, so a model held in the bot's namespace would be reloaded each
  time. Download + load always run in a background thread (a first download
  dwarfs the 10 s `BOT_EXECUTION_TIMEOUT`); a run only starts it and reports
  progress. Generation streams tokens and stops at a deadline (≤ 7 s) so the
  reply still goes out inside the timeout; one answer at a time.
  **Memory** (measured, SmolLM2 135M Q8: ~200 MB, ~45 MB unreclaimable): weights
  stay memory-mapped (reclaimable page cache), `n_ctx` 512 / `n_batch` 64, and
  `_check_memory` refuses a load when `available_memory_mb()` (min of
  `MemAvailable` and the cgroup limit headroom) is below file + 64 + 128 MB.
  `n_threads_batch = n_threads` (default half the cores).
  **The model runs in a child process** (`_ModelProcess` →
  `python -m app.bots.llm --worker`, JSON lines over stdin/stdout, one reply per
  request; `_worker_main` moves fd 1 to stderr so native prints can't corrupt
  the protocol). The child sets its own `oom_score_adj` to 1000, so the kernel
  kills the model rather than the radio server; EOF/silence past
  deadline + 3 s raises `LlmWorkerDiedError` with a plain reason
  (`_exit_reason`: -9 = out of memory, -4 = illegal instruction) and puts the
  runtime in `error` (retried after `RETRY_AFTER_SECONDS`). The server never
  imports `llama_cpp` (`find_spec` only). **Idle unload**: a daemon reaper
  (`unload_if_idle`, every 10 s) stops the child after `unload_after_minutes`
  unused (0 = after every answer; never mid-answer — it only takes the
  generate lock non-blocking); state goes `idle` with `unloaded`, and the next
  `ensure` reloads. The bot waits `RELOAD_WAIT_SECONDS` (4 s) for a reload and
  takes the elapsed time out of the answer deadline (`RUN_BUDGET_SECONDS`
  7.5 s), so a reload still answers in the same run.
  **DM memory** (`DmSessions`, `dm_sessions` in `llm.py`): per-sender
  question/answer turns, DMs only, `history_messages` of them (default 10)
  sent before the new question; trimmed to `HISTORY_MAX_CHARS` and to start
  with a user turn; forgotten after `SESSION_IDLE_SECONDS` (1 h), `ask
  reset`/`forget`, or a restart. Deliberately in memory, not the bot's
  persisted `state`: runs are concurrent and each saves its own copy of
  `state`, so concurrent askers would overwrite each other's turns. A prompt
  over the context window with history is retried once without it.
  Weight **repacking is off** (`_weight_repacking` wraps
  `llama_model_default_params` during the load to set `use_extra_bufts=False`;
  `Llama()` has no argument for it): on ARM with dotprod (Pi 5) llama.cpp
  otherwise keeps a second, anonymous copy of Q8_0/Q4_0/Q4_K weights next to
  the mmapped file. The `fast_arm_layout` setting re-enables it and
  `_check_memory` then counts the weights twice. `ensure` keys the loaded model
  on (spec, threads, repack), so changing either setting reloads.
  `run.sh` caps the llama.cpp compile at one job per ~800 MB free (measured
  ~700 MB per compiler process), niced.
  Docker: `MESHCORE_ENABLE_LLM=true` makes `run.sh` compile llama-cpp-python
  in the background *after* the server is up (`uv sync --inexact`, never
  before `exec`: a Pi takes 10-20 min), leaving `.installing` /
  `.install-failed` / `.install.log` in the model dir; `_missing_package_reason`
  reads them (`.installing` holds `phase` = waiting / installing / compiling,
  `started`, and the job's `pid`: `_describe_install` reports elapsed time and
  calls a marker whose PID is gone "stopped", never "still installing"; a
  missing package is rechecked every question, no retry pause). Errors are
  two-level: `LlmUserError` messages are written for the mesh; anything else
  is summarized by `_plain_reason` and its raw text kept in `error_detail`,
  which the bot shows only to an admin in a DM (`describe(detailed=True)`).
  llama.cpp is built with OpenMP, so `libgomp1` must be present at runtime:
  the Dockerfile installs it and `run.sh`'s `llm_runtime_libs` repairs older
  images. `importlib.invalidate_caches()` lets the next retry import
  the package without a restart. Every `uv sync` in `run.sh` names all extras
  wanted or already present, because a sync removes the ones it isn't told of.
- `placeholders.py` — `{total_contacts}`-style tokens for scheduled messages.
- `library/` — built-in bots as real `.py` files under `library/code/`, each
  self-describing via a module-level `BOT_META` dict (metadata +
  `settings_schema`). Both descriptions are required: `description` is the one
  line the bots list shows, `long_description` the 3-5 lines the editor's
  Settings tab shows under it. Seeding backfills an empty `long_description`
  without a version bump (only an empty one — never over an operator's text). Seeded at startup (`ensure_seeded`): inserts are
  **disabled by default** unless `BOT_META["enabled_by_default"]` is true
  (only `bots` and `source`, the #bots etiquette commands; insert-time only); unmodified built-ins refresh on version bumps;
  operator-modified ones are never touched. "Reset to default" restores from
  the shipped file.
  - **Settings are never rewritten by a refresh — unless the bot asks.** A
    version refresh replaces code, schema and descriptions but keeps the stored
    `settings`, so a setting whose meaning changes leaves the Settings tab and
    the bot disagreeing. A library bot may define a module-level
    `migrate_settings(settings) -> settings`; `ensure_seeded` runs it on the
    stored settings at refresh (`_migrated_settings`, never raises) and saves
    the result. Call it at run time too, so a row that was not refreshed yet
    behaves the same. First user: tinyllm's free-text prompt becoming a
    "match the model / custom" choice (1.2.0).
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

`bots` (code, settings_schema, settings, `scope` = `{channels, rooms}` plus the
`respond_to_dms` flag, limits, ui_triggers, state, builtin lineage),
`bot_runs` (bounded history, feeds the dashboard),
`bot_schedules` (standalone cron messages), `bot_feeds`, and the singleton
`bot_engine_settings` (prefix, mention mode, rate limits, language, moderation,
admin users, author contact). Repository: `app/repository/bots.py`.

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
  064 moved them here verbatim). Their signature has no room of its own and its
  two kinds are "DM" and "everything else", so `call_legacy` hands a room post
  over as its room: `channel_key`/`channel_name` carry the room contact rather
  than `None`. A bot written before rooms reads `not is_dm` as "`channel_key` is
  a string", and `execute_bot_code` swallows the TypeError — the bot would just
  go quiet, with nothing in Bots › Logs. Decorated bots keep `channel_key` None
  on purpose: they have `msg.room_key`, and `ctx.send` must never mistake a room
  for a channel.
- Seeded bots ship disabled — enabling what a node answers is an operator act.
  The one exception is the #bots etiquette pair, `bots` (answers `!bots`:
  what this bot is and its commands) and `source` (`!source` / `!author`: code
  link and operator contact). Every bot on #bot / #bots is expected to answer
  those, so they carry `enabled_by_default: True`, stay on the default
  #bot / #bots + DMs scope, and have a 10 s per-bot cooldown. The flag is read
  only when the row is inserted: an operator who disables either is never
  re-enabled by seeding or a version refresh. Keep that list short — the flag
  is for courtesy commands, not features.
  - **`!bots` and `!author` always answer.** `UNIVERSAL_COMMAND_RE` in
    `engine.py` lets a literal `!bots` / `!author` count as prefixed whatever
    `command_prefix` is (another symbol, several, or empty), and
    `is_universal_command` skips the `require_prefix` and `mention_mode` gates
    for them, so the etiquette commands work on every node however it is
    configured. Scope, the enabled flag, `admin_only` and the rate limits still
    apply. They are the only exceptions: `!ping` or `!source` on a `?` node
    still do nothing.
  - **The author contact is an engine setting** (`bot_engine_settings.author_contact`,
    migration 092; Bots › Engine › Author contact, next to the prefix), handed
    to every run as `BotContext.author_contact`. Engine-wide so it is set once
    and survives a bot reset; the `source` bot has no contact setting of its own.
  - `BotContext.command_prefix` is the node's first configured prefix (`""`
    when none), so a reply can spell commands the way they are typed here —
    the `bots` bot's `help` / `source` / `author` hints use it.
  - **The `bots` bot cannot be deleted**, only disabled.
    `UNDELETABLE_BUILTINS` (`library/__init__.py`) makes `DELETE /api/bots/{id}`
    answer 403, and the derived `Bot.deletable` field hides the editor's Delete
    button. Protection is keyed on `builtin_key`, which the API never lets an
    operator change. `source` stays deletable.
- New and seeded bots are scoped to `#bot` / `#bots` + DMs, never "all
  channels": a command bot on Public is noise for the whole mesh. A built-in may
  widen its own default via `BOT_META["scope"]`, but nothing may default to
  `{"channels": "all"}`. Migration 071 retargeted existing bots that were still
  at the old "all" default **and still disabled** — an enabled or hand-scoped
  bot is a decision and was left alone.
- Rooms are **opt-in**, like channels but stricter: `default_bot_scope()` ships
  `rooms: {"only": []}` and the engine reads a missing key as the same thing
  (`no_rooms()`), so a bot answers in the rooms the operator named and in no
  other. There is no `#bot` convention to fall back on, and unlike a DM the
  answer is public to everyone logged into the room. `{"only": []}` rather than
  `"none"` because the editor then opens on "Only…" with nothing ticked — a list
  to add to, not a switch to flip. Migration 080 writes the empty list onto
  scopes that predate rooms, which only makes the stored scope say out loud what
  the engine already reads it as. **081 exists because 080 shipped once with the
  opposite rule** — it inherited `respond_to_dms`, so a node that upgraded during
  that build stores `"rooms": "all"`, and the runner never re-runs a migration
  whose number the database has recorded. Editing a shipped migration only helps
  databases that have not seen it; changing one's mind needs a new number.
- Newly installed SMS bots are `admin_only`; existing installations retain
  their stored permission flag during version refreshes.
- `ui_triggers` only feed handlers declared with **no-argument** decorators
  (`@bot.on_keyword()` / `@bot.on_cron()`); code-declared triggers are derived
  at load time and never stored. Every library bot that has a command therefore
  carries **exactly one** bare `@bot.on_keyword()`, on its primary handler and
  written *above* the declared decorator — the editor's "Extra keywords" box is
  always shown, so a bot without one accepts keywords that silently never fire.
  A bot with no command (cron/webhook/event only) must not have one: there would
  be nothing for the word to answer. Keep the count at one per bot — two generic
  handlers are both fed the same word and the first registered wins.
  `LoadedBot.keyword_map` drops a UI keyword the code already declares, so an
  extra keyword can never answer for a command the code owns
  (`tests/test_bots_library_ports.py::TestGenericKeywordHandler` pins all of it).
- **Editing a `library/code/*.py` file means bumping its `BOT_META["version"]`,
  in the same commit.** `ensure_seeded` refreshes an unmodified row only when
  `builtin_version != entry["version"]` — it compares versions, never code — so
  an edit shipped without a bump reaches new installs and *no existing one*,
  permanently. This is easy to miss on a sweep across many bots: the library-wide
  change that added the bare `@bot.on_keyword()` to 35 files shipped with zero
  bumps and had to be followed by a bump-only commit.
  `tests/test_bots_library_schema.py::test_a_version_bump_is_what_delivers_new_code`
  asserts both halves. Additive, behaviour-preserving edits take the minor slot.
- The engine never raises into `broadcast_event` — every handler run is
  wrapped, recorded, and logged.
- Frontend: the Bots view lives at `#bots` / `#bots/{botId}`
  (`frontend/src/components/bots/`), live logs ride the module-level
  `stores/botLogStore.ts` exactly like raw packets (never lift into App state).
