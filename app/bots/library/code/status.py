"""Runtime status report. Seeded from meshcore-bot's status command."""

from remoteterm import bot

BOT_META = {
    "key": "status",
    "name": "status",
    "category": "Admin",
    "description": "Runtime status: radio, mesh counts, bot engine (DM, admins)",
    "long_description": (
        "`status` reports this installation's health: whether the radio is connected, how many "
        "contacts and repeaters are known, the 24-hour message count, and how many bots are "
        "enabled or failing to load. DM-only and admin-gated — it describes the server, not the "
        "mesh. Use it to check on a node you are not sitting in front of."
    ),
    "version": "1.1.1",
    "admin_only": True,
    "respond_to_dms": True,
}


@bot.on_keyword()
@bot.on_keyword("status")
async def status(ctx, msg):
    if not msg.is_dm:
        await ctx.reply(ctx.t("rt.dm_only"))
        return

    # Read-only introspection of this app's own runtime (allowed for admin bots).
    from app.bots.engine import bot_engine
    from app.services.radio_runtime import radio_runtime

    stats = await ctx.mesh_stats()
    radio = "connected" if radio_runtime.is_connected else "DISCONNECTED"
    enabled = sum(1 for b in bot_engine.bots.values() if b.record.enabled)
    erroring = sum(1 for b in bot_engine.bots.values() if b.record.enabled and b.load_error)
    engine_state = "disabled" if bot_engine.disabled else "running"

    await ctx.reply(
        f"Radio {radio} · {stats['total_contacts']} contacts / {stats['total_repeaters']} "
        f"repeaters · {stats['messages_24h']} msgs 24h"
    )
    await ctx.reply(
        f"Bot engine {engine_state}: {enabled}/{len(bot_engine.bots)} bots enabled"
        + (f", {erroring} failing to load" if erroring else "")
    )
