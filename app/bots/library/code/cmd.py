"""Lists the keyword commands this node currently answers to."""

from remoteterm import bot

BOT_META = {
    "key": "cmd",
    "name": "cmd",
    "category": "Basic",
    "description": "Lists enabled keyword commands",
    "version": "1.1.0",
}


@bot.on_keyword("cmd", "commands")
async def list_commands(ctx, msg):
    names = sorted({b["name"] for b in ctx.get_enabled_bots() if b["keywords"]}, key=str.lower)
    if not names:
        await ctx.reply("No keyword commands enabled — see the Bots tab")
        return

    # ctx.reply_split packs the full list into as many RF-sized "(i/n)" parts as
    # it takes, so nothing is dropped and nothing is cut mid-name.
    await ctx.reply_split(", ".join(names))
