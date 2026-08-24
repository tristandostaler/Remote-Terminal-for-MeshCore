"""Dice and random numbers. Seeded from meshcore-bot's dice command.

``dice`` (d20), ``dice d6``, ``dice 3d6``, ``dice decade`` (d10+d10), and
``roll [max]`` for a flat 1..N draw (default 100).

Merged from the separate ``roll`` bot (library 1.1.0): both were random-number
rollers, so they shared a purpose and one enable toggle now covers both. The
two commands keep their own syntax and output — ``roll`` is not an alias for
``dice``.
"""

import random

from remoteterm import bot

BOT_META = {
    "key": "dice",
    "name": "dice",
    "category": "Fun",
    "description": "Dice roller (d20, 2d6, decade) and flat 1..N rolls",
    "version": "1.1.0",
    "settings_schema": [
        {
            "key": "max_dice",
            "label": "Maximum dice per roll",
            "type": "int",
            "default": 10,
            "min": 1,
            "max": 50,
        }
    ],
    "settings": {"max_dice": 10},
}

ROLL_MAX = 10000


def _parse_spec(spec):
    """'d20' -> (1, 20); '3d6' -> (3, 6). Returns None when unparseable."""
    spec = spec.lower().strip()
    if "d" not in spec:
        return None
    count_str, _, sides_str = spec.partition("d")
    try:
        count = int(count_str) if count_str else 1
        sides = int(sides_str)
    except ValueError:
        return None
    if count < 1 or sides < 2:
        return None
    return count, sides


@bot.on_keyword()
@bot.on_keyword("dice")
async def roll_dice(ctx, msg):
    max_dice = int(ctx.settings.get("max_dice", 10))
    spec = msg.arg_text.strip() or "d20"

    if spec.lower() == "decade":
        tens = random.randint(0, 9) * 10
        ones = random.randint(0, 9)
        total = tens + ones if (tens or ones) else 100
        await ctx.reply(f"decade roll: {tens:02d} + {ones} = {total}")
        return

    parsed = _parse_spec(spec)
    if parsed is None:
        await ctx.reply("usage: dice [NdS|decade] — e.g. dice d20, dice 3d6")
        return
    count, sides = parsed
    if count > max_dice:
        await ctx.reply(f"max {max_dice} dice per roll")
        return
    rolls = [random.randint(1, sides) for _ in range(count)]
    if count == 1:
        await ctx.reply(f"d{sides}: {rolls[0]}")
    else:
        # Many dice overflow one RF frame — split, never truncate.
        await ctx.reply_split(f"{count}d{sides}: {' + '.join(map(str, rolls))} = {sum(rolls)}")


@bot.on_keyword("roll")
async def roll(ctx, msg):
    arg = msg.arg_text.strip()
    max_num = 100
    if arg:
        if not arg.isdigit() or not 1 <= int(arg) <= ROLL_MAX:
            await ctx.reply(f"usage: roll [max] — e.g. roll, roll 20 (max {ROLL_MAX})")
            return
        max_num = int(arg)
    result = random.randint(1, max_num)
    # @[name] is the mention syntax mesh clients recognize and highlight.
    who = f"@[{msg.sender_name}]" if msg.sender_name else "Someone"
    await ctx.reply(f"{who} rolled {result} (1-{max_num})")
