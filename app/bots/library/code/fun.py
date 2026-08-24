"""One-liners for the mesh: jokes, facts, fortunes, and the Magic 8-Ball.

Commands: ``joke``, ``dadjoke``, ``catfact``, ``funfact``, ``fortune``,
``magic8``, and ``fun`` for a random pick of whatever is switched on.

Merged from the ``joke``, ``dadjoke``, ``catfact``, ``funfact``, ``fortunes``
and ``magic8`` bots (library 1.0.0): six bots that each replied with one random
line. Each source has its own on/off setting here, so the granularity the
separate bots gave you is preserved — including keeping the network-backed ones
off if this node should stay offline. ``funfact``, ``fortune`` and ``magic8``
need no network at all.
"""

import random

from remoteterm import bot

BOT_META = {
    "key": "fun",
    "name": "fun",
    "category": "Fun",
    "description": "Jokes, dad jokes, cat facts, fun facts, fortunes, Magic 8-Ball",
    "version": "1.1.0",
    "settings_schema": [
        {
            "key": "joke_enabled",
            "label": "joke — JokeAPI (network)",
            "type": "bool",
            "default": True,
        },
        {
            "key": "dadjoke_enabled",
            "label": "dadjoke — icanhazdadjoke.com (network)",
            "type": "bool",
            "default": True,
        },
        {
            "key": "catfact_enabled",
            "label": "catfact — catfact.ninja (network)",
            "type": "bool",
            "default": True,
        },
        {
            "key": "funfact_enabled",
            "label": "funfact — built-in list (offline)",
            "type": "bool",
            "default": True,
        },
        {
            "key": "fortune_enabled",
            "label": "fortune — built-in list (offline)",
            "type": "bool",
            "default": True,
        },
        {
            "key": "magic8_enabled",
            "label": "magic8 — built-in list (offline)",
            "type": "bool",
            "default": True,
        },
    ],
    "settings": {
        "joke_enabled": True,
        "dadjoke_enabled": True,
        "catfact_enabled": True,
        "funfact_enabled": True,
        "fortune_enabled": True,
        "magic8_enabled": True,
    },
}

FACTS = (
    "Honey never spoils — sealed honey found in ancient tombs was still edible.",
    "Octopuses have three hearts and blue blood.",
    "A day on Venus is longer than its year.",
    "Bananas are berries; strawberries are not.",
    "Sunlight takes about 8 minutes to reach Earth.",
    "Sharks existed before trees appeared on Earth.",
    "The Eiffel Tower grows several centimeters taller in summer heat.",
    "At its triple point, water can boil and freeze at the same time.",
    "A group of crows is called a murder.",
    "Sloths can hold their breath longer than dolphins can.",
    "The human body contains enough iron to make a small nail.",
    "Wombats produce cube-shaped droppings.",
    "Scotland's national animal is the unicorn.",
    "There are more possible chess games than atoms in the observable universe.",
    "Venus is the hottest planet, even though Mercury is closer to the Sun.",
    "An octopus can taste with its arms.",
    "Sound travels roughly four times faster in water than in air.",
    "The Moon drifts about 3.8 cm farther from Earth every year.",
    "Some turtles can absorb oxygen through their rear ends while hibernating.",
    "A lightning bolt is about five times hotter than the Sun's surface.",
    "Butterflies taste with their feet.",
    "A blue whale's heart can weigh over 180 kg.",
    "Antarctica is the largest desert on Earth.",
    "Cows form close friendships and get stressed when separated.",
    "Polar bear skin is black under all that white fur.",
    "The Great Wall of China is not visible from the Moon with the naked eye.",
    "Olympus Mons on Mars is the tallest volcano in the solar system.",
    "Sea otters hold hands while sleeping so they don't drift apart.",
    "Honeybees can recognize human faces.",
    "Oxford University is older than the Aztec Empire.",
    "There are more trees on Earth than stars in the Milky Way.",
    "The first computer bug was an actual moth found in a relay in 1947.",
    "Human radio broadcasts have been traveling into space for about a century.",
    "GPS satellites must correct for Einstein's relativity to stay accurate.",
    "The International Space Station orbits Earth about every 90 minutes.",
    "Voyager 1, launched in 1977, is the most distant human-made object.",
    "Sound cannot travel through the vacuum of space.",
    "Hawaii drifts several centimeters closer to Japan every year.",
    "A teaspoon of neutron star material would weigh billions of tons.",
    "A hummingbird's heart can beat more than 1,200 times per minute.",
)

