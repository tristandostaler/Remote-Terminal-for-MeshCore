"""The ``tinyllm`` bot and its tiny-LLM runtime (``app/bots/llm.py``).

No test downloads or runs a real model: the runtime singleton is stubbed for
the bot tests, and the runtime tests start the real model *process* with a fake
``llama_cpp`` package first on its path (see ``fake_llama``).
"""

import os
import textwrap
import time
from pathlib import Path

import pytest

from app.bots import llm
from app.bots.engine import BotEngine
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code
from app.models import BotTestRequest


@pytest.fixture(autouse=True)
def _fresh_dm_sessions(monkeypatch):
    """Each test starts with no remembered DM conversations."""
    monkeypatch.setattr(llm, "dm_sessions", llm.DmSessions())


class TestCatalog:
    def test_keys_are_unique_and_default_exists(self):
        keys = [spec.key for spec in llm.CATALOG]
        assert len(keys) == len(set(keys))
        assert llm.DEFAULT_MODEL in keys

    def test_every_option_states_download_and_memory(self):
        options = llm.model_options()
        assert [o["value"] for o in options][-1] == llm.CUSTOM_MODEL
        for option in options[:-1]:
            assert "download" in option["label"] and "RAM" in option["label"], option
            assert "RAM" in option["description"] and "huggingface.co/" in option["description"]

    def test_urls_point_at_gguf_files(self):
        for spec in llm.CATALOG:
            assert spec.filename.endswith(".gguf")
            assert spec.url.startswith(f"https://huggingface.co/{spec.repo}/resolve/main/")

    def test_resolve_spec(self):
        assert llm.resolve_spec({}).key == llm.DEFAULT_MODEL
        custom = llm.resolve_spec(
            {"model": "custom", "custom_repo": "me/models", "custom_file": "tiny.gguf"}
        )
        assert custom.repo == "me/models" and custom.filename == "tiny.gguf"
        for bad in (
            {"model": "nope"},
            {"model": "custom", "custom_repo": "no-owner", "custom_file": "x.gguf"},
            {"model": "custom", "custom_repo": "a/b", "custom_file": "x.bin"},
            {"model": "custom", "custom_repo": "a/b", "custom_file": "../x.gguf"},
        ):
            with pytest.raises(ValueError):
                llm.resolve_spec(bad)


class TestBotMeta:
    def test_library_entry(self):
        entry = get_library_entry("tinyllm")
        assert entry is not None
        assert not entry.get("enabled_by_default"), "feature bots ship disabled"
        assert "scope" not in entry, "keeps the default #bot / #bots + DMs scope"
        assert entry.get("respond_to_dms", True)
        model_field = next(f for f in entry["settings_schema"] if f["key"] == "model")
        assert model_field["type"] == "select"
        assert model_field["options"] == llm.model_options()

    def test_keywords(self):
        entry = get_library_entry("tinyllm")
        assert set(load_bot_code(entry["code"]).declared_keywords) == {
            "ask",
            "ai",
            "llm",
            "tinyllm",
        }


class _FakeRuntime:
    def __init__(self, state="ready", answer="Paris is the capital of France."):
        self.state = state
        self.answer = answer
        self.asked: list = []
        self.state_after_wait = state

    def ensure(self, spec, threads=0, repack=False, idle_unload_seconds=300):
        self.ensured = (spec, threads, repack)
        self.idle_unload_seconds = idle_unload_seconds
        return self.state

    def wait_ready(self, timeout):
        self.waited = timeout
        self.state = self.state_after_wait
        return self.state

    def describe(self, detailed=False):
        self.detailed = detailed
        return "RAW DETAIL" if detailed else "Downloading Qwen2.5 0.5B: 42% of 468 MB"

    def generate(self, messages, **kwargs):
        self.asked.append((messages, kwargs))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


