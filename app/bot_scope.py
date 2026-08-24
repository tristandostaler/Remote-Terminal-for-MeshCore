"""The default channel scope for a bot.

A bot answers commands, so the channels it listens to are the channels it can
annoy. Left at "every channel", enabling one built-in makes the node reply on
Public and on every other channel it happens to carry — which is why a fresh
bot starts scoped to the two conventional bot channels (``#bot`` / ``#bots``)
plus DMs, and the operator widens it from the Settings tab.

Hashtag keys are derived from the name, identical on every node, so the default
names channels this node has not joined yet: joining ``#bot`` later brings the
bot to life there with no scope edit.
"""

from __future__ import annotations

from typing import Any

from app.channel_constants import BOT_CHANNEL_KEYS

# The literal the ``bots.scope`` column defaults to (see app/database.py). Kept
# next to the builder so the two are checked against each other by the tests.
DEFAULT_BOT_SCOPE_JSON = (
    '{"channels": {"only": ["EB50A1BCB3E4E5D7BF69A57C9DADA211", '
    '"0D24F5830B449668B8C221759B6C50D2"]}}'
)


def default_bot_scope() -> dict[str, Any]:
    """A fresh default-scope dict (never a shared mutable constant)."""
    return {"channels": {"only": list(BOT_CHANNEL_KEYS)}}


def is_default_bot_scope(scope: Any) -> bool:
    """True when ``scope`` selects exactly the default bot channels."""
    if not isinstance(scope, dict):
        return False
    channels = scope.get("channels")
    if not isinstance(channels, dict):
        return False
    only = channels.get("only")
    if not isinstance(only, list) or "except" in channels:
        return False
    return {str(key).upper() for key in only} == set(BOT_CHANNEL_KEYS)
