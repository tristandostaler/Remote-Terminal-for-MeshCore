"""Answers ``!source`` and ``!author``: how to reach whoever runs this bot.

Running a bot is a responsibility, and the #bots etiquette asks that others can
contact its author. ``source`` leads with the source-code link, ``author`` with
the operator's contact; both carry the other half so either word is enough.

The contact is engine-wide — Bots › Engine › Author contact, next to the command
prefix — rather than a setting here, so it is set once and survives a reset.
``!author`` is answered whatever the prefix and mention settings are, like
``!bots`` (the engine's only two exceptions).

Ships **enabled** (``enabled_by_default``) — unlike the rest of the library —
because a bot nobody can reach is the thing the etiquette asks you not to run.
Like every built-in it is scoped to #bot / #bots + DMs.
"""

from remoteterm import bot

DEFAULT_SOURCE_URL = "https://github.com/tristandostaler/Remote-Terminal-for-MeshCore"
NO_CONTACT = "DM this node to reach its operator"

BOT_META = {
    "key": "source",
    "name": "source",
    "category": "Basic",
    "description": "Answers !source and !author: the source-code link and the operator's contact",
    "long_description": (
        "`source` replies with a link to the code this bot runs, and `author` with how to reach "
        "the person running it — both include the other half, so either word is enough. Set your "
        "email, callsign or mesh handle in Bots › Engine › Author contact; left blank, the reply "
        "asks people to DM this node. `!author` answers whatever the prefix. Enabled by default."
    ),
    "version": "1.0.0",
    "enabled_by_default": True,
    "cooldown_seconds": 10,
    "settings_schema": [
        {
            "key": "source_url",
            "label": "Source URL",
            "type": "text",
            "default": DEFAULT_SOURCE_URL,
            "help": "Where the code this bot runs can be read. Blank uses the RemoteTerm repository.",
        },
    ],
    "settings": {"source_url": DEFAULT_SOURCE_URL},
}


@bot.on_keyword()
@bot.on_keyword("source", "author")
async def contact(ctx, msg):
    url = str(ctx.settings.get("source_url") or "").strip() or DEFAULT_SOURCE_URL
    author = str(ctx.author_contact or "").strip() or NO_CONTACT
    source_part = f"Source: {url}"
    author_part = f"Author: {author}"
    if (msg.keyword or "").lower() == "author":
        text = f"🤖 {author_part} | {source_part}"
    else:
        text = f"🤖 {source_part} | {author_part}"
    await ctx.reply_split(text)
