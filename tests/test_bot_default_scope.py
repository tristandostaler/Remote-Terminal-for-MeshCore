"""The default channel scope for a new bot: #bot / #bots plus DMs.

Bots answer commands, so an unscoped bot replies on Public and on every other
channel the node carries. A fresh bot is therefore scoped to the two
conventional bot channels, and these tests pin that default at every layer it is
spelled out: the derived keys, the schema column, the repository, and the engine
gate that actually silences Public.
"""

import json

import pytest

from app.bot_scope import DEFAULT_BOT_SCOPE_JSON, default_bot_scope, is_default_bot_scope
from app.bots.api import BotMessage
from app.bots.engine import BotEngine
from app.channel_constants import (
    BOT_CHANNEL_KEYS,
    BOT_CHANNEL_NAMES,
    PUBLIC_CHANNEL_KEY,
    hashtag_channel_key,
    is_bot_channel_key,
)
from app.models import Bot


class TestDefaultBotChannelKeys:
    def test_keys_are_derived_from_the_hashtag_names(self):
        # The whole point of deriving them: "#bot" hashes to the same key on
        # every node, so the default can name a channel we have not joined.
        assert BOT_CHANNEL_NAMES == ("#bot", "#bots")
        assert tuple(hashtag_channel_key(n) for n in BOT_CHANNEL_NAMES) == BOT_CHANNEL_KEYS
        assert all(len(key) == 32 and key == key.upper() for key in BOT_CHANNEL_KEYS)

    def test_hashtag_key_hashes_the_name_verbatim_including_the_hash(self):
        assert hashtag_channel_key("#bot") != hashtag_channel_key("bot")

    def test_public_is_not_a_bot_channel(self):
        assert not is_bot_channel_key(PUBLIC_CHANNEL_KEY)
        assert all(is_bot_channel_key(key.lower()) for key in BOT_CHANNEL_KEYS)


class TestDefaultScopeShape:
    def test_default_scope_selects_exactly_the_bot_channels(self):
        assert default_bot_scope() == {"channels": {"only": list(BOT_CHANNEL_KEYS)}}

    def test_default_scope_is_a_fresh_dict_each_call(self):
        first = default_bot_scope()
        first["channels"]["only"].append("deadbeef")
        assert default_bot_scope() == {"channels": {"only": list(BOT_CHANNEL_KEYS)}}

    def test_json_literal_matches_the_builder(self):
        # The schema column default is a SQL literal and cannot import Python.
        assert json.loads(DEFAULT_BOT_SCOPE_JSON) == default_bot_scope()

    def test_recognizes_only_the_default(self):
        assert is_default_bot_scope(default_bot_scope())
        assert is_default_bot_scope({"channels": {"only": [k.lower() for k in BOT_CHANNEL_KEYS]}})
        assert not is_default_bot_scope({"channels": "all"})
        assert not is_default_bot_scope({"channels": "none"})
        assert not is_default_bot_scope({"channels": {"except": list(BOT_CHANNEL_KEYS)}})
        assert not is_default_bot_scope({"channels": {"only": [BOT_CHANNEL_KEYS[0]]}})
        assert not is_default_bot_scope(None)

    def test_bot_model_defaults_to_it(self):
        assert is_default_bot_scope(Bot(id="b", name="n").scope)


class TestSchemaAndRepositoryDefaults:
    @pytest.mark.asyncio
    async def test_column_default_matches_the_builder(self, test_db):
        async with test_db.readonly() as conn:
            async with conn.execute("PRAGMA table_info(bots)") as cursor:
                columns = {row["name"]: row["dflt_value"] for row in await cursor.fetchall()}
        assert json.loads(columns["scope"].strip("'")) == default_bot_scope()

    @pytest.mark.asyncio
    async def test_created_bot_is_scoped_to_the_bot_channels_and_dms(self, test_db):
        from app.repository.bots import BotRepository

        bot = await BotRepository.create(name="scope-default")
        assert is_default_bot_scope(bot.scope)
        assert bot.respond_to_dms is True

    @pytest.mark.asyncio
    async def test_an_explicit_scope_still_wins(self, test_db):
        from app.repository.bots import BotRepository

        bot = await BotRepository.create(name="scope-explicit", scope={"channels": "all"})
        assert bot.scope == {"channels": "all"}

    @pytest.mark.asyncio
    async def test_api_created_bots_get_the_default(self, test_db, client):
        code = (
            "from remoteterm import bot\n"
            '@bot.on_keyword("hi")\n'
            "async def f(ctx, msg):\n"
            '    await ctx.reply("hey")\n'
        )
        async with client:
            created = (
                await client.post("/api/bots", json={"name": "api-scope-default", "code": code})
            ).json()
        assert is_default_bot_scope(created["scope"])


class TestSeededBotsAreScoped:
    @pytest.mark.asyncio
    async def test_library_seeding_uses_the_default(self, test_db):
        from app.bots.library import ensure_seeded
        from app.repository.bots import BotRepository

        await ensure_seeded()
        seeded = await BotRepository.get_all()
        assert seeded, "the built-in library should have seeded at least one bot"
        # A built-in may opt out via BOT_META["scope"], but none may silently
        # land on "every channel".
        assert all(bot.scope.get("channels") != "all" for bot in seeded)
        assert any(is_default_bot_scope(bot.scope) for bot in seeded)


class TestEngineHonorsTheDefault:
    def setup_method(self):
        self.engine = BotEngine()

    def _bot(self) -> Bot:
        return Bot(id="b1", name="default-scoped", enabled=True)

    def _msg(self, **overrides) -> BotMessage:
        base = {
            "text": "ping",
            "sender_name": "K0PHX",
            "sender_key": "ab" * 32,
            "is_dm": False,
            "channel_key": BOT_CHANNEL_KEYS[0],
            "channel_name": "#bot",
        }
        base.update(overrides)
        return BotMessage(**base)

    @pytest.mark.parametrize("key", BOT_CHANNEL_KEYS)
    def test_answers_on_the_bot_channels(self, key):
        assert self.engine._scope_allows(self._bot(), self._msg(channel_key=key))

    def test_stays_quiet_on_public(self):
        assert not self.engine._scope_allows(
            self._bot(), self._msg(channel_key=PUBLIC_CHANNEL_KEY, channel_name="Public")
        )

    def test_stays_quiet_on_an_unrelated_channel(self):
        assert not self.engine._scope_allows(
            self._bot(), self._msg(channel_key="A" * 32, channel_name="#general")
        )

    def test_still_answers_dms(self):
        assert self.engine._scope_allows(
            self._bot(), self._msg(is_dm=True, channel_key=None, channel_name=None)
        )
