"""Bots in room servers: detection, scope gating, and where the reply lands.

A room-server post travels as a direct message from the room's own contact, with
the person who wrote it carried in the signed sender prefix. The engine has to
tell that apart from a real DM, or ``ctx.reply`` answers the author privately and
the room never sees what it asked for.
"""

import pytest

from app.bots.api import BotContext, BotMessage
from app.bots.engine import BotEngine
from app.models import CONTACT_TYPE_ROOM, Bot, ContactUpsert

ROOM_KEY = "aa" * 32
POSTER_KEY = "bb" * 32
OUR_KEY = "cc" * 32


def make_bot(**overrides) -> Bot:
    base = {
        "id": "b1",
        "name": "t",
        "category": "Basic",
        "description": "",
        "code": "",
        "enabled": True,
        "scope": {"channels": "all"},
    }
    base.update(overrides)
    return Bot(**base)


def room_msg(**overrides) -> BotMessage:
    base = {
        "text": "ping",
        "sender_name": "K0PHX",
        "sender_key": POSTER_KEY,
        "is_dm": False,
        "is_room": True,
        "room_key": ROOM_KEY,
        "room_name": "Ops Board",
    }
    base.update(overrides)
    return BotMessage(**base)


async def seed_room(name: str = "Ops Board", key: str = ROOM_KEY) -> None:
    from app.repository import ContactRepository

    await ContactRepository.upsert(ContactUpsert(public_key=key, name=name, type=CONTACT_TYPE_ROOM))


OTHER_ROOM_KEY = "dd" * 32


def scoped(rooms) -> Bot:
    return make_bot(scope={"channels": "all", "rooms": rooms})


class TestScopeGate:
    def setup_method(self):
        self.engine = BotEngine()

    def test_all_rooms(self):
        assert self.engine._scope_allows(scoped("all"), room_msg())

    def test_no_rooms(self):
        assert not self.engine._scope_allows(scoped("none"), room_msg())

    def test_only_list_answers_some_and_ignores_others(self):
        bot = scoped({"only": [ROOM_KEY]})
        assert self.engine._scope_allows(bot, room_msg())
        assert not self.engine._scope_allows(bot, room_msg(room_key=OTHER_ROOM_KEY))

    def test_except_list_answers_everywhere_but_the_named_room(self):
        bot = scoped({"except": [ROOM_KEY]})
        assert not self.engine._scope_allows(bot, room_msg())
        assert self.engine._scope_allows(bot, room_msg(room_key=OTHER_ROOM_KEY))

    def test_an_empty_only_list_is_silence(self):
        assert not self.engine._scope_allows(scoped({"only": []}), room_msg())

    def test_room_keys_match_case_insensitively(self):
        # Room keys are 64-hex contact keys, stored lower-case, while channel
        # keys are upper — the shared matcher must not care which it is given.
        bot = scoped({"only": [ROOM_KEY.upper()]})
        assert self.engine._scope_allows(bot, room_msg(room_key=ROOM_KEY.lower()))

    def test_a_scope_written_before_rooms_answers_every_room(self):
        assert self.engine._scope_allows(make_bot(scope={"channels": "all"}), room_msg())

    def test_dm_gate_does_not_cover_rooms(self):
        # The two are separate decisions in both directions.
        assert not self.engine._scope_allows(
            make_bot(respond_to_dms=True, scope={"channels": "all", "rooms": "none"}),
            room_msg(),
        )
        assert self.engine._scope_allows(
            make_bot(respond_to_dms=False, scope={"channels": "all", "rooms": "all"}),
            room_msg(),
        )

    def test_channel_scope_is_not_consulted_for_rooms(self):
        # A room is not a channel: an empty channel allow-list must not silence it.
        assert self.engine._scope_allows(
            make_bot(scope={"channels": "none", "rooms": "all"}), room_msg()
        )

    def test_room_scope_is_not_consulted_for_channels(self):
        bot = make_bot(scope={"channels": "all", "rooms": "none"})
        assert self.engine._scope_allows(
            bot, BotMessage(text="hi", is_dm=False, channel_key="A" * 32)
        )


