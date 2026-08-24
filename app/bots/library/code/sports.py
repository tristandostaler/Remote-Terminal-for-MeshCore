"""Compact live/final scores from ESPN's public scoreboard API.

``sports`` / ``score`` / ``scores`` report the configured league (or one named
inline: ``sports nhl``). ``wc`` / ``worldcup`` are shorthand for the FIFA World
Cup scoreboard. A cron trigger can additionally announce live score changes to
a channel.

Merged from the separate ``worldcup`` and ``worldcup_live`` bots
(library 1.2.0): all three called the same ESPN scoreboard endpoint with a
different sport path, so they were one bot with three names.
"""

import time

from remoteterm import bot

BOT_META = {
    "key": "sports",
    "name": "sports",
    "category": "Sports",
    "description": "Live and final scores from ESPN (NFL, NBA, MLB, NHL, MLS, World Cup)",
    "long_description": (
        "`sports` (or `score` / `scores`) reports live and final scores for the league configured "
        "below, and an inline league such as `sports nhl` overrides it for one call. `wc` / "
        "`worldcup` are shorthand for the FIFA World Cup board. Add a cron trigger and a live "
        "channel and it will also announce score changes while games are on. Scores come from "
        "ESPN's public scoreboard; internet access required."
    ),
    "version": "1.3.1",
    "settings_schema": [
        {
            "key": "league",
            "label": "League",
            "type": "select",
            "default": "nfl",
            "options": [
                {"value": "nfl", "label": "NFL"},
                {"value": "nba", "label": "NBA"},
                {"value": "mlb", "label": "MLB"},
                {"value": "nhl", "label": "NHL"},
                {"value": "mls", "label": "MLS"},
                {"value": "worldcup", "label": "FIFA World Cup"},
            ],
        },
        {
            "key": "teams",
            "label": "Teams filter",
            "type": "text",
            "default": "",
            "help": "Optional comma-separated team abbreviations (e.g. SEA,SF)",
        },
        {
            "key": "live_channel",
            "label": "Live announcement channel",
            "type": "text",
            "default": "",
            "help": (
                "Channel name (e.g. #sports) for the live score announcer. Empty disables it. "
                "Add a cron trigger such as */2 * * * * on the Triggers tab to drive it."
            ),
        },
        {
            "key": "live_league",
            "label": "Live announcement league",
            "type": "select",
            "default": "worldcup",
            "options": [
                {"value": "nfl", "label": "NFL"},
                {"value": "nba", "label": "NBA"},
                {"value": "mlb", "label": "MLB"},
                {"value": "nhl", "label": "NHL"},
                {"value": "mls", "label": "MLS"},
                {"value": "worldcup", "label": "FIFA World Cup"},
            ],
        },
    ],
    "settings": {
        "league": "nfl",
        "teams": "",
        "live_channel": "",
        "live_league": "worldcup",
    },
}

_LEAGUE_PATHS = {
    "nfl": "football/nfl",
    "nba": "basketball/nba",
    "mlb": "baseball/mlb",
    "nhl": "hockey/nhl",
    "mls": "soccer/usa.1",
    "worldcup": "soccer/fifa.world",
}

# 'wc'/'worldcup' pin the league regardless of the league setting.
_KEYWORD_LEAGUES = {"wc": "worldcup", "worldcup": "worldcup"}

# When no match is live, back off instead of polling the scoreboard every tick.
IDLE_SECONDS = 30 * 60


def _scoreboard_url(league: str) -> str:
    path = _LEAGUE_PATHS.get(league, _LEAGUE_PATHS["nfl"])
    return f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard"


