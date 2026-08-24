"""The built-in bot library: seeding and reset.

Every file in ``library/code/*.py`` is one seedable bot. Files are never
imported — their source text is stored into the ``bots`` table and executed by
the runtime like any user bot. Each must declare a module-level ``BOT_META``
dict (read by exec'ing the source through the normal loader):

    BOT_META = {
        "key": "wx",                  # stable identity (builtin_key)
        "name": "wx",                 # default display name
        "category": "Weather",
        "description": "...",
        "version": "1.0.0",
        "settings_schema": [...],      # optional; drives the Settings tab
        "settings": {...},             # optional defaults for ctx.settings
        "respond_to_dms": True,        # optional (default True)
        "admin_only": False,           # optional
        "cooldown_seconds": 0,         # optional
        "per_user_cooldown_seconds": 0,
        "queue_threshold_seconds": 0,
    }

Seeding is additive and non-destructive: a bot the operator modified
(``modified = 1``) is never touched; an unmodified built-in is refreshed when
the library ships a newer ``version``. All seeded bots start **disabled** —
enabling what a node answers to is the operator's call.

Bots that were *merged* into another bot are handled by ``retire_merged_bots``,
which runs right after seeding — see ``MERGED_BOTS``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CODE_DIR = Path(__file__).parent / "code"


class LibraryError(ValueError):
    """Raised when a library code file is malformed."""


def _extract_meta(source: str, filename: str) -> dict[str, Any]:
    from app.bots.runtime import BotCodeError, load_bot_code

    try:
        loaded = load_bot_code(source)
    except BotCodeError as exc:
        raise LibraryError(f"{filename}: {exc}") from exc
    meta = loaded.namespace.get("BOT_META")
    if not isinstance(meta, dict):
        raise LibraryError(f"{filename}: missing module-level BOT_META dict")
    for required in ("key", "name", "category", "description", "version"):
        if not meta.get(required):
            raise LibraryError(f"{filename}: BOT_META.{required} is required")
    return meta


def list_library() -> list[dict[str, Any]]:
    """Return ``[{meta..., code}]`` for every library file, sorted by key."""
    entries: list[dict[str, Any]] = []
    if not CODE_DIR.exists():
        return entries
    for path in sorted(CODE_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue
        source = path.read_text(encoding="utf-8")
        try:
            meta = _extract_meta(source, path.name)
        except LibraryError as exc:
            logger.error("Skipping library bot: %s", exc)
            continue
        entries.append({**meta, "code": source})
    return entries


def get_library_entry(builtin_key: str) -> dict[str, Any] | None:
    for entry in list_library():
        if entry["key"] == builtin_key:
            return entry
    return None


# Built-in bots that were merged into another bot: retired key -> surviving key.
# Seeding never deletes, and the engine dispatches to *every* enabled bot whose
# keyword matches, so a retired row left in place would answer alongside its
# survivor — two replies to one command — and would never be updated again.
MERGED_BOTS = {
    "test": "ping",
    "cmd": "help",
    "roll": "dice",
    "worldcup": "sports",
    "worldcup_live": "sports",
    "hfcond": "solar",
    "aurora": "solar",
    "joke": "fun",
    "dadjoke": "fun",
    "catfact": "fun",
    "funfact": "fun",
    "fortunes": "fun",
    "magic8": "fun",
}

# Settings worth carrying to the survivor: retired key -> {old name: new name}.
MERGED_SETTINGS = {
    "worldcup_live": {"channel": "live_channel"},
}

# What the library shipped as each retired bot's settings. A row still holding
# these has not been configured, so nothing is lost by deleting it.
_RETIRED_STOCK_SETTINGS = {
    "worldcup_live": {"channel": ""},
}


def _is_pristine(record: Any, builtin_key: str) -> bool:
    """True when the operator never touched this bot.

    ``modified`` is set only when the *code* is edited, so custom triggers and
    changed settings have to be checked separately or an operator's work would
    be deleted silently.
    """
    if record.modified or record.ui_triggers:
        return False
    return dict(record.settings) == _RETIRED_STOCK_SETTINGS.get(builtin_key, {})


async def _retire(record: Any, survivor: Any) -> None:
    """Keep an operator-customized retired bot, minus its keyword clash."""
    from app.repository.bots import BotRepository

    name = record.name or "retired bot"
    if not name.startswith("(retired)"):
        name = f"(retired) {name}"
    candidate, suffix = name, 2
    while await BotRepository.name_exists(candidate, exclude_id=record.id):
        candidate = f"{name} {suffix}"
        suffix += 1
    # builtin_key is cleared so seeding never claims this row again; the code
    # and settings stay exactly as the operator left them.
    await BotRepository.update(
        record.id, enabled=False, modified=True, builtin_key=None, name=candidate
    )
    survivor_name = survivor.name if survivor is not None else "its replacement"
    logger.info(
        "Retired customized built-in %r as %r (merged into %s)",
        record.name,
        candidate,
        survivor_name,
    )


async def retire_merged_bots() -> int:
    """Fold merged-away built-ins into their survivors. Returns rows touched.

    Runs after seeding so every survivor row already exists, and is idempotent:
    once a retired row is gone or unclaimed, later runs do nothing.
    """
    from app.repository.bots import BotRepository

    changed = 0
    for retired_key, survivor_key in MERGED_BOTS.items():
        record = await BotRepository.get_by_builtin_key(retired_key)
        if record is None:
            continue
        survivor = await BotRepository.get_by_builtin_key(survivor_key)

        # The merged commands only keep answering if the survivor is enabled.
        if record.enabled and survivor is not None and not survivor.enabled:
            await BotRepository.update(survivor.id, enabled=True)
            logger.info(
                "Enabled %r because the merged-away %r was enabled", survivor.name, record.name
            )
            survivor = await BotRepository.get_by_builtin_key(survivor_key)

        if survivor is not None:
            renames = MERGED_SETTINGS.get(retired_key) or {}
            carried = {
                new: record.settings[old]
                for old, new in renames.items()
                if record.settings.get(old) not in (None, "")
            }
            if carried:
                await BotRepository.update(
                    survivor.id, settings={**dict(survivor.settings), **carried}
                )

        if _is_pristine(record, retired_key):
            await BotRepository.delete(record.id)
        else:
            await _retire(record, survivor)
        changed += 1

    if changed:
        logger.info("Bot library retired %d merged-away bot(s)", changed)
    return changed


async def ensure_seeded() -> int:
    """Insert missing library bots; refresh unmodified ones on version bumps.

    Returns the number of rows inserted or refreshed.
    """
    from app.repository.bots import BotRepository

    changed = 0
    for entry in list_library():
        existing = await BotRepository.get_by_builtin_key(entry["key"])
        if existing is None:
            name = entry["name"]
            suffix = 2
            while await BotRepository.name_exists(name):
                name = f"{entry['name']} {suffix}"
                suffix += 1
            await BotRepository.create(
                name=name,
                category=entry["category"],
                description=entry["description"],
                code=entry["code"],
                enabled=False,
                admin_only=bool(entry.get("admin_only", False)),
                respond_to_dms=bool(entry.get("respond_to_dms", True)),
                scope=entry.get("scope"),
                cooldown_seconds=float(entry.get("cooldown_seconds", 0)),
                per_user_cooldown_seconds=float(entry.get("per_user_cooldown_seconds", 0)),
                queue_threshold_seconds=float(entry.get("queue_threshold_seconds", 0)),
                settings_schema=entry.get("settings_schema") or [],
                settings=entry.get("settings") or {},
                builtin_key=entry["key"],
                builtin_version=entry["version"],
            )
            changed += 1
        elif not existing.modified and existing.builtin_version != entry["version"]:
            await BotRepository.update(
                existing.id,
                code=entry["code"],
                description=entry["description"],
                category=entry["category"],
                settings_schema=entry.get("settings_schema") or [],
                builtin_version=entry["version"],
            )
            changed += 1
    if changed:
        logger.info("Bot library seeding applied %d change(s)", changed)
    # After seeding, so every survivor row exists before anything is folded in.
    changed += await retire_merged_bots()
    return changed