class TestBuildMessage:
    async def test_room_post_is_not_a_dm(self, test_db):
        await seed_room()
        engine = BotEngine()
        msg, _hops = await engine._build_message(
            {
                "type": "PRIV",
                "conversation_key": ROOM_KEY,
                "sender_key": POSTER_KEY,
                "sender_name": "K0PHX",
                "text": "ping",
            }
        )
        assert msg.is_room
        assert not msg.is_dm
        assert msg.room_key == ROOM_KEY
        assert msg.room_name == "Ops Board"
        # The poster, not the room, is who spoke.
        assert msg.sender_key == POSTER_KEY
        assert msg.sender_name == "K0PHX"
        assert msg.channel_key is None

    async def test_unsigned_room_post_has_no_sender_key(self, test_db):
        # Ingest attributes an unsigned post to the room itself. Keeping that key
        # would let a bot DM the room believing it is the author.
        await seed_room()
        engine = BotEngine()
        msg, _hops = await engine._build_message(
            {
                "type": "PRIV",
                "conversation_key": ROOM_KEY,
                "sender_name": "Ops Board",
                "text": "ping",
            }
        )
        assert msg.is_room
        assert msg.sender_key is None

    async def test_plain_dm_is_still_a_dm(self, test_db):
        from app.repository import ContactRepository

        await ContactRepository.upsert(ContactUpsert(public_key=POSTER_KEY, name="K0PHX", type=0))
        engine = BotEngine()
        msg, _hops = await engine._build_message(
            {"type": "PRIV", "conversation_key": POSTER_KEY, "text": "ping"}
        )
        assert msg.is_dm
        assert not msg.is_room
        assert msg.room_key is None
        assert msg.sender_key == POSTER_KEY
        assert msg.sender_name == "K0PHX"

    async def test_outgoing_room_post_keeps_no_author(self, test_db):
        await seed_room()
        engine = BotEngine()
        msg, _hops = await engine._build_message(
            {
                "type": "PRIV",
                "conversation_key": ROOM_KEY,
                "text": "our own post",
                "outgoing": True,
            }
        )
        assert msg.is_room
        assert msg.is_outgoing
        assert msg.sender_key is None
        assert msg.sender_name is None


class TestReplyTarget:
    def make_ctx(self, **overrides) -> BotContext:
        base = {
            "bot_id": "b1",
            "bot_name": "t",
            "settings": {},
            "state": {},
            "origin_is_room": True,
            "origin_room_key": ROOM_KEY,
            "origin_sender_key": POSTER_KEY,
            "is_test": True,
        }
        base.update(overrides)
        return BotContext(**base)

    async def test_reply_goes_to_the_room_not_the_poster(self):
        ctx = self.make_ctx()
        await ctx.reply("pong")
        assert ctx.captured_sends == [
            {
                "is_dm": True,
                "destination": ROOM_KEY,
                "channel_key": None,
                "text": "pong",
                "region": None,
            }
        ]

    async def test_dm_origin_unchanged(self):
        ctx = self.make_ctx(origin_is_room=False, origin_room_key=None, origin_is_dm=True)
        await ctx.reply("pong")
        assert ctx.captured_sends[0]["destination"] == POSTER_KEY

    async def test_channel_origin_unchanged(self):
        ctx = self.make_ctx(
            origin_is_room=False,
            origin_room_key=None,
            origin_is_dm=False,
            origin_channel_key="A" * 32,
        )
        await ctx.reply("pong")
        assert ctx.captured_sends[0] == {
            "is_dm": False,
            "destination": None,
            "channel_key": "A" * 32,
            "text": "pong",
            "region": None,
        }

    async def test_room_reply_uses_the_dm_byte_budget(self):
        # A room post carries no "<name>: " framing, so it gets the full DM
        # budget rather than the shorter channel one.
        from app.imaging.aeic.transport import DEFAULT_MESSAGE_BUDGET

        assert await self.make_ctx()._resolve_split_budget() == DEFAULT_MESSAGE_BUDGET

    async def test_send_room_resolves_by_name(self, test_db):
        await seed_room()
        ctx = self.make_ctx()
        await ctx.send_room("Ops Board", "hello room")
        assert ctx.captured_sends[0]["destination"] == ROOM_KEY

    async def test_send_room_rejects_a_non_room(self, test_db):
        from app.repository import ContactRepository

        await ContactRepository.upsert(ContactUpsert(public_key=POSTER_KEY, name="K0PHX", type=0))
        with pytest.raises(ValueError, match="unknown room"):
            await self.make_ctx().send_room("K0PHX", "hello")


class TestLegacyBots:
    """A migrated ``def bot(**kwargs)`` source must keep working (AGENTS invariant)."""

    LEGACY = (
        "def bot(**kwargs):\n"
        "    if kwargs['is_dm']:\n"
        "        return 'private answer'\n"
        "    # Pre-rooms bots reasonably read 'not a DM' as 'a channel'.\n"
        "    return 'on ' + kwargs['channel_key'][:8] + '/' + str(kwargs['channel_name'])\n"
    )

    async def _run(self, msg: BotMessage) -> list[dict]:
        from concurrent.futures import ThreadPoolExecutor

        from app.bots.runtime import call_legacy, load_bot_code

        ctx = BotContext(
            bot_id="b",
            bot_name="b",
            settings={},
            state={},
            is_test=True,
            origin_is_dm=msg.is_dm,
            origin_sender_key=msg.sender_key,
            origin_channel_key=msg.channel_key,
            origin_is_room=msg.is_room,
            origin_room_key=msg.room_key,
        )
        with ThreadPoolExecutor(max_workers=1) as pool:
            await call_legacy(load_bot_code(self.LEGACY), ctx, msg, 10.0, pool)
        return ctx.captured_sends

    async def test_dm_and_channel_are_untouched(self):
        dm = await self._run(BotMessage(text="hi", is_dm=True, sender_key=POSTER_KEY))
        assert dm[0]["destination"] == POSTER_KEY
        assert dm[0]["text"] == "private answer"

        chan = await self._run(
            BotMessage(text="hi", is_dm=False, channel_key="A" * 32, channel_name="#g")
        )
        assert chan[0]["channel_key"] == "A" * 32
        assert chan[0]["text"] == "on AAAAAAAA/#g"

    async def test_a_room_arrives_as_that_room_not_as_a_none_channel(self):
        # The legacy signature has no room of its own, so the room stands in as
        # the conversation. Handing it None here made the bot raise inside the
        # legacy executor, which swallows it — a working bot going quiet with no
        # reply and nothing in Bots › Logs.
        sends = await self._run(room_msg())
        assert sends[0]["text"] == f"on {ROOM_KEY[:8]}/Ops Board"
        # The answer still goes into the room, like any other bot's.
        assert sends[0]["is_dm"] is True
        assert sends[0]["destination"] == ROOM_KEY