def _parse_game(event):
    """Return ('SEA 21 @ SF 17 Q3', state, {'SEA', 'SF'}) for one event, or None."""
    competitions = event.get("competitions") or [{}]
    away = home = None
    for side in competitions[0].get("competitors") or []:
        if side.get("homeAway") == "home":
            home = side
        elif side.get("homeAway") == "away":
            away = side
    if not away or not home:
        return None
    status = (event.get("status") or {}).get("type") or {}
    state = str(status.get("state") or "pre")
    detail = str(status.get("shortDetail") or "").replace(" - ", " ").strip()

    def _abbr(side):
        return str((side.get("team") or {}).get("abbreviation") or "?")

    away_abbr, home_abbr = _abbr(away), _abbr(home)
    line = f"{away_abbr} {away.get('score', '?')} @ {home_abbr} {home.get('score', '?')} {detail}"
    return line.strip(), state, {away_abbr.upper(), home_abbr.upper()}


@bot.on_keyword()
@bot.on_keyword("sports", "score", "scores", "wc", "worldcup")
async def sports(ctx, msg):
    league = (
        _KEYWORD_LEAGUES.get((msg.keyword or "").lower())
        or str(ctx.settings.get("league") or "nfl").lower()
    )
    args = [a.strip() for a in msg.args if a.strip()]
    if args and args[0].lower() in _LEAGUE_PATHS:
        league = args[0].lower()
        args = args[1:]

    try:
        data = await ctx.http.get_json(_scoreboard_url(league))
    except Exception as exc:  # httpx errors surface here; ctx.http owns the client
        ctx.log(f"ESPN scoreboard failed: {exc}", level="WARNING")
        await ctx.reply(ctx.t("rt.error_upstream"))
        return

    teams_setting = str(ctx.settings.get("teams") or "")
    wanted = {t.strip().upper() for t in teams_setting.split(",") if t.strip()}
    if args:
        wanted = {args[0].upper()}

    lines = []
    for event in data.get("events") or []:
        parsed = _parse_game(event)
        if parsed is None:
            continue
        line, state, team_abbrs = parsed
        if state not in ("in", "post"):
            continue
        if wanted and not (wanted & team_abbrs):
            continue
        lines.append(line)
        if len(lines) == 3:
            break

    if not lines:
        await ctx.reply(ctx.t("rt.no_results"))
        return

    # ctx.reply_split packs the games into as many RF-sized "(i/n)" parts as it
    # takes, so no game is dropped and no line is cut in half.
    await ctx.reply_split(" | ".join(lines))


@bot.on_cron("*/2 * * * *")
async def announce_live(ctx):
    """Posts kickoffs, goals and finals to a channel.

    Same schedule the worldcup-live bot used, so operators who had it enabled
    keep working after the merge. With no live_channel set this returns before
    any network call, and between matches it idles for 30 minutes at a time.
    """
    channel = str(ctx.settings.get("live_channel", "") or "").strip()
    if not channel:
        return
    now = int(time.time())
    if now < int(ctx.state.get("idle_until", 0)):
        return

    league = str(ctx.settings.get("live_league") or "worldcup").lower()
    try:  # upstream failures: log and retry next poll (ctx.http owns the client)
        events = (await ctx.http.get_json(_scoreboard_url(league))).get("events", [])
    except Exception as exc:
        ctx.log(f"ESPN poll failed: {exc}", level="WARNING")
        return

    # (event_id, line) per event, so a score change is a plain string compare.
    summaries = {}
    for event in events:
        parsed = _parse_game(event)
        if parsed is None:
            continue
        line, state, _teams = parsed
        summaries[str(event.get("id") or line)] = (line, state)

    live = {eid: line for eid, (line, state) in summaries.items() if state == "in"}
    if not live:
        ctx.state["idle_until"] = now + IDLE_SECONDS
        return

    scores = ctx.state.setdefault("scores", {})
    for event_id, line in live.items():
        previous = scores.get(event_id)
        if previous is None:
            scores[event_id] = line
            await ctx.send(channel, f"Kickoff: {line}")
        elif previous != line:
            scores[event_id] = line
            await ctx.send(channel, f"SCORE! {line}")

    # Finished matches: announce once, then forget.
    for event_id, (line, state) in summaries.items():
        if state == "post" and event_id in scores and event_id not in live:
            del scores[event_id]
            await ctx.send(channel, f"FT: {line}")
