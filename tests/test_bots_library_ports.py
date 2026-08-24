"""Tests for the trickier ported library bots (alert, mowas, worldcup-live, mesh admin)."""

import asyncio
import base64
import hashlib
import json
import os
import time

from app.bots.api import BotContext, BotMessage
from app.bots.engine import BotEngine
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code
from app.models import BotTestRequest


def _load_namespace(key: str) -> dict:
    entry = get_library_entry(key)
    assert entry is not None, f"library bot {key} missing"
    return load_bot_code(entry["code"]).namespace


class TestPulsePointDecrypt:
    def test_roundtrip_against_reference_scheme(self):
        """Encrypt a sample payload the way PulsePoint does; the bot must decrypt it."""
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad

        ns = _load_namespace("alert")
        derive_key = ns["_derive_key"]
        decrypt = ns["_decrypt"]

        payload = {"incidents": {"active": [{"ID": "1", "PulsePointIncidentCallType": "SF"}]}}
        # PulsePoint wraps the JSON in quotes with escaped inner quotes.
        plaintext = '"' + json.dumps(payload).replace('"', '\\"') + '"'
        salt = os.urandom(8)
        iv = os.urandom(16)
        cipher = AES.new(derive_key(salt), AES.MODE_CBC, iv)
        ciphertext = cipher.encrypt(pad(plaintext.encode(), 16))

        envelope = {
            "ct": base64.b64encode(ciphertext).decode(),
            "iv": iv.hex(),
            "s": salt.hex(),
        }
        assert decrypt(envelope) == payload

    def test_key_derivation_is_deterministic(self):
        ns = _load_namespace("alert")
        derive_key = ns["_derive_key"]
        salt = bytes.fromhex("0011223344556677")
        key = derive_key(salt)
        assert len(key) == 32
        assert key == derive_key(salt)
        # EVP_BytesToKey with MD5: first block is md5(password + salt).
        e = "CommonIncidents"
        password = e[13] + e[1] + e[2] + "brady" + "5" + "r" + e.lower()[6] + e[5] + "gs"
        assert key[:16] == hashlib.md5(password.encode() + salt).digest()


class TestMowasExtraction:
    def test_cap_json_shape(self):
        extract = _load_namespace("mowas")["_extract"]
        result = extract(
            {
                "info": [
                    {"language": "de-DE", "headline": "Unwetterwarnung Stufe Rot"},
                    {"language": "en-US", "headline": "Severe weather warning"},
                ]
            }
        )
        assert result == [
            ("de-de", "Unwetterwarnung Stufe Rot"),
            ("en-us", "Severe weather warning"),
        ]

    def test_cap_xml_shape(self):
        extract = _load_namespace("mowas")["_extract"]
        xml = (
            '<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">'
            "<info><language>de-DE</language><headline>Bombenfund</headline>"
            "<description>Evakuierung</description></info></alert>"
        )
        result = extract({"cap_xml": xml})
        assert result == [("de-de", "Bombenfund")]

    def test_simple_shape_and_garbage(self):
        extract = _load_namespace("mowas")["_extract"]
        assert extract({"headline": "Warnung"}) == [("de", "Warnung")]
        assert extract({}) == []
        assert extract({"cap_xml": "<not-xml"}) == []


class TestWorldcupSummarize:
    def test_summarize_event(self):
        summarize = _load_namespace("worldcup_live")["_summarize"]
        event = {
            "id": "401",
            "status": {"type": {"state": "in"}},
            "competitions": [
                {
                    "competitors": [
                        {"homeAway": "home", "score": "2", "team": {"abbreviation": "ARG"}},
                        {"homeAway": "away", "score": "1", "team": {"abbreviation": "FRA"}},
                    ]
                }
            ],
        }
        assert summarize(event) == ("401", "ARG 2-1 FRA", "in")
        assert summarize({"competitions": []}) is None


