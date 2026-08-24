"""Command help and the command list. Seeded from meshcore-bot's help command.

``help`` lists every keyword this node answers to; ``help <command>`` explains
one. ``cmd`` / ``commands`` are aliases for the bare list.

Merged from the separate ``cmd`` bot (library 1.2.0): it printed the same list
with less detail, so the two bots duplicated each other and answering both
meant two replies.

Both screens name every trigger a command answers to (library 1.3.0). The list
used to print one keyword per bot and ``help <command>`` stopped after six, so
operator-added keywords and the words merged bots absorbed were unreachable
unless you already knew them. A node with the wordier bots enabled spends a few
more RF frames on the list; ctx.reply_split numbers them and drops nothing.
"""

from remoteterm import bot

# A handful of bots answer to a vocabulary rather than to alias names: greeter
# takes 30 greetings, sudo 28 fake shell commands. Those are the bot's subject
# matter, not other names for the command, and spelling them out costs more
# airtime than the whole rest of the list. Alias sets that really are alternate
# names top out around 24 characters ("score/scores/wc/worldcup"), so anything
# past this width shows its count instead and 'help <command>' spells it out.
ALIAS_INLINE_WIDTH = 40

BOT_META = {
    "key": "help",
    "name": "help",
    "category": "Basic",
    "description": "Command list (help, cmd) and per-command help",
    "version": "1.3.0",
}


def _triggers(entry):
    """Every keyword the bot answers to, in declared order, without repeats.

    A bot can register the same word from more than one handler (UI keywords
    route to every generic handler), so the raw list is not unique.
    """
    unique = []
    for keyword in entry.get("keywords") or []:
        if keyword not in unique:
            unique.append(keyword)
    return unique


@bot.on_keyword("help", "cmd", "commands")
async def show_help(ctx, msg):
    bots = ctx.get_enabled_bots()
    # Any keyword honours an argument, so operator-added aliases behave like
    # 'help' rather than depending on which word was matched.
    wanted = msg.arg_text.strip().lower()

    if wanted:
        for entry in bots:
            keywords = _triggers(entry)
            if wanted == entry["name"].lower() or wanted in keywords:
                tries = ", ".join(keywords) or "(no keywords)"
                await ctx.reply_split(f"{entry['name']}: {entry['description']} — try: {tries}")
                return
        await ctx.reply(ctx.t("rt.no_results"))
        return

    commands: list[str] = []
    for entry in bots:
        keywords = _triggers(entry)
        if not keywords:
            continue
        # Aliases ride in parentheses behind the first keyword, slash-separated
        # so they never read as more commands in the comma-separated list.
        aliases = "/".join(keywords[1:])
        if len(aliases) > ALIAS_INLINE_WIDTH:
            aliases = f"+{len(keywords) - 1}"
        commands.append(f"{keywords[0]} ({aliases})" if aliases else keywords[0])
    commands.sort()
    if not commands:
        await ctx.reply("No keyword commands enabled — see the Bots tab")
        return
    # ctx.reply_split packs the whole list into as many RF-sized "(i/n)" parts as
    # it takes, so no command is dropped and no keyword is cut in half.
    await ctx.reply_split(f"Commands: {', '.join(commands)}")
    await ctx.reply("Say 'help <command>' for details.")
