"""The default scope for a new bot: which channels, and which rooms.

A bot answers commands, so the conversations it listens to are the conversations
it can annoy. Left at "everywhere", enabling one built-in makes the node reply on
Public, on every other channel it happens to carry, and in every room server it
is logged into. A fresh bot therefore starts scoped to the two conventional bot
channels (``#bot`` / ``#bots``) plus DMs, and to no room at all; the operator
widens it from the Settings tab.

Hashtag keys are derived from the name, identical on every node, so the channel
default names channels this node has not joined yet: joining ``#bot`` later
brings the bot to life there with no scope edit. Rooms have no such convention —
no ``#bot`` room to fall back on, and a room answer is public to everyone logged
in — so they start as an empty pick list and the operator names the rooms a bot
may speak in.
"""

from __future__ import annotations

from typing import Any

from app.channel_constants import BOT_CHANNEL_KEYS

# The literal the ``bots.scope`` column defaults to (see app/database.py). Kept
# next to the builder so the two are checked against each other by the tests.
DEFAULT_BOT_SCOPE_JSON = (
    '{"channels": {"only": ["EB50A1BCB3E4E5D7BF69A57C9DADA211", '
    '"0D24F5830B449668B8C221759B6C50D2"]}, "rooms": {"only": []}}'
)


def no_rooms() -> dict[str, Any]:
    """The "answer in no room" selection: an empty pick list.

    Spelled ``{"only": []}`` rather than ``"none"`` so the editor opens on
    "Only…" with nothing ticked — the operator sees a list to add rooms to, not a
    switch to flip. Also what the engine falls back to for a scope that never
    named rooms at all, so rooms are opt-in however the scope was written.
    """
    return {"only": []}


def default_bot_scope() -> dict[str, Any]:
    """A fresh default-scope dict (never a shared mutable constant)."""
    return {"channels": {"only": list(BOT_CHANNEL_KEYS)}, "rooms": no_rooms()}


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