class TestMentionPattern:
    """Bots that address the sender use the @[name] mention syntax clients recognize."""

    async def _test_run(self, test_db, key: str, request: BotTestRequest):
        from app.repository.bots import BotRepository

        entry = get_library_entry(key)
        assert entry is not None
        bot = await BotRepository.create(name=f"{key}-mentiontest", code=entry["code"])
        engine = BotEngine()
        return await engine.test_run(bot, request)

    async def test_ping_mentions_the_sender(self, test_db):
        response = await self._test_run(
            test_db, "ping", BotTestRequest(text="ping", sender_name="K0PHX")
        )
        assert response.error is None
        assert response.replies[0]["text"].startswith("@[K0PHX] ")

    async def test_signal_report_prefixes_a_mention(self, test_db):
        response = await self._test_run(
            test_db, "test", BotTestRequest(text="test", sender_name="K0PHX")
        )
        assert response.error is None
        assert response.replies[0]["text"].startswith("@[K0PHX]: ")

    async def test_roll_names_the_roller_as_a_mention(self, test_db):
        response = await self._test_run(
            test_db, "roll", BotTestRequest(text="roll 20", sender_name="K0PHX")
        )
        assert response.error is None
        assert response.replies[0]["text"].startswith("@[K0PHX] rolled ")

    async def test_missing_sender_name_stays_plain(self, test_db):
        response = await self._test_run(
            test_db, "ping", BotTestRequest(text="ping", sender_name="")
        )
        assert response.error is None
        assert "@[" not in response.replies[0]["text"]


class TestMultitest:
    async def test_counts_only_the_window_and_collapses_flood_stages(self, test_db):
        conn = test_db.conn
        now = int(time.time())

        # Outside the window — must not be counted (the old code returned the
        # newest 100 rows of all time because after= was ignored without
        # after_id=).
        await conn.execute(
            "INSERT INTO messages (type, conversation_key, text, received_at, paths, outgoing)"
            " VALUES ('CHAN', ?, 'old traffic', ?, ?, 0)",
            ("AA" * 16, now - 3600, json.dumps([{"path": "beef", "received_at": now - 3600}])),
        )
        # In the window: one message heard at three flood stages of the same
        # route (each repeat appends one hop) plus one genuinely distinct route.
        await conn.execute(
            "INSERT INTO messages (type, conversation_key, text, received_at, paths, outgoing)"
            " VALUES ('CHAN', ?, 'test one', ?, ?, 0)",
            (
                "AA" * 16,
                now + 5,
                json.dumps(
                    [
                        {"path": "2f52", "received_at": now + 5, "path_len": 2},
                        {"path": "2f52f0", "received_at": now + 6, "path_len": 3},
                        {"path": "2f52f0bf", "received_at": now + 7, "path_len": 4},
                        {"path": "aabb", "received_at": now + 6, "path_len": 2},
                    ]
                ),
            ),
        )
        # In the window: a zero-hop (direct) arrival.
        await conn.execute(
            "INSERT INTO messages (type, conversation_key, text, received_at, outgoing)"
            " VALUES ('CHAN', ?, 'test two', ?, 0)",
            ("AA" * 16, now + 5),
        )
        await conn.commit()

        entry = get_library_entry("multitest")
        assert entry is not None
        loaded = load_bot_code(entry["code"])
        loaded.namespace["WINDOW_SECONDS"] = 0  # skip the collection sleep
        handler = loaded.collector.keywords[0].handler

        ctx = BotContext(
            bot_id="mt",
            bot_name="multitest",
            settings={},
            state={},
            is_test=True,
            loop=asyncio.get_event_loop(),
            origin_is_dm=True,
            origin_sender_key="ab" * 32,
        )
        await handler(ctx, BotMessage(text="multitest", is_dm=True, sender_key="ab" * 32))

        assert len(ctx.captured_sends) == 1
        text = ctx.captured_sends[0]["text"]
        # 2f52 and 2f52f0 are flood stages of 2f52f0bf; aabb and direct stand.
        # Routes render as comma-separated repeater hops (1-byte hops here).
        assert text.startswith("3 unique path(s)")
        assert "2f,52,f0,bf" in text
        assert "aa,bb" in text
        assert "direct" in text
        assert "2f,52 |" not in text and "2f,52,f0 |" not in text
        assert "be,ef" not in text and "beef" not in text


