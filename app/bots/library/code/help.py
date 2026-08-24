"""Command help. Seeded from meshcore-bot's help command."""

from remoteterm import bot

BOT_META = {
    "key": "help",
    "name": "help",
    "category": "Basic",
    "description": "Command list and per-command help",
    "version": "1.1.0",
}


@bot.on_keyword("help")
async def show_help(ctx, msg):
    bots = ctx.get_enabled_bots()
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
    # ctx.reply_split packs the whole list into as many RF-sized "(i/n)" parts as
    # it takes, so no command is dropped and no keyword is cut in half.
    await ctx.reply_split(f"Commands: {', '.join(keywords)}")
    await ctx.reply("Say 'help <command>' for details.")
