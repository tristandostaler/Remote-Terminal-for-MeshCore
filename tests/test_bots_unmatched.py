"""``@bot.on_unmatched``: a DM no bot's keyword claimed, from a listed contact.

This is how tinyllm answers chosen contacts without ``ask``: the engine decides
"nobody matched" synchronously while dispatching keywords, so a fallback never
races the bot whose keyword did match (``hello`` still goes to the hello bot).
"""

import textwrap

import pytest

from app.bots.api import contact_listed
from app.bots.engine import UNMATCHED_TRIGGER, BotEngine, LoadedBot
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code
from app.models import Bot, BotAdminUser, BotEngineSettings, BotTestRequest

ALICE = "ab" * 32
BOB = "cd" * 32

FALLBACK_CODE = textwrap.dedent(
    """
    from remoteterm import bot

    @bot.on_keyword("ask")
    @bot.on_unmatched("contacts")
    async def ask(ctx, msg):
        await ctx.reply("answer: " + msg.arg_text)
    """
)

KEYWORD_CODE = textwrap.dedent(
    """
    from remoteterm import bot

    @bot.on_keyword("hello", "reboot")
    async def hello(ctx, msg):
        await ctx.reply("hi")
    """
)


def _loaded(bot_id, code, **overrides):
    record = Bot(
        **{
            "id": bot_id,
            "name": bot_id,
            "code": code,
            "enabled": True,
            "scope": {"channels": "all"},
            **overrides,
        }
    )
    return LoadedBot(record=record, code=load_bot_code(code))


@pytest.fixture
def engine(monkeypatch, test_db):
    engine = BotEngine()
    engine._started = True
    engine.settings = BotEngineSettings(global_reply_seconds=0, per_user_seconds=0)
    engine.spawned = []  # type: ignore[attr-defined]
    monkeypatch.setattr(
        engine,
        "_spawn_run",
        lambda loaded, trigger, msg, **kw: engine.spawned.append(  # type: ignore[attr-defined]
            (loaded.record.id, trigger, msg.keyword, msg.arg_text)
        ),
    )
    return engine


def _install(engine, *bots):
    engine.bots = {b.record.id: b for b in bots}


async def _dm(engine, text, sender=ALICE, **extra):
    engine.spawned.clear()
    await engine._handle_message(
        {"type": "PRIV", "conversation_key": sender, "text": text, **extra}
    )
    return list(engine.spawned)


class TestContactListed:
    def test_keys_prefixes_and_separators(self):
        assert contact_listed(f"{BOB}, {ALICE}", ALICE)
        assert contact_listed("cdcdcd ABABAB", ALICE.upper())
        assert contact_listed("x;abab ab", ALICE) is False  # too-short prefixes never match
        assert contact_listed([ALICE[:8]], ALICE)

    def test_star_empty_and_no_sender(self):
        assert contact_listed("*", ALICE)
        assert contact_listed("*", None)
        assert not contact_listed("", ALICE)
        assert not contact_listed(None, ALICE)
        assert not contact_listed(ALICE, None)