async def _run(monkeypatch, runtime, request, settings=None, admin_key=None):
    from app.repository.bots import BotRepository

    monkeypatch.setattr(llm, "llm_runtime", runtime)
    entry = get_library_entry("tinyllm")
    name, suffix = "tinyllm-test", 2
    while await BotRepository.name_exists(name):
        name, suffix = f"tinyllm-test-{suffix}", suffix + 1
    bot = await BotRepository.create(name=name, code=entry["code"], settings=settings or {})
    engine = BotEngine()
    if admin_key:
        from app.models import BotAdminUser

        engine.settings.admin_users = [BotAdminUser(public_key=admin_key)]
    response = await engine.test_run(bot, request)
    assert response.error is None, response.error
    return [r["text"] for r in response.replies]


class TestAskBot:
    async def test_answers_in_a_channel_with_a_mention(self, test_db, monkeypatch):
        runtime = _FakeRuntime()
        replies = await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask capital of France?", sender_name="K0PHX"),
        )
        assert replies == ["@[K0PHX] Paris is the capital of France."]
        messages, kwargs = runtime.asked[0]
        assert messages[-1] == {"role": "user", "content": "capital of France?"}
        assert kwargs["deadline_seconds"] <= 7

    async def _prompt(self, monkeypatch, settings):
        runtime = _FakeRuntime()
        await _run(monkeypatch, runtime, BotTestRequest(text="ask hi"), settings=settings)
        return runtime.asked[0][0][0]["content"]

    async def test_the_prompt_follows_the_model(self, test_db, monkeypatch):
        assert (
            await self._prompt(monkeypatch, {})
            == llm.CATALOG_BY_KEY[llm.DEFAULT_MODEL].system_prompt
        )
        for spec in llm.CATALOG:
            prompt = await self._prompt(monkeypatch, {"model": spec.key})
            assert prompt == spec.system_prompt, spec.key

    async def test_a_custom_prompt_survives_model_changes(self, test_db, monkeypatch):
        custom = {"prompt_mode": "custom", "system_prompt": "Talk like a pirate."}
        for spec in llm.CATALOG:
            prompt = await self._prompt(monkeypatch, {**custom, "model": spec.key})
            assert prompt == "Talk like a pirate.", spec.key
        # "Match the model" ignores text left in the custom box.
        match = {"prompt_mode": "model", "system_prompt": "Talk like a pirate."}
        assert await self._prompt(monkeypatch, match) != "Talk like a pirate."
        # Custom with nothing written falls back to the model's prompt.
        empty = await self._prompt(monkeypatch, {"prompt_mode": "custom", "system_prompt": ""})
        assert empty == llm.CATALOG_BY_KEY[llm.DEFAULT_MODEL].system_prompt

    async def test_settings_from_before_prompt_modes(self, test_db, monkeypatch):
        """No prompt_mode stored: an old default means "match the model", a
        hand-written prompt means "custom" -- never erased."""
        for old in (
            "You are a helpful assistant on a low-bandwidth mesh radio network. "
            "Answer in one or two short sentences, plain text, no markdown, "
            "under 200 characters.",
            "You are a helpful assistant on a low-bandwidth mesh radio network. "
            "Answer in one or two short sentences, plain text, no markdown, "
            "under 140 characters.",
            "",
        ):
            prompt = await self._prompt(monkeypatch, {"system_prompt": old})
            assert prompt == llm.CATALOG_BY_KEY[llm.DEFAULT_MODEL].system_prompt
        assert await self._prompt(monkeypatch, {"system_prompt": "Be a pirate."}) == "Be a pirate."

    async def test_too_long_question_gets_a_clear_reply(self, test_db, monkeypatch):
        runtime = _FakeRuntime(answer=llm.LlmPromptTooLongError("exceed context window"))
        replies = await _run(monkeypatch, runtime, BotTestRequest(text="ask " + "🙂" * 400))
        assert replies == ["🤖 That question is too long for this model, try a shorter one."]

    async def test_weight_repacking_is_off_unless_asked_for(self, test_db, monkeypatch):
        runtime = _FakeRuntime()
        await _run(monkeypatch, runtime, BotTestRequest(text="ask hi"))
        assert runtime.ensured[2] is False
        runtime = _FakeRuntime()
        await _run(
            monkeypatch, runtime, BotTestRequest(text="ask hi"), settings={"fast_arm_layout": True}
        )
        assert runtime.ensured[2] is True

    async def test_a_reload_is_waited_for_and_answered_in_the_same_run(self, test_db, monkeypatch):
        runtime = _FakeRuntime(state="loading")
        runtime.state_after_wait = "ready"
        replies = await _run(monkeypatch, runtime, BotTestRequest(text="ask hi", is_dm=True))
        assert replies == ["Paris is the capital of France."]
        assert runtime.waited <= 4
        # The reload's time came out of the answer's budget, never past the run.
        assert runtime.asked[0][1]["deadline_seconds"] <= 7.5

    async def test_idle_unload_setting_reaches_the_runtime(self, test_db, monkeypatch):
        runtime = _FakeRuntime()
        await _run(monkeypatch, runtime, BotTestRequest(text="ask hi"))
        assert runtime.idle_unload_seconds == 300
        runtime = _FakeRuntime()
        await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask hi"),
            settings={"unload_after_minutes": 0},
        )
        assert runtime.idle_unload_seconds == 0

    async def test_a_killed_model_says_why(self, test_db, monkeypatch):
        died = llm.LlmWorkerDiedError(llm._exit_reason(-9))
        replies = await _run(
            monkeypatch, _FakeRuntime(answer=died), BotTestRequest(text="ask hi", is_dm=True)
        )
        assert "memory ran out" in replies[0]

    async def test_raw_errors_only_reach_an_admin_in_a_dm(self, test_db, monkeypatch):
        admin = "ab" * 32
        cases = [
            # (request, admin configured, sees raw detail)
            (BotTestRequest(text="ask hi", is_dm=True, sender_key=admin), True, True),
            (BotTestRequest(text="ask hi", is_dm=True, sender_key="cd" * 32), True, False),
            (BotTestRequest(text="ask hi", sender_key=admin), True, False),  # channel
            (BotTestRequest(text="ask hi", is_dm=True, sender_key=admin), False, False),
        ]
        for request, configured, raw in cases:
            runtime = _FakeRuntime(state="error")
            replies = await _run(
                monkeypatch, runtime, request, admin_key=admin if configured else None
            )
            assert runtime.detailed is raw, request
            assert ("RAW DETAIL" in replies[0]) is raw, request

    async def test_dm_answer_has_no_mention(self, test_db, monkeypatch):
        replies = await _run(
            monkeypatch,
            _FakeRuntime(answer="  Hi\nthere  "),
            BotTestRequest(text="ai hello", is_dm=True, sender_key="ab" * 32),
        )
        assert replies == ["Hi there"]

    async def test_warming_up_says_so(self, test_db, monkeypatch):
        replies = await _run(
            monkeypatch, _FakeRuntime(state="downloading"), BotTestRequest(text="ask hi")
        )
        assert "Warming up" in replies[0] and "42%" in replies[0]

    async def test_bare_ask_reports_readiness(self, test_db, monkeypatch):
        replies = await _run(monkeypatch, _FakeRuntime(), BotTestRequest(text="ask"))
        assert "is ready" in replies[0]

    async def test_bad_custom_model_is_explained(self, test_db, monkeypatch):
        replies = await _run(
            monkeypatch,
            _FakeRuntime(),
            BotTestRequest(text="ask hi"),
            settings={"model": "custom", "custom_repo": "", "custom_file": ""},
        )
        assert "custom model needs" in replies[0]

    async def test_a_rambling_answer_is_capped_to_one_message(self, test_db, monkeypatch):
        answer = "Hello there. " + " ".join(["word"] * 80) + "."
        replies = await _run(
            monkeypatch, _FakeRuntime(answer=answer), BotTestRequest(text="ask x", is_dm=True)
        )
        assert len(replies) == 1
        assert len(replies[0].encode()) <= 156

    async def test_more_messages_when_allowed(self, test_db, monkeypatch):
        answer = " ".join(["word"] * 80)
        replies = await _run(
            monkeypatch,
            _FakeRuntime(answer=answer),
            BotTestRequest(text="ask x", is_dm=True),
            settings={"max_messages": 3},
        )
        assert 1 < len(replies) <= 3
        assert all(len(r.encode()) <= 156 for r in replies)


