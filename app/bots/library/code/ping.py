"""Liveness check and signal report: replies with the path the message took.

``ping`` and ``test`` are the same command — the reply confirms the node is
alive and describes how the message arrived: hop count and hop ids (or
"Direct"), the region scope when the message was scoped, and the sender's clock
offset when it is far enough off to matter.

Merged from the separate ``ping`` and ``test`` bots (library 1.2.0): they were
two names for one liveness check, so enabling one and not the other was a
coin flip, and enabling both answered twice.
"""

import time
from datetime import datetime

from remoteterm import bot

BOT_META = {
    "key": "ping",
    "name": "ping",
    "category": "Basic",
    "description": "Liveness check with signal report: hops, path, region, clock offset",
    "version": "1.3.0",
}

# Below this the two clocks are close enough that reporting the delta is noise.
CLOCK_OFFSET_THRESHOLD_SECONDS = 120


@bot.on_keyword()
@bot.on_keyword("ping", "test")
async def signal_report(ctx, msg):
    parts = []
    path = (msg.path or "").strip()
    current_time = datetime.now().strftime("%H:%M")
    if path:
        width = max(1, int(msg.path_bytes_per_hop or 1)) * 2
        hops = [path[i : i + width] for i in range(0, len(path), width)]
        noun = "hop" if len(hops) == 1 else "hops"
        parts.append(f"Via {len(hops)} {noun}: {'→'.join(hops)}")
    else:
        parts.append("Direct (no path)")
    if msg.scoped and msg.region:
        parts.append(f"region {msg.region}")
    if msg.sender_timestamp:
        offset = int(time.time()) - int(msg.sender_timestamp)
        if offset >= CLOCK_OFFSET_THRESHOLD_SECONDS:
            parts.append(f"clock offset {offset:+d}s")
    sender_name_to_use = msg.sender_name if msg.sender_name else ""
    # @[name] is the mention syntax mesh clients recognize and highlight.
    prefix = f"🤖 Copy, @[{sender_name_to_use}] @ {current_time}\n"
    # A long multibyte path can overflow one RF frame — split, never truncate.
    await ctx.reply_split(prefix + "\n".join(parts))
