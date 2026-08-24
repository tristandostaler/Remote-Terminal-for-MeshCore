"""Space weather: solar indices, HF band conditions, and the aurora outlook.

``solar`` reports SFI / sunspots / A-index / K-index. ``hfcond`` (or ``bands``)
reports day and night propagation per band. Both come from one HamQSL document,
fetched once and cached for a few minutes, so asking for both costs one
request. ``aurora`` / ``kp`` maps the NOAA SWPC planetary K index to the G storm
scale.

Merged from the separate ``hfcond`` and ``aurora`` bots (library 1.1.0):
``solar`` and ``hfcond`` were fetching the *same* URL and parsing different
halves of it, and ``aurora`` answered the same "what is the ionosphere doing"
question from NOAA.
"""

import re
import time
import xml.etree.ElementTree as ElementTree

from remoteterm import bot

BOT_META = {
    "key": "solar",
    "name": "solar",
    "category": "Solar",
    "description": "Space weather: solar indices, HF band conditions, aurora outlook",
    "long_description": (
        "`solar` reports the space-weather numbers — solar flux, sunspots, A index and K index. "
        "`hfcond` (or `bands`) turns the same HamQSL document into day and night propagation per "
        "band, and `aurora` / `kp` maps the NOAA planetary K index onto the G storm scale. The "
        "HamQSL fetch is cached for a few minutes, so asking for both costs one request. Internet "
        "access required."
    ),
    "version": "1.2.1",
    "cooldown_seconds": 5,
}

_HAMQSL_URL = "https://www.hamqsl.com/solarxml.php"
_SWPC_URL = "https://services.swpc.noaa.gov/json/planetary_k_index_1m.json"

# HamQSL publishes hourly, so a few minutes of reuse costs nothing in freshness
# and means `solar` followed by `hfcond` is one request, not two. Module-level:
# persists across handler calls for this load, resets when the code is saved.
_HAMQSL_TTL_SECONDS = 300
_cache: dict = {}


async def _hamqsl_root(ctx):
    """Parsed HamQSL document, from cache when it is still fresh."""
    cached = _cache.get("hamqsl")
    if cached and time.time() - cached[0] < _HAMQSL_TTL_SECONDS:
        return cached[1]
    root = ElementTree.fromstring(await ctx.http.get_text(_HAMQSL_URL))
    _cache["hamqsl"] = (time.time(), root)
    return root


def _describe_kp(kp: float) -> tuple[str, str]:
    """(label, visibility hint) for a Kp value, on the NOAA G scale."""
    if kp >= 9.0:
        return "G5 extreme storm", "aurora possible far south"
    if kp >= 8.0:
        return "G4 severe storm", "aurora possible far south"
    if kp >= 7.0:
        return "G3 strong storm", "aurora possible to mid latitudes"
    if kp >= 6.0:
        return "G2 moderate storm", "aurora visible at high latitudes"
    if kp >= 5.0:
        return "G1 storm", "aurora possible at high latitudes"
    if kp >= 4.0:
        return "active", "aurora possible at very high latitudes"
    return "quiet", "aurora unlikely"


@bot.on_keyword()
@bot.on_keyword("solar")
async def solar(ctx, msg):
    try:
        data = (await _hamqsl_root(ctx)).find("solardata")
        if data is None:
            raise ValueError("no solardata element")
        sfi = (data.findtext("solarflux") or "?").strip()
        ssn = (data.findtext("sunspots") or "?").strip()
        a_index = (data.findtext("aindex") or "?").strip()
        k_index = (data.findtext("kindex") or "?").strip()
        updated = (data.findtext("updated") or "").strip()
    except Exception:  # httpx.HTTPError / XML shape surprises (ctx.http owns the client)
        await ctx.reply(ctx.t("rt.error_upstream"))
        return
    stamp = re.search(r"(\d{2})\d{2} GMT", updated)
    suffix = f" — updated {stamp.group(1)}Z" if stamp else ""
    await ctx.reply(f"SFI {sfi} SSN {ssn} A {a_index} K {k_index}{suffix}")


@bot.on_keyword("hfcond", "bands")
async def hfcond(ctx, msg):
    try:
        root = await _hamqsl_root(ctx)
        rows = []
        for band in root.findall("./solardata/calculatedconditions/band"):
            period = (band.get("time") or "").strip().lower()
            name = (band.get("name") or "").strip()
            condition = (band.text or "").strip()
            if period and name and condition:
                rows.append((period, name, condition))
    except Exception:  # httpx.HTTPError / XML shape surprises (ctx.http owns the client)
        await ctx.reply(ctx.t("rt.error_upstream"))
        return
    if not rows:
        await ctx.reply(ctx.t("rt.no_results"))
        return
    for period in ("day", "night"):
        parts = [f"{name}={cond}" for row_period, name, cond in rows if row_period == period]
        if parts:
            await ctx.reply_split(f"HF {period}: " + " ".join(parts))


@bot.on_keyword("aurora", "kp")
async def aurora(ctx, msg):
    try:
        rows = await ctx.http.get_json(_SWPC_URL)
        last = rows[-1]
        kp = last.get("estimated_kp")
        if kp is None:
            kp = last["kp_index"]
        kp = float(kp)
    except Exception:  # httpx.HTTPError / payload shape surprises (ctx.http owns the client)
        await ctx.reply(ctx.t("rt.error_upstream"))
        return
    label, hint = _describe_kp(kp)
    await ctx.reply(f"Kp {kp:.1f} ({label}) — {hint}")
