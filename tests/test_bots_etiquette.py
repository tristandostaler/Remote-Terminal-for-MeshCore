"""The #bots etiquette pair: ``bots`` and ``source`` ship enabled and answer."""

import asyncio

from app.bot_scope import is_default_bot_scope
from app.bots.api import BotContext, BotMessage
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code

ETIQUETTE_KEYS = {"bots", "source"}


async def _run(
    key, text, *, settings=None, enabled_bots=None, command_prefix="!", author_contact=""
):
    entry = get_library_entry(key)
    assert entry is not None
    handler = load_bot_code(entry["code"]).collector.keywords[0].handler
    ctx = BotContext(
        bot_id=key,
        bot_name=key,
        settings=settings if settings is not None else dict(entry.get("settings") or {}),
        state={},
        is_test=True,
        loop=asyncio.get_event_loop(),
        origin_is_dm=True,
        origin_sender_key="ab" * 32,
        command_prefix=command_prefix,
        author_contact=author_contact,
    )
    ctx.get_enabled_bots = lambda: enabled_bots or []  # type: ignore[method-assign]
    word, _, rest = text.partition(" ")
    await handler(
        ctx,
        BotMessage(
            text=text,
            is_dm=True,
            sender_key="ab" * 32,
            keyword=word,
            args=rest.split() if rest else [],
        ),
    )
    return [s["text"] for s in ctx.captured_sends]


async def test_only_the_etiquette_bots_are_seeded_enabled(test_db):
    from app.bots.library import ensure_seeded
    from app.repository.bots import BotRepository

    await ensure_seeded()
    records = [r for r in await BotRepository.get_all() if r.builtin_key]
    enabled = {r.builtin_key for r in records if r.enabled}
    assert enabled == ETIQUETTE_KEYS
    for record in records:
        if record.builtin_key in ETIQUETTE_KEYS:
            # Enabled, but still only on #bot / #bots (+ DMs), never Public.
            assert is_default_bot_scope(record.scope)


async def test_an_operator_disabling_one_is_never_overridden(test_db):
    from app.bots.library import ensure_seeded
    from app.repository.bots import BotRepository

    await ensure_seeded()
    record = await BotRepository.get_by_builtin_key("bots")
    assert record is not None
    # Disabled by the operator, and an older version so the refresh path runs.
    await BotRepository.update(record.id, enabled=False, builtin_version="0.0.1")

    await ensure_seeded()
    refreshed = await BotRepository.get(record.id)
    assert refreshed is not None
    assert refreshed.builtin_version == get_library_entry("bots")["version"]
    assert refreshed.enabled is False


async def test_bots_lists_commands_and_points_at_help_and_contact():
    enabled = [
        {"name": "bots", "description": "d", "keywords": ["bots"]},
        {"name": "help", "description": "d", "keywords": ["help", "cmd"]},
        {"name": "ping", "description": "d", "keywords": ["ping", "test"]},
        {"name": "source", "description": "d", "keywords": ["source", "author"]},
    ]
    texts = await _run("bots", "bots", enabled_bots=enabled)
    assert texts == [
        "🤖 RemoteTerm bot\n"
        "Commands: bots, help, ping, source\n"
        "!help <command> for details; !source / !author to reach the author"
    ]


async def test_bots_hints_use_the_nodes_own_prefix():
    enabled = [
        {"name": "help", "description": "d", "keywords": ["help"]},
        {"name": "source", "description": "d", "keywords": ["source", "author"]},
    ]
    texts = await _run("bots", "bots", enabled_bots=enabled, command_prefix="?")
    assert texts[0].endswith("?help <command> for details; ?source / ?author to reach the author")
    # No prefix configured: the bare words are what people type.
    texts = await _run("bots", "bots", enabled_bots=enabled, command_prefix="")
    assert texts[0].endswith("help <command> for details; source / author to reach the author")


async def test_bots_uses_the_operator_introduction():
    texts = await _run(
        "bots",
        "bots",
        settings={"about": "Weather bot run by VE2XYZ"},
        enabled_bots=[{"name": "wx", "description": "d", "keywords": ["wx"]}],
    )
    assert texts == ["Weather bot run by VE2XYZ\nCommands: wx"]


