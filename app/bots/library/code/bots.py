"""Answers ``!bots``: the discovery command every bot on #bot / #bots implements.

The #bots etiquette is that bots should be discoverable, and the easiest way to
do that is one command all of them answer. This one introduces the node's bot,
lists the commands it currently takes, and points at ``help`` and
``source`` / ``author`` when those are enabled here — spelled with this node's
own command prefix, so the hint works as typed. ``!bots`` itself is answered
whatever the prefix and mention settings are (the engine's one exception).

Ships **enabled** (``enabled_by_default``) — unlike the rest of the library —
because answering ``!bots`` is the baseline courtesy of running a bot at all.
Like every built-in it is scoped to #bot / #bots + DMs.
"""

from remoteterm import bot

BOT_META = {
    "key": "bots",
    "name": "bots",
    "category": "Basic",
    "description": "Answers !bots in #bot / #bots: what this bot is and the commands it takes",
    "long_description": (
        "`bots` is the common discovery command every bot on #bot / #bots is expected to answer. "
        "The reply introduces this node's bot (the one-liner configured below, or a generic "
        "RemoteTerm introduction), lists the first keyword of every enabled command, and points at "
        "`help` and `source` / `author` when those are enabled. Enabled out of the box."
    ),
    "version": "1.0.0",
    "enabled_by_default": True,
    "cooldown_seconds": 10,
    "settings_schema": [
        {
            "key": "about",
            "label": "About this bot",
            "type": "text",
            "default": "",
            "help": "One line introducing your bot. Blank uses a generic RemoteTerm introduction.",
        },
    ],
    "settings": {"about": ""},
}

DEFAULT_ABOUT = "🤖 RemoteTerm bot"


@bot.on_keyword()
@bot.on_keyword("bots")
async def announce(ctx, msg):
    about = str(ctx.settings.get("about") or "").strip() or DEFAULT_ABOUT

    words = set()
    all_keywords = set()
    for entry in ctx.get_enabled_bots():
        keywords = entry.get("keywords") or []
        if keywords:
            words.add(keywords[0])
        all_keywords.update(keywords)

    parts = [about]
    if words:
        parts.append(f"Commands: {', '.join(sorted(words))}")
    # The node's own prefix: "!help" is useless advice on a node that wants "?help".
    prefix = ctx.command_prefix
    hints = []
    if "help" in all_keywords:
        hints.append(f"{prefix}help <command> for details")
    contact = [f"{prefix}{word}" for word in ("source", "author") if word in all_keywords]
    if contact:
        hints.append(f"{' / '.join(contact)} to reach the author")
    if hints:
        parts.append("; ".join(hints))
    # A node with many bots enabled overflows one frame — split, never truncate.
    await ctx.reply_split("\n".join(parts))
