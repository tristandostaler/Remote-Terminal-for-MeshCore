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

# How much of the list one command's aliases may spend. Alias sets that really
# are alternate names fit whole ("score/scores/wc/worldcup" is 24), but a few
# bots answer to a vocabulary instead: greeter takes 30 greetings, sudo 28 fake
# shell commands. Those words are the bot's subject matter rather than other
# names for the command, and in full they cost more airtime than the whole rest
# of the list — so those show a couple of examples and count the remainder.
ALIAS_INLINE_WIDTH = 40
ALIAS_EXAMPLES = 2

BOT_META = {
    "key": "help",
    "name": "help",
    "category": "Basic",
    "description": "Command list (help, cmd) and per-command help",
    "version": "1.4.0",
}


def _alias_hint(aliases):
    """Every alias if they fit, else a couple of examples and ``+n`` for the rest.

    ``hello (hi/hey/+28)`` says what the other 28 look like where a bare count
    said only that they exist; ``help <command>`` still names every one.
    """
    if not aliases:
        return ""
    whole = "/".join(aliases)
    if len(whole) <= ALIAS_INLINE_WIDTH:
        return whole
    shown = aliases[:ALIAS_EXAMPLES]
    return "/".join([*shown, f"+{len(aliases) - len(shown)}"])


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


@bot.on_keyword()
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
        aliases = _alias_hint(keywords[1:])
        commands.append(f"{keywords[0]} ({aliases})" if aliases else keywords[0])
    commands.sort()
    if not commands:
        await ctx.reply("No keyword commands enabled — see the Bots tab")
        return
    # The hint leads the list instead of following it as its own message: one
    # fewer transmission, and it arrives with the first part rather than after
    # however many the list took. ctx.reply_split packs the rest into as many
    # RF-sized "(i/n)" parts as it needs, so no command is dropped and no
    # keyword is cut in half; it breaks on the newline or a later space, never
    # inside the hint.
    await ctx.reply_split(f"Say 'help <command>' for details.\nCommands: {', '.join(commands)}")
