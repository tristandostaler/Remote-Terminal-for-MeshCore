"""Mesh statistics digest. Seeded from meshcore-bot's stats command."""

from remoteterm import bot

BOT_META = {
    "key": "stats",
    "name": "stats",
    "category": "Mesh",
    "description": "24h mesh statistics digest",
    "long_description": (
        "`stats` sends a two-line digest of the last 24 hours — messages seen, nodes heard, "
        "repeaters up, new contacts this week — followed by the totals this node knows about. It "
        "reads the app's own database, so it answers immediately and adds no mesh traffic beyond "
        "the reply. A good pulse check to leave enabled on a bot channel."
    ),
    "version": "1.1.1",
}


@bot.on_keyword()
@bot.on_keyword("stats")
async def mesh_stats(ctx, msg):
    stats = await ctx.mesh_stats()
    await ctx.reply(
        f"Mesh 24h: {stats['messages_24h']} msgs · {stats['contacts_24h']} nodes heard · "
        f"{stats['repeaters_24h']} repeaters up · {stats['new_contacts_7d']} new this week"
    )
    await ctx.reply(
        f"Known: {stats['total_contacts']} contacts, {stats['total_repeaters']} repeaters"
    )