FORTUNES = (
    "A message you almost didn't send will matter most.",
    "Your patience is a signal; someone distant is receiving it.",
    "Good news travels slowly but arrives intact.",
    "The quiet channel holds the loudest opportunity.",
    "An unexpected hop brings an unexpected friend.",
    "Fortune favors the well-placed antenna.",
    "You will find what you seek two nodes away.",
    "A small kindness today echoes for many hops.",
    "The answer you await is already in transit.",
    "Listen twice, transmit once, prosper always.",
    "A closed door hides an open relay.",
    "Your next idea deserves more power than you plan to give it.",
    "Someone remembers your help long after you forgot giving it.",
    "Clear skies ahead; keep your line of sight.",
    "The detour you dread becomes the story you tell.",
    "Persistence beats signal strength.",
    "An old contact returns with new coordinates.",
    "What you practice quietly will be praised publicly.",
    "The best time to raise an antenna was yesterday; the second best is today.",
    "A short message can carry a long friendship.",
    "Luck is loudest where preparation meets propagation.",
    "You are closer to the summit than the map suggests.",
    "Share what you know; it multiplies in the sharing.",
    "A stranger's question leads you to your next project.",
    "Redundancy today prevents regret tomorrow.",
    "Your curiosity is a compass; follow it uphill.",
    "Slow progress is still propagation.",
    "The network grows because you showed up.",
    "Something lost returns by an unlikely route.",
    "Trust the process, verify the checksum.",
    "A good night's sleep improves your signal-to-noise ratio.",
    "Tomorrow brings a clear frequency and a clearer mind.",
    "Help offered freely returns amplified.",
    "The mountain does not move, but your repeater might.",
    "Your smallest habit is quietly compounding.",
    "An overlooked detail proves valuable this week.",
    "Speak less, mean more, reach farther.",
    "New paths open when old assumptions retire.",
    "You will be the good news in someone's feed.",
    "Adventure begins at the edge of coverage.",
    "The favor you forgot is remembered fondly.",
    "Keep your promises short and your memory long.",
    "A change in weather brings a change in luck.",
    "What seems like noise today decodes tomorrow.",
    "Generosity is the strongest signal you can send.",
    "Your backup plan becomes the main event.",
    "A friendly ping opens a lasting link.",
    "Doubt is fog; movement is wind.",
    "The best conversations start with hello.",
    "Every expert was once lost without a map.",
)

MAGIC8_ANSWERS = (
    "It is certain.",
    "It is decidedly so.",
    "Without a doubt.",
    "Yes definitely.",
    "You may rely on it.",
    "As I see it, yes.",
    "Most likely.",
    "Outlook good.",
    "Yes.",
    "Signs point to yes.",
    "Reply hazy, try again.",
    "Ask again later.",
    "Better not tell you now.",
    "Cannot predict now.",
    "Concentrate and ask again.",
    "Don't count on it.",
    "My reply is no.",
    "My sources say no.",
    "Outlook not so good.",
    "Very doubtful.",
)

# Keyword -> source. Every alias the six original bots answered to is kept.
_KEYWORD_SOURCES = {
    "joke": "joke",
    "jokes": "joke",
    "dadjoke": "dadjoke",
    "dadjokes": "dadjoke",
    "dad joke": "dadjoke",
    "dad jokes": "dadjoke",
    "catfact": "catfact",
    "meow": "catfact",
    "purr": "catfact",
    "funfact": "funfact",
    "fortune": "fortune",
    "magic8": "magic8",
}

_SOURCE_KEYWORDS = tuple(_KEYWORD_SOURCES)

# Canonical source names, for the 'fun' random pick.
_SOURCES = ("joke", "dadjoke", "catfact", "funfact", "fortune", "magic8")


def _enabled(ctx, source: str) -> bool:
    value = ctx.settings.get(f"{source}_enabled", True)
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


async def _joke(ctx):
    data = await ctx.http.get_json("https://v2.jokeapi.dev/joke/Any?safe-mode")
    kind = data.get("type")
    if kind == "single":
        return [str(data.get("joke") or "").strip()]
    if kind == "twopart":
        setup = str(data.get("setup") or "").strip()
        delivery = str(data.get("delivery") or "").strip()
        if setup and delivery:
            return [setup, delivery]
    return []


async def _dadjoke(ctx):
    data = await ctx.http.get_json(
        "https://icanhazdadjoke.com/", headers={"Accept": "application/json"}
    )
    return [str(data.get("joke") or "").strip()]


async def _catfact(ctx):
    data = await ctx.http.get_json("https://catfact.ninja/fact")
    return [str(data.get("fact") or "").strip()]


async def _funfact(ctx):
    return [random.choice(FACTS)]


async def _fortune(ctx):
    return [random.choice(FORTUNES)]


async def _magic8(ctx):
    return [f"Magic 8-Ball: {random.choice(MAGIC8_ANSWERS)}"]


_FETCHERS = {
    "joke": _joke,
    "dadjoke": _dadjoke,
    "catfact": _catfact,
    "funfact": _funfact,
    "fortune": _fortune,
    "magic8": _magic8,
}


async def _deliver(ctx, source: str) -> None:
    try:
        messages = [m for m in await _FETCHERS[source](ctx) if m]
    except Exception as exc:  # httpx errors surface here; ctx.http owns the client
        ctx.log(f"{source} source failed: {exc}", level="WARNING")
        await ctx.reply(ctx.t("rt.error_upstream"))
        return
    if not messages:
        await ctx.reply(ctx.t("rt.no_results"))
        return
    for message in messages:
        # Upstream jokes and facts run long — split, never truncate.
        await ctx.reply_split(message)


@bot.on_keyword(*_SOURCE_KEYWORDS)
async def one_liner(ctx, msg):
    # Only the declared source words reach here: a Triggers-tab keyword routes
    # to the generic handler below, which is `fun` and picks a source itself. So
    # this lookup always resolves — keep it that way if a handler is added.
    source = _KEYWORD_SOURCES[(msg.keyword or "").lower()]
    if not _enabled(ctx, source):
        await ctx.reply(f"'{msg.keyword}' is switched off in this bot's settings.")
        return
    await _deliver(ctx, source)


@bot.on_keyword()
@bot.on_keyword("fun")
async def surprise_me(ctx, msg):
    available = [s for s in _SOURCES if _enabled(ctx, s)]
    if not available:
        await ctx.reply("Every fun source is switched off in this bot's settings.")
        return
    await _deliver(ctx, random.choice(available))