class TestFitMessages:
    def _fit(self, text, budget=60, n=1):
        return _bot_namespace()["fit_messages"](text, budget, n)

    def test_short_text_is_untouched(self):
        assert self._fit("Hi there.") == "Hi there."

    def test_cuts_at_a_sentence_end(self):
        text = "The first sentence is here. The second one is far too long to fit in it."
        assert self._fit(text) == "The first sentence is here."

    def test_falls_back_to_a_word_boundary(self):
        out = self._fit("word " * 40)
        assert out.endswith("\u2026") and len(out.encode()) <= 60
        assert not out[:-1].endswith(" ")

    def test_multibyte_text_never_breaks_a_character(self):
        out = self._fit("🙂" * 100)
        out.encode()  # would raise on a broken surrogate
        assert len(out.encode()) <= 60


class TestSettingsMigration:
    async def test_seeding_migrates_stored_settings_on_refresh(self, test_db):
        """A refresh replaces code and schema but never settings; migrate_settings
        brings them in line so the Settings tab shows what the bot does."""
        from app.bots.library import ensure_seeded
        from app.repository.bots import BotRepository

        entry = get_library_entry("tinyllm")
        old_default = (
            "You are a helpful assistant on a low-bandwidth mesh radio network. "
            "Answer in one or two short sentences, plain text, no markdown, "
            "under 140 characters."
        )
        existing = await BotRepository.get_by_builtin_key("tinyllm")
        if existing is None:
            existing = await BotRepository.create(
                name="tinyllm", code=entry["code"], builtin_key="tinyllm"
            )
        await BotRepository.update(
            existing.id,
            builtin_version="1.1.1",
            settings={"system_prompt": old_default, "max_tokens": 64, "model": "gemma3-270m"},
        )
        await ensure_seeded()
        migrated = (await BotRepository.get(existing.id)).settings
        assert migrated["prompt_mode"] == "model"
        assert migrated["system_prompt"] == ""
        assert migrated["max_tokens"] == 40
        assert migrated["model"] == "gemma3-270m"

        await BotRepository.update(
            existing.id,
            builtin_version="1.1.1",
            settings={"system_prompt": "Talk like a pirate.", "max_tokens": 80},
        )
        await ensure_seeded()
        kept = (await BotRepository.get(existing.id)).settings
        assert kept == {
            "prompt_mode": "custom",
            "system_prompt": "Talk like a pirate.",
            "max_tokens": 80,
        }

    def test_model_details_show_the_default_prompt(self):
        for option in llm.model_options()[:-1]:
            spec = llm.CATALOG_BY_KEY[option["value"]]
            assert spec.system_prompt in option["description"]