class TestEchoGuard:
    def _engine_with_spy(self, monkeypatch, self_key):
        engine = BotEngine()
        engine._started = True
        spawned: list[str] = []
        monkeypatch.setattr(engine, "_self_key", lambda: self_key)
        monkeypatch.setattr(
            engine,
            "_spawn_run",
            lambda loaded, trigger, msg, **kw: spawned.append(trigger),
        )
        loaded = _loaded_catch_all_bot()
        engine.bots = {loaded.record.id: loaded}
        return engine, spawned

    async def test_our_own_relayed_post_is_ignored(self, monkeypatch, test_db):
        # The room server relays every post to everyone logged in, us included.
        # Reacting to our own reply would let two bots answer each other forever.
        await seed_room()
        engine, spawned = self._engine_with_spy(monkeypatch, OUR_KEY)
        await engine._handle_message(
            {
                "type": "PRIV",
                "conversation_key": ROOM_KEY,
                "sender_key": OUR_KEY,
                "text": "pong",
            }
        )
        assert spawned == []

    async def test_someone_elses_post_still_runs(self, monkeypatch, test_db):
        await seed_room()
        engine, spawned = self._engine_with_spy(monkeypatch, OUR_KEY)
        await engine._handle_message(
            {
                "type": "PRIV",
                "conversation_key": ROOM_KEY,
                "sender_key": POSTER_KEY,
                "text": "ping",
            }
        )
        assert spawned == ["message"]


def _loaded_catch_all_bot():
    from app.bots.engine import LoadedBot
    from app.bots.runtime import load_bot_code

    code = load_bot_code(
        "from remoteterm import bot\n\n@bot.on_message()\nasync def seen(ctx, msg):\n    pass\n"
    )
    return LoadedBot(record=make_bot(scope={"channels": "all", "rooms": "all"}), code=code)


class TestRepository:
    async def test_room_selection_roundtrip(self, test_db):
        from app.repository.bots import BotRepository

        bot = await BotRepository.create(
            name="rooms-test", scope={"channels": "all", "rooms": {"only": [ROOM_KEY]}}
        )
        assert bot.scope["rooms"] == {"only": [ROOM_KEY]}
        updated = await BotRepository.update(
            bot.id, scope={"channels": "all", "rooms": {"except": [ROOM_KEY]}}
        )
        assert updated is not None
        assert updated.scope["rooms"] == {"except": [ROOM_KEY]}


class TestMigration:
    async def _run(self, test_db, rows):
        from app.migrations._080_bot_room_scope import migrate

        async with test_db.tx() as conn:
            for bot_id, answers_dms, scope in rows:
                await conn.execute(
                    "INSERT INTO bots (id, name, respond_to_dms, scope, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, 0, 0)",
                    (bot_id, bot_id, answers_dms, scope),
                )
        async with test_db.tx() as conn:
            await migrate(conn)

    async def test_inherits_respond_to_dms(self, test_db):
        # Existing bots keep the reach they had: room posts used to pass the DM
        # gate, so that is what the new selection starts from.
        from app.repository.bots import BotRepository

        await self._run(
            test_db,
            [
                ("legacy-off", 0, '{"channels": "all"}'),
                ("legacy-on", 1, '{"channels": "all"}'),
            ],
        )
        off = await BotRepository.get("legacy-off")
        on = await BotRepository.get("legacy-on")
        assert off is not None and off.scope["rooms"] == {"only": []}
        assert on is not None and on.scope["rooms"] == "all"
        # The channel half is left exactly as it was.
        assert on.scope["channels"] == "all"

    async def test_leaves_a_scope_that_already_names_rooms(self, test_db):
        from app.repository.bots import BotRepository

        await self._run(test_db, [("picked", 0, '{"channels": "all", "rooms": {"only": ["ab"]}}')])
        bot = await BotRepository.get("picked")
        assert bot is not None and bot.scope["rooms"] == {"only": ["ab"]}

    async def test_survives_an_unreadable_scope(self, test_db):
        from app.repository.bots import BotRepository

        await self._run(test_db, [("broken", 1, "not json"), ("fine", 1, '{"channels": "all"}')])
        broken = await BotRepository.get("broken")
        fine = await BotRepository.get("fine")
        # An unreadable scope falls back to the default rather than crashing the
        # migration, and its neighbour is still migrated.
        assert broken is not None
        assert fine is not None and fine.scope["rooms"] == "all"