class TestMeshAdminBots:
    async def _test_run(self, test_db, key: str, request: BotTestRequest):
        from app.repository.bots import BotRepository

        entry = get_library_entry(key)
        assert entry is not None
        bot = await BotRepository.create(name=f"{key}-porttest", code=entry["code"])
        engine = BotEngine()
        return await engine.test_run(bot, request)

    async def test_status_requires_dm(self, test_db):
        response = await self._test_run(test_db, "status", BotTestRequest(text="status"))
        assert response.matched
        assert response.replies, response.error
        assert "direct message" in response.replies[0]["text"]

    async def test_status_reports_in_dm(self, test_db):
        response = await self._test_run(
            test_db, "status", BotTestRequest(text="status", is_dm=True, sender_key="ab" * 32)
        )
        assert response.error is None
        assert any("Radio" in r["text"] for r in response.replies)
        assert any("Bot engine" in r["text"] for r in response.replies)

    async def test_neighbors_lists_zero_hop(self, test_db):
        conn = test_db.conn
        await conn.execute(
            "INSERT INTO contacts (public_key, name, type, direct_path_len, last_seen)"
            " VALUES (?, ?, ?, ?, strftime('%s','now'))",
            ("aa" * 32, "NearNode", 1, 0),
        )
        await conn.execute(
            "INSERT INTO contacts (public_key, name, type, direct_path_len) VALUES (?, ?, ?, ?)",
            ("bb" * 32, "FarNode", 1, 3),
        )
        await conn.commit()
        response = await self._test_run(test_db, "neighbors", BotTestRequest(text="neighbors"))
        assert response.error is None
        text = response.replies[0]["text"]
        assert "NearNode" in text
        assert "FarNode" not in text

    async def test_repeater_stats(self, test_db):
        conn = test_db.conn
        await conn.execute(
            "INSERT INTO contacts (public_key, name, type, direct_path_len,"
            " direct_path_hash_mode, last_seen) VALUES (?, ?, 2, 0, 2, strftime('%s','now'))",
            ("cc" * 32, "Repeater One"),
        )
        await conn.commit()
        response = await self._test_run(
            test_db,
            "repeater",
            BotTestRequest(text="repeater stats", is_dm=True, sender_key="ab" * 32),
        )
        assert response.error is None
        text = response.replies[0]["text"]
        assert "1 known" in text
        assert "1 multibyte" in text

    async def test_trace_is_guarded_in_test_runs(self, test_db):
        response = await self._test_run(
            test_db, "trace", BotTestRequest(text="trace a1b2", is_dm=True)
        )
        assert response.error is None
        assert "not transmitted" in response.replies[0]["text"]


class TestLongReplySplitting:
    """Library bots hand long text to ctx.reply_split instead of hand-rolling it.

    Before this migration each of these bots carried its own splitter or a hard
    ``[:180]`` cut, so a long answer was either re-implemented per bot or lost.
    """

    MIGRATED = (
        "mailbox",
        "help",
        "cmd",
        "channels",
        "sports",
        "neighbors",
        "repeater",
        "trace",
        "gwx",
        "worldcup",
        "wx",
    )

    async def _test_run(self, key: str, request: BotTestRequest, settings=None):
        from app.repository.bots import BotRepository

        entry = get_library_entry(key)
        assert entry is not None
        bot = await BotRepository.create(
            name=f"{key}-splittest", code=entry["code"], settings=settings or {}
        )
        return await BotEngine().test_run(bot, request)

    def test_migrated_bots_use_reply_split(self):
        for key in self.MIGRATED:
            entry = get_library_entry(key)
            assert entry is not None, key
            assert "ctx.reply_split(" in entry["code"], f"{key} still splits by hand"

    def test_no_library_bot_reimplements_the_splitter(self):
        """``(i/n)`` numbering belongs to ctx.reply_split, not to bot code."""
        from app.bots.library import list_library

        for entry in list_library():
            assert "({i}/{total})" not in entry["code"], f"{entry['key']} hand-numbers parts"

    async def test_cmd_lists_every_command_across_parts(self, test_db):
        response = await self._test_run("cmd", BotTestRequest(text="cmd"))
        assert response.error is None
        # Nothing is dropped and nothing is advertised as omitted.
        joined = " ".join(r["text"] for r in response.replies)
        assert "more — see the Bots tab" not in joined

    async def test_help_splits_a_long_command_list(self, test_db):
        """A command list past one frame goes out numbered, never cut with '…'."""
        from app.bots.api import DEFAULT_SPLIT_BYTES

        entry = get_library_entry("help")
        assert entry is not None
        loaded = load_bot_code(entry["code"])
        handler = loaded.collector.keywords[0].handler

        ctx = BotContext(
            bot_id="help",
            bot_name="help",
            settings={},
            state={},
            is_test=True,
            loop=asyncio.get_event_loop(),
            origin_is_dm=True,
            origin_sender_key="ab" * 32,
        )
        ctx.get_enabled_bots = lambda: [  # type: ignore[method-assign]
            {"name": f"bot{i}", "description": "d", "keywords": [f"keyword{i:02d}"]}
            for i in range(40)
        ]
        await handler(ctx, BotMessage(text="help", is_dm=True, sender_key="ab" * 32))

        texts = [s["text"] for s in ctx.captured_sends]
        # Last message is the unchanged hint; the list before it is numbered.
        assert texts[-1] == "Say 'help <command>' for details."
        parts = texts[:-1]
        assert len(parts) > 1, "expected the long list to split"
        assert [p.split("/")[0] for p in parts] == [f"({i}" for i in range(1, len(parts) + 1)]
        assert "…" not in " ".join(parts), "list must not be truncated any more"
        for part in parts:
            assert len(part.encode("utf-8")) <= DEFAULT_SPLIT_BYTES
        # Every keyword survives.
        joined = " ".join(p.split(") ", 1)[1] for p in parts)
        for i in range(40):
            assert f"keyword{i:02d}" in joined


