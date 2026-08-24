"""Lists the hashtag channels this node knows about."""

from remoteterm import bot

BOT_META = {
    "key": "channels",
    "name": "channels",
    "category": "Basic",
    "description": "Lists known hashtag channels",
    "version": "1.1.0",
}


@bot.on_keyword()
@bot.on_keyword("channels", "channel")
async def list_channels(ctx, msg):
    # Mesh introspection: read-only import of RemoteTerm's own channel
    # repository — an allowed exception to the stdlib-only import rule.
    from app.repository import ChannelRepository

    all_channels = await ChannelRepository.get_all()
    names = sorted(
        {"#" + ch.name.lstrip("#") for ch in all_channels if ch.is_hashtag and ch.name.strip()},
        key=str.lower,
    )
    if not names:
        await ctx.reply(ctx.t("rt.no_results"))
        return

    # ctx.reply_split packs the full list into as many RF-sized "(i/n)" parts as
    # it takes, so no channel is dropped and no name is cut in half.
    await ctx.reply_split(" ".join(names))
