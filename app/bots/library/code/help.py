"""Command help and the command list. Seeded from meshcore-bot's help command.

``help`` lists every keyword this node answers to; ``help <command>`` explains
one. ``cmd`` / ``commands`` are aliases for the bare list.

Merged from the separate ``cmd`` bot (library 1.2.0): it printed the same list
with less detail, so the two bots duplicated each other and answering both
meant two replies.
"""

from remoteterm import bot

BOT_META = {
    "key": "help",
    "name": "help",
    "category": "Basic",
    "description": "Command list (help, cmd) and per-command help",
    "version": "1.2.0",
}


@bot.on_keyword("help", "cmd", "commands")
async def show_help(ctx, msg):
    bots = ctx.get_enabled_bots()
    # Any keyword honours an argument, so operator-added aliases behave like
    # 'help' rather than depending on which word was matched.
    wanted = msg.arg_text.strip().lower()

    if wanted:
        for entry in bots:
            if wanted == entry["name"].lower() or wanted in entry["keywords"]:
                tries = ", ".join(entry["keywords"][:6]) or "(no keywords)"
                await ctx.reply_split(f"{entry['name']}: {entry['description']} — try: {tries}")
                return
        await ctx.reply(ctx.t("rt.no_results"))
        return

    keywords: list[str] = []
    for entry in bots:
        if entry["keywords"]:
            keywords.append(entry["keywords"][0])
    keywords.sort()
    if not keywords:
        await ctx.reply("No keyword commands enabled — see the Bots tab")
        return
    # ctx.reply_split packs the whole list into as many RF-sized "(i/n)" parts as
    # it takes, so no command is dropped and no keyword is cut in half.
    await ctx.reply_split(f"Commands: {', '.join(keywords)}")
    await ctx.reply("Say 'help <command>' for details.")