class TestMailbox:
    """Mailbox builds one unsplit string per command; the entrypoint splits it.

    ``_bot_inner`` runs in a worker thread and cannot await, so every ``_cmd_*``
    branch returns raw text and ``mailbox()`` hands it to ctx.reply_split. These
    tests pin the two things that migration could silently break: a branch
    forgetting to return, and the raw ``mbx test`` probe getting renumbered.
    """

    async def _mailbox(self, tmp_path, settings=None):
        """One mailbox bot record; run as many messages through it as needed."""
        from app.repository.bots import BotRepository

        entry = get_library_entry("mailbox")
        assert entry is not None
        base = {"db_path": str(tmp_path / "mailbox.db"), "prefix": "mbx"}
        record = await BotRepository.create(
            name="mailbox-splittest", code=entry["code"], settings={**base, **(settings or {})}
        )
        engine = BotEngine()

        async def run(text: str, sender_key: str = "ab" * 32):
            response = await engine.test_run(
                record, BotTestRequest(text=text, is_dm=True, sender_key=sender_key)
            )
            assert response.error is None, response.error
            return [r["text"] for r in response.replies]

        return run

    async def test_help_goes_out_as_numbered_parts(self, test_db, tmp_path):
        run = await self._mailbox(tmp_path)
        texts = await run("mbx help")
        assert len(texts) > 1, "the full guide does not fit one frame"
        assert [t.split("/")[0] for t in texts] == [f"({i}" for i in range(1, len(texts) + 1)]
        for text in texts:
            assert len(text.encode("utf-8")) <= 155

    async def test_response_budget_setting_still_sizes_the_parts(self, test_db, tmp_path):
        run = await self._mailbox(tmp_path, settings={"response_budget": 60})
        for text in await run("mbx help"):
            assert len(text.encode("utf-8")) <= 60

    async def test_short_reply_is_a_single_unnumbered_message(self, test_db, tmp_path):
        run = await self._mailbox(tmp_path)
        await run("mbx accept")
        texts = await run("mbx bogus")
        assert len(texts) == 1
        assert texts[0].startswith("Unknown command.")

    async def test_consent_gate_answers_alone(self, test_db, tmp_path):
        """A gated branch must return, not fall through into a later command."""
        run = await self._mailbox(tmp_path)
        texts = await run("mbx inbox")
        assert len(texts) == 1
        assert "UNENCRYPTED" in texts[0]

    async def test_size_probe_is_sent_raw_in_one_message(self, test_db, tmp_path):
        """`mbx test N` measures the link — reply_split must not touch it."""
        run = await self._mailbox(tmp_path)
        assert "unlocked" in (await run("mbx debug CHANGE-ME-s3cret"))[0]

        texts = await run("mbx test 300")
        assert len(texts) == 1, "the probe must not be split"
        assert len(texts[0].encode("utf-8")) == 300
        assert texts[0].startswith("TEST 300B ")

    async def test_stored_message_plays_back_whole(self, test_db, tmp_path):
        run = await self._mailbox(tmp_path)
        sender = "ab" * 32
        recipient = "cd" * 32
        body = " ".join(f"word{i:03d}" for i in range(80))

        await run("mbx accept", sender_key=sender)
        await run(f"mbx msg {recipient} {body}", sender_key=sender)
        await run("mbx accept", sender_key=recipient)
        texts = await run("mbx play", sender_key=recipient)

        assert len(texts) > 1
        joined = " ".join(t.split(") ", 1)[1] for t in texts)
        for i in range(80):
            assert f"word{i:03d}" in joined, f"word{i:03d} lost in playback"
