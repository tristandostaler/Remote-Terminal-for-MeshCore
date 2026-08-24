"""Command help and the command list. Seeded from meshcore-bot's help command.

``help`` lists every keyword this node answers to; ``help <command>`` explains
one. ``cmd`` / ``commands`` are aliases for the bare list.

Merged from the separate ``cmd`` bot (library 1.2.0): it printed the same list
with less detail, so the two bots duplicated each other and answering both
meant two replies.

``help <command>`` names every trigger the command answers to (library 1.3.0);
it used to stop after six, hiding operator-added keywords and the words merged
bots absorbed. The bare list marks how many extra triggers each command has
(``hello (+27)``) rather than spelling them out — a mesh with the greeter and
sudo bots enabled has ~150 keywords, four times the airtime of the plain list.
Operators who want them inline can switch on "Spell out aliases in the list".
"""

from remoteterm import bot

BOT_META = {
    "key": "help",
    "name": "help",
    "category": "Basic",
    "description": "Command list (help, cmd) and per-command help",
    "version": "1.3.0",
    "settings_schema": [
        {
            "key": "spell_out_aliases",
            "label": "Spell out aliases in the command list",
            "type": "bool",
            "default": False,
            "help": (
                "Off: the list shows one keyword per command with a (+n) alias count. "
                "On: every trigger is listed inline, which can multiply the airtime "
                "the list costs. 'help <command>' always names them all."
            ),
        },
    ],
    "settings": {"spell_out_aliases": False},
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

    spell_out = bool(ctx.settings.get("spell_out_aliases", False))
    commands: list[str] = []
    for entry in bots:
        keywords = _triggers(entry)
        if not keywords:
            continue
        aliases = keywords[1:]
        if not aliases:
            commands.append(keywords[0])
        elif spell_out:
            commands.append(f"{keywords[0]} ({'/'.join(aliases)})")
        else:
            commands.append(f"{keywords[0]} (+{len(aliases)})")
    commands.sort()
    if not commands:
        await ctx.reply("No keyword commands enabled — see the Bots tab")
        return
    # ctx.reply_split packs the whole list into as many RF-sized "(i/n)" parts as
    # it takes, so no command is dropped and no keyword is cut in half.
    await ctx.reply_split(f"Commands: {', '.join(commands)}")
    await ctx.reply("Say 'help <command>' for details — it names every trigger.")