def _bot_namespace():
    return load_bot_code(get_library_entry("tinyllm")["code"]).namespace


_FAKE_LLAMA = textwrap.dedent(
    """
    import os, time
    from llama_cpp import llama_cpp as low

    class Llama:
        def __init__(self, model_path, **kwargs):
            if "broken" in model_path:
                raise RuntimeError("bad model file")
            self.kwargs = kwargs
            self.repacking = low.llama_model_default_params().use_extra_bufts

        def create_chat_completion(self, messages, **kwargs):
            text = messages[-1]["content"]
            if text == "too long":
                raise ValueError("Requested tokens (900) exceed context window of 512")
            if text == "die":
                os.kill(os.getpid(), 9)  # what the OOM killer does
            if text == "config":
                words = [str(self.kwargs["n_ctx"]), " ", str(self.repacking)]
            elif text == "slow":
                words = iter(lambda: time.sleep(0.05) or "a", None)
            else:
                words = ["Hel", "lo"]
            return ({"choices": [{"delta": {"content": w}}]} for w in words)
    """
)
_FAKE_LOW = textwrap.dedent(
    """
    class _Params:
        use_extra_bufts = True

    class llama_model_params:
        _fields_ = [("use_extra_bufts", bool)]

    def llama_model_default_params():
        return _Params()
    """
)