class TestDispatch:
    async def test_unlisted_contacts_still_need_the_keyword(self, engine):
        """Option off (empty list): exactly the old behavior."""
        _install(engine, _loaded("llm", FALLBACK_CODE))
        assert await _dm(engine, "what is LoRa") == []
        assert await _dm(engine, "ask what is LoRa") == [("llm", "kw ask", "ask", "what is LoRa")]

    async def test_listed_contact_needs_no_keyword(self, engine):
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": ALICE[:12]}))
        assert await _dm(engine, "what is LoRa") == [
            ("llm", UNMATCHED_TRIGGER, None, "what is LoRa")
        ]
        assert await _dm(engine, "what is LoRa", sender=BOB) == []

    async def test_keyword_still_answers_once(self, engine):
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": ALICE}))
        assert await _dm(engine, "ask what is LoRa") == [("llm", "kw ask", "ask", "what is LoRa")]

    async def test_another_bots_keyword_wins(self, engine):
        _install(
            engine,
            _loaded("llm", FALLBACK_CODE, settings={"contacts": ALICE}),
            _loaded("hello", KEYWORD_CODE),
        )
        assert await _dm(engine, "hello there") == [("hello", "kw hello", "hello", "there")]

    async def test_a_throttled_or_admin_only_match_still_claims(self, engine):
        hello = _loaded("hello", KEYWORD_CODE, per_user_cooldown_seconds=60)
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": ALICE}), hello)
        assert await _dm(engine, "hello") == [("hello", "kw hello", "hello", "")]
        assert await _dm(engine, "hello") == []  # cooled down, and not handed to the llm

        admin = _loaded("admin", KEYWORD_CODE, admin_only=True)
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": ALICE}), admin)
        assert await _dm(engine, "reboot now") == []

    async def test_bang_bots_is_never_a_fallback(self, engine):
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": "*"}))
        assert await _dm(engine, "!bots") == []

    async def test_only_dms(self, engine):
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": "*"}))
        engine.spawned.clear()
        await engine._handle_message(
            {
                "type": "CHAN",
                "conversation_key": "A" * 32,
                "channel_name": "#bots",
                "sender_name": "Alice",
                "sender_key": ALICE,
                "text": "Alice: what is LoRa",
            }
        )
        assert engine.spawned == []
        assert await _dm(engine, "what is LoRa", outgoing=True) == []

    async def test_out_of_scope_or_disabled_bots_do_not_fall_back(self, engine):
        settings = {"contacts": "*"}
        _install(engine, _loaded("llm", FALLBACK_CODE, settings=settings, respond_to_dms=False))
        assert await _dm(engine, "what is LoRa") == []
        _install(engine, _loaded("llm", FALLBACK_CODE, settings=settings, enabled=False))
        assert await _dm(engine, "what is LoRa") == []

    async def test_fallback_passes_the_limiters(self, engine):
        engine.settings = BotEngineSettings(global_reply_seconds=0, per_user_seconds=60)
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": "*"}))
        assert len(await _dm(engine, "first")) == 1
        assert await _dm(engine, "second") == []

    async def test_unlisted_dms_spend_no_reply_slot(self, engine):
        engine.settings = BotEngineSettings(global_reply_seconds=60, per_user_seconds=0)
        _install(
            engine,
            _loaded("llm", FALLBACK_CODE, settings={"contacts": ALICE}),
            _loaded("hello", KEYWORD_CODE),
        )
        assert await _dm(engine, "chatting", sender=BOB) == []
        assert await _dm(engine, "hello", sender=BOB) == [("hello", "kw hello", "hello", "")]

    async def test_admin_only_fallback_bot(self, engine):
        engine.settings.admin_users = [BotAdminUser(public_key=BOB)]
        _install(engine, _loaded("llm", FALLBACK_CODE, settings={"contacts": "*"}, admin_only=True))
        assert await _dm(engine, "hi") == []
        assert len(await _dm(engine, "hi", sender=BOB)) == 1


class TestLibraryBots:
    async def test_hello_goes_to_hello_and_the_rest_to_tinyllm(self, engine):
        bots = []
        for key, settings in (("hello", {}), ("tinyllm", {"keywordless_contacts": [ALICE]})):
            entry = get_library_entry(key)
            assert entry is not None
            bots.append(_loaded(key, entry["code"], settings=settings))
        _install(engine, *bots)
        assert [s[:2] for s in await _dm(engine, "hello")] == [("hello", "kw hello")]
        assert await _dm(engine, "what is LoRa") == [
            ("tinyllm", UNMATCHED_TRIGGER, None, "what is LoRa")
        ]
        assert await _dm(engine, "what is LoRa", sender=BOB) == []


class TestTestTab:
    async def test_the_contact_list_applies(self, test_db):
        from app.repository.bots import BotRepository

        record = await BotRepository.create(
            name="fallback-test", code=FALLBACK_CODE, settings={"contacts": ALICE}
        )
        engine = BotEngine()
        listed = await engine.test_run(
            record, BotTestRequest(text="what is LoRa", is_dm=True, sender_key=ALICE)
        )
        assert listed.error is None
        assert [r["text"] for r in listed.replies] == ["answer: what is LoRa"]
        unlisted = await engine.test_run(
            record, BotTestRequest(text="what is LoRa", is_dm=True, sender_key=BOB)
        )
        assert unlisted.matched is False