async def test_source_leads_with_the_link_and_author_with_the_contact():
    settings = {"source_url": "https://example.com/bot"}
    contact = "me@example.com"
    assert await _run("source", "source", settings=settings, author_contact=contact) == [
        "🤖 Source: https://example.com/bot | Author: me@example.com"
    ]
    assert await _run("source", "author", settings=settings, author_contact=contact) == [
        "🤖 Author: me@example.com | Source: https://example.com/bot"
    ]


async def test_author_contact_is_an_engine_setting(test_db):
    """Set once in Bots › Engine, stored, and handed to every bot run."""
    from app.bots.engine import BotEngine, LoadedBot
    from app.models import Bot
    from app.repository.bots import BotEngineSettingsRepository

    saved = await BotEngineSettingsRepository.update(author_contact="VE2XYZ on #bots")
    assert saved.author_contact == "VE2XYZ on #bots"

    engine = BotEngine()
    engine.settings = saved
    entry = get_library_entry("source")
    assert entry is not None
    record = Bot(id="source", name="source", code=entry["code"], enabled=True)
    ctx = await engine._make_context(
        LoadedBot(record=record, code=load_bot_code(entry["code"])), msg=None, is_test=True
    )
    assert ctx.author_contact == "VE2XYZ on #bots"


async def test_source_falls_back_when_nothing_is_configured():
    texts = await _run("source", "author", settings={"source_url": ""})
    assert texts == [
        "🤖 Author: DM this node to reach its operator | "
        "Source: https://github.com/tristandostaler/Remote-Terminal-for-MeshCore"
    ]


async def test_the_bots_bot_can_be_disabled_but_not_deleted(test_db, client):
    from app.bots.library import ensure_seeded
    from app.repository.bots import BotRepository

    await ensure_seeded()
    protected = await BotRepository.get_by_builtin_key("bots")
    other = await BotRepository.get_by_builtin_key("source")
    assert protected is not None and other is not None
    async with client:
        fetched = (await client.get(f"/api/bots/{protected.id}")).json()
        assert fetched["deletable"] is False
        assert (await client.get(f"/api/bots/{other.id}")).json()["deletable"] is True

        refused = await client.delete(f"/api/bots/{protected.id}")
        assert refused.status_code == 403
        assert await BotRepository.get(protected.id) is not None

        disabled = await client.patch(f"/api/bots/{protected.id}", json={"enabled": False})
        assert disabled.status_code == 200
        assert disabled.json()["enabled"] is False

        # source is not protected.
        assert (await client.delete(f"/api/bots/{other.id}")).status_code == 200


async def test_bang_bots_answers_whatever_the_prefix_and_mention_settings(monkeypatch, test_db):
    """A node set to '?' prefix and mention-only still answers '!bots' and '!author'."""
    from app.bots.engine import BotEngine, LoadedBot
    from app.models import Bot, BotEngineSettings

    engine = BotEngine()
    engine._started = True
    engine.settings = BotEngineSettings(
        command_prefix="?",
        require_prefix=True,
        mention_mode="only",
        global_reply_seconds=0,
        per_user_seconds=0,
    )
    monkeypatch.setattr(engine, "_node_name", lambda: "MyNode")
    spawned: list[str] = []
    monkeypatch.setattr(
        engine, "_spawn_run", lambda loaded, trigger, msg, **kw: spawned.append(trigger)
    )
    for key in ("bots", "source"):
        entry = get_library_entry(key)
        assert entry is not None
        record = Bot(id=key, name=key, code=entry["code"], enabled=True, scope={"channels": "all"})
        engine.bots[key] = LoadedBot(record=record, code=load_bot_code(entry["code"]))

    async def send(text, sender="Alice"):
        spawned.clear()
        await engine._handle_message(
            {
                "type": "CHAN",
                "conversation_key": "A" * 32,
                "channel_name": "#bots",
                "sender_name": sender,
                "text": f"{sender}: {text}",
            }
        )
        return list(spawned)

    assert await send("!bots") == ["kw bots"]
    assert await send("!author", sender="Dave") == ["kw author"]
    # Everything else still obeys the node's settings: mention-only means no.
    assert await send("?source", sender="Bob") == []
    assert await send("@[MyNode] ?source", sender="Carol") == ["kw source"]