@pytest.fixture
def fake_llama(tmp_path):
    package = tmp_path / "fake" / "llama_cpp"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(_FAKE_LLAMA)
    (package / "llama_cpp.py").write_text(_FAKE_LOW)
    env = dict(os.environ)
    env["PYTHONPATH"] = str(package.parent)
    return env


def _spec(filename="tiny.gguf"):
    return llm.resolve_spec(
        {"model": "custom", "custom_repo": "test/tiny", "custom_file": filename}
    )


def _runtime(tmp_path, env, filename="tiny.gguf"):
    """A runtime whose model file is already 'downloaded'."""
    models = tmp_path / "models"
    (models / "test__tiny").mkdir(parents=True, exist_ok=True)
    (models / "test__tiny" / filename).write_bytes(b"gguf")
    return llm.LlmRuntime(model_dir=models, worker_env=env)


def _load(runtime, filename="tiny.gguf", **kwargs):
    runtime.ensure(_spec(filename), **kwargs)
    return runtime.wait_ready(30)


def _ask(runtime, text, deadline=5.0):
    return runtime.generate(
        [{"role": "user", "content": text}],
        max_tokens=16,
        temperature=0.5,
        deadline_seconds=deadline,
    )


class TestModelProcess:
    """The model runs in a child process, so running out of memory costs the
    model, never the radio server."""

    def test_answers_through_the_child_process(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        assert _load(runtime) == "ready"
        pid = runtime.status()["worker_pid"]
        assert pid and pid != os.getpid()
        assert _ask(runtime, "hi") == "Hello"

    def test_child_volunteers_for_the_oom_killer(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        pid = runtime.status()["worker_pid"]
        assert Path(f"/proc/{pid}/oom_score_adj").read_text().strip() == "1000"

    def test_a_killed_model_is_reported_and_the_server_lives(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        with pytest.raises(llm.LlmWorkerDiedError, match="memory ran out"):
            _ask(runtime, "die")
        status = runtime.status()
        assert status["state"] == "error" and "memory ran out" in status["error"]
        assert status["worker_pid"] is None

    def test_settings_reach_the_child(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        assert _ask(runtime, "config") == f"{llm.CONTEXT_TOKENS} False"
        _load(runtime, repack=True)
        assert _ask(runtime, "config") == f"{llm.CONTEXT_TOKENS} True"

    def test_generation_stops_at_the_deadline(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        started = time.monotonic()
        answer = _ask(runtime, "slow", deadline=0.3)
        assert answer and set(answer) == {"a"}
        assert time.monotonic() - started < 2

    def test_prompt_over_the_context_window_is_its_own_error(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        with pytest.raises(llm.LlmPromptTooLongError):
            _ask(runtime, "too long")
        assert runtime.status()["state"] == "ready"

    def test_a_model_that_fails_to_load_is_reported(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama, filename="broken.gguf")
        assert _load(runtime, filename="broken.gguf") == "error"
        assert runtime.status()["error"] == "the model failed to load"
        assert "bad model file" in runtime.status()["error_detail"]

    def test_generate_refuses_while_busy(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        runtime._generate_lock.acquire()
        try:
            with pytest.raises(llm.LlmBusyError):
                _ask(runtime, "hi")
        finally:
            runtime._generate_lock.release()


class TestIdleUnload:
    """The model only holds memory while someone is talking to the bot."""

    def test_unloads_after_the_idle_timeout_and_reloads_on_demand(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime, idle_unload_seconds=300)
        pid = runtime.status()["worker_pid"]
        assert not runtime.unload_if_idle(now=time.monotonic() + 100)
        assert runtime.unload_if_idle(now=time.monotonic() + 301)
        status = runtime.status()
        assert status["state"] == "idle" and status["unloaded"]
        assert status["worker_pid"] is None
        assert not Path(f"/proc/{pid}").exists(), "the model process is gone"
        assert "unloaded to save memory" in runtime.describe()
        assert _load(runtime) == "ready"
        assert _ask(runtime, "hi") == "Hello"

    def test_zero_unloads_after_every_answer(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime, idle_unload_seconds=0)
        assert _ask(runtime, "hi") == "Hello"
        assert runtime.status()["state"] == "idle"
        assert runtime.status()["unloaded"]

    def test_never_unloads_mid_answer(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        runtime._generate_lock.acquire()
        try:
            assert not runtime.unload_if_idle(now=time.monotonic() + 10_000)
        finally:
            runtime._generate_lock.release()
        assert runtime.status()["state"] == "ready"


class TestRuntime:
    def test_missing_llama_cpp_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None)
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        assert runtime.ensure(llm.CATALOG[0]) == "downloading"
        assert runtime.wait_ready(5) == "error"
        assert "uv sync --extra llm" in runtime.status()["error"]
        # Rechecked on every question (cheap), so an install is picked up at once.
        assert runtime.ensure(llm.CATALOG[0]) == "downloading"
        runtime.wait_ready(5)

    def test_cached_file_skips_the_download(self, tmp_path):
        spec = llm.CATALOG[0]
        target = tmp_path / spec.repo.replace("/", "__") / spec.filename
        target.parent.mkdir(parents=True)
        target.write_bytes(b"gguf")
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        assert runtime._download(spec) == target

    def test_install_markers_from_run_sh_are_reported(self, tmp_path):
        """Docker compiles llama-cpp-python in the background (run.sh); the bot
        says so instead of claiming the package is simply missing."""
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        assert "MESHCORE_ENABLE_LLM" in runtime._missing_package_reason()
        (tmp_path / ".install-failed").touch()
        assert "failed" in runtime._missing_package_reason()
        (tmp_path / ".installing").touch()
        assert runtime._missing_package_reason().startswith("installing llama-cpp-python (")


class TestMemory:
    """A model that does not fit is refused with a reason, never loaded: the
    OOM killer would take the whole radio server with it."""

    def _model(self, tmp_path, mb):
        path = tmp_path / "m.gguf"
        with path.open("wb") as handle:
            handle.truncate(mb << 20)
        return path

    def test_refuses_when_memory_is_short(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm, "available_memory_mb", lambda: 250)
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        with pytest.raises(RuntimeError, match="not enough free memory.*needs about 292 MB"):
            runtime._check_memory(llm.CATALOG[0], self._model(tmp_path, 100))

    def test_allows_when_it_fits_or_memory_is_unknown(self, tmp_path, monkeypatch):
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        path = self._model(tmp_path, 100)
        monkeypatch.setattr(llm, "available_memory_mb", lambda: 300)
        runtime._check_memory(llm.CATALOG[0], path)
        monkeypatch.setattr(llm, "available_memory_mb", lambda: None)
        runtime._check_memory(llm.CATALOG[0], path)

    def test_reads_this_machine(self):
        available = llm.available_memory_mb()
        assert available is None or available > 0

    def test_the_smallest_model_comes_first(self):
        assert llm.CATALOG[0].download_mb == min(spec.download_mb for spec in llm.CATALOG)
        assert llm.CATALOG[0].ram_mb == min(spec.ram_mb for spec in llm.CATALOG)

    def test_repacking_counts_the_weights_twice(self, tmp_path, monkeypatch):
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        path = self._model(tmp_path, 100)
        monkeypatch.setattr(llm, "available_memory_mb", lambda: 300)
        runtime._check_memory(llm.CATALOG[0], path, repack=False)
        with pytest.raises(RuntimeError, match="needs about 392 MB"):
            runtime._check_memory(llm.CATALOG[0], path, repack=True)

    def test_changing_threads_or_repacking_reloads(self, tmp_path, monkeypatch):
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        monkeypatch.setattr(runtime, "_prepare", lambda *args: None)
        spec = llm.CATALOG[0]
        runtime._state, runtime._load_key = "ready", (spec, 0, False)
        assert runtime.ensure(spec) == "ready"
        assert runtime.ensure(spec, repack=True) == "downloading"
        runtime._state = "ready"
        assert runtime.ensure(spec, threads=2, repack=True) == "downloading"

    def test_repacking_switch_reaches_llama_cpp(self):
        low = pytest.importorskip("llama_cpp.llama_cpp")
        assert low.llama_model_default_params().use_extra_bufts is True
        with llm._weight_repacking(False):
            assert low.llama_model_default_params().use_extra_bufts is False
        assert low.llama_model_default_params().use_extra_bufts is True
        with llm._weight_repacking(True):
            assert low.llama_model_default_params().use_extra_bufts is True


class TestInstallStatus:
    """What the bot says while run.sh installs llama-cpp-python in Docker."""

    def _marker(self, tmp_path, **fields):
        marker = tmp_path / ".installing"
        marker.write_text("".join(f"{k}={v}\n" for k, v in fields.items()))
        return marker

    def test_phases_and_elapsed_time(self, tmp_path):
        now = time.time()
        me = os.getpid()
        installing = llm._describe_install(
            self._marker(tmp_path, phase="installing", started=now - 40, pid=me)
        )
        assert installing.startswith("installing llama-cpp-python (40 s so far")
        compiling = llm._describe_install(
            self._marker(tmp_path, phase="compiling", started=now - 720, pid=me, jobs=1)
        )
        assert compiling == "compiling llama.cpp (12 min so far, up to an hour on a Pi)"
        waiting = llm._describe_install(self._marker(tmp_path, phase="waiting", pid=me))
        assert "once the server is up" in waiting

    def test_a_dead_install_is_not_reported_as_running(self, tmp_path):
        import subprocess

        dead = subprocess.Popen(["true"])
        dead.wait()
        text = llm._describe_install(
            self._marker(tmp_path, phase="compiling", started=time.time(), pid=dead.pid)
        )
        assert "stopped before finishing" in text

    def test_an_old_empty_marker_still_reads(self, tmp_path):
        marker = tmp_path / ".installing"
        marker.touch()
        assert llm._describe_install(marker).startswith("installing llama-cpp-python (")

    def test_every_message_fits_one_mesh_message(self, tmp_path):
        name = "🤖 SmolLM2 135M (Q4, smallest) unavailable: "
        for phase in ("waiting", "installing", "compiling"):
            text = llm._describe_install(
                self._marker(tmp_path, phase=phase, started=time.time() - 5000, pid=os.getpid())
            )
            assert len((name + text).encode()) <= 140, text

    def test_a_missing_package_is_rechecked_on_every_question(self, tmp_path, monkeypatch):
        monkeypatch.setattr(llm.importlib.util, "find_spec", lambda name: None)
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        self._marker(tmp_path, phase="installing", started=time.time(), pid=os.getpid())
        runtime.ensure(llm.CATALOG[0])
        assert runtime.wait_ready(5) == "error"
        # No 60 s retry pause: the next question looks again straight away.
        assert runtime.ensure(llm.CATALOG[0]) == "downloading"
        runtime.wait_ready(5)


class TestPlainErrors:
    def test_a_missing_system_library_is_explained_plainly(self):
        raw = RuntimeError(
            "Failed to load shared library '/app/.venv/lib/python3.14/site-packages/llama_cpp/"
            "lib/libllama.so': libgomp.so.1: cannot open shared object file"
        )
        plain = llm._plain_reason(raw)
        assert "system library is missing" in plain
        assert "/app" not in plain and "libgomp" not in plain

    def test_our_own_messages_pass_through(self):
        assert llm._plain_reason(llm.LlmUserError("not enough free memory")) == (
            "not enough free memory"
        )

    def test_the_raw_error_is_kept_for_admins(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama, filename="broken.gguf")
        _load(runtime, filename="broken.gguf")
        assert runtime.describe().endswith("the model failed to load")
        assert "bad model file" in runtime.describe(detailed=True)


class _HistoryAwareRuntime(_FakeRuntime):
    """Answers with a counter; refuses prompts carrying history if asked to."""

    def __init__(self, refuse_history=False):
        super().__init__()
        self.refuse_history = refuse_history
        self.turn = 0

    def generate(self, messages, **kwargs):
        self.asked.append((messages, kwargs))
        if self.refuse_history and len(messages) > 2:
            raise llm.LlmPromptTooLongError("exceed context window")
        self.turn += 1
        return f"Answer {self.turn}."


class TestDmMemory:
    ALICE = "ab" * 32
    BOB = "cd" * 32

    async def _dm(self, monkeypatch, runtime, text, sender=ALICE, settings=None):
        return await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text=text, is_dm=True, sender_key=sender),
            settings=settings,
        )

    async def test_a_dm_remembers_the_conversation(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask my name is Ada")
        await self._dm(monkeypatch, runtime, "ask what is my name")
        sent = runtime.asked[1][0]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[1]["content"] == "my name is Ada"
        assert sent[2]["content"] == "Answer 1."
        assert sent[3]["content"] == "what is my name"

    async def test_each_sender_has_their_own_memory(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask I am Alice")
        await self._dm(monkeypatch, runtime, "ask who am I", sender=self.BOB)
        assert len(runtime.asked[1][0]) == 2  # system + Bob's question only

    async def test_channels_are_never_remembered(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        for _ in range(2):
            await _run(monkeypatch, runtime, BotTestRequest(text="ask hi", sender_key=self.ALICE))
        assert all(len(messages) == 2 for messages, _ in runtime.asked)

    async def test_the_setting_limits_and_disables_it(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        for n in range(4):
            await self._dm(monkeypatch, runtime, f"ask q{n}", settings={"history_messages": 2})
        assert [m["content"] for m in runtime.asked[3][0][1:]] == ["q2", "Answer 3.", "q3"]
        runtime = _HistoryAwareRuntime()
        for n in range(2):
            await self._dm(
                monkeypatch, runtime, f"ask q{n}", sender=self.BOB, settings={"history_messages": 0}
            )
        assert len(runtime.asked[1][0]) == 2

    async def test_too_long_with_history_retries_without_it(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask first")
        runtime.refuse_history = True
        replies = await self._dm(monkeypatch, runtime, "ask second")
        assert replies == ["Answer 2."]
        assert len(runtime.asked[-1][0]) == 2

    async def test_reset_forgets(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask remember this")
        assert await self._dm(monkeypatch, runtime, "ask reset") == ["🤖 Conversation forgotten."]
        assert await self._dm(monkeypatch, runtime, "ask forget") == ["🤖 Nothing to forget."]
        await self._dm(monkeypatch, runtime, "ask fresh start")
        assert len(runtime.asked[-1][0]) == 2


class TestDmSessions:
    def test_expires_after_an_idle_hour(self):
        sessions = llm.DmSessions()
        sessions.record("a", "q", "r", now=0)
        assert len(sessions.history("a", 10, now=llm.SESSION_IDLE_SECONDS - 1)) == 2
        assert sessions.history("a", 10, now=llm.SESSION_IDLE_SECONDS + 1) == []

    def test_history_is_capped_by_length_and_starts_with_the_sender(self):
        sessions = llm.DmSessions()
        for n in range(6):
            sessions.record("a", "q" * 300, f"answer {n}", now=0)
        history = sessions.history("a", 20, now=0)
        assert sum(len(m["content"]) for m in history) <= llm.HISTORY_MAX_CHARS
        assert history[0]["role"] == "user"
        assert history[-1]["content"] == "answer 5"

    def test_only_the_most_recent_senders_are_kept(self):
        sessions = llm.DmSessions()
        for n in range(llm.SESSION_MAX_SENDERS + 5):
            sessions.record(f"s{n}", "q", "r", now=0)
        assert sessions.history("s0", 10, now=0) == []
        assert sessions.history(f"s{llm.SESSION_MAX_SENDERS + 4}", 10, now=0)
