"""The ``tinyllm`` bot and its tiny-LLM runtime (``app/bots/bots_utils/tinyllm/llm.py``).

No test downloads or runs a real model: the runtime singleton is stubbed for
the bot tests, and the runtime tests start the real model *process* with a fake
``llama_cpp`` package first on its path (see ``fake_llama``).
"""

import itertools
import os
import textwrap
import time
from pathlib import Path

import pytest

from app.bots.bots_utils.tinyllm import llm
from app.bots.engine import BotEngine
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code
from app.models import BotTestRequest


@pytest.fixture(autouse=True)
def _no_reference_notes(tmp_path, monkeypatch):
    """An empty docs folder unless a test brings its own, so the starter notes
    never leak into prompts other tests compare exactly."""
    from app.bots.bots_utils.tinyllm import llm_docs

    empty = llm_docs.DocsIndex(tmp_path / "no-docs")
    monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: empty)
    return empty


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

    def test_x86_only_models_are_hidden_off_x86(self):
        x86_only = {spec.key for spec in llm.CATALOG if spec.x86_only}
        assert x86_only == {"llama3.2-3b", "qwen2.5-3b", "phi4-mini"}
        on_x86 = {o["value"] for o in llm.model_options(x86=True)}
        on_arm = {o["value"] for o in llm.model_options(x86=False)}
        assert x86_only <= on_x86 and not (x86_only & on_arm)
        assert on_x86 - on_arm == x86_only and llm.CUSTOM_MODEL in on_arm
        # A bot already set to one keeps working on any server.
        assert llm.resolve_spec({"model": "phi4-mini"}).key == "phi4-mini"

    def test_is_x86(self):
        for arch in ("x86_64", "AMD64", "i686"):
            assert llm.is_x86(arch), arch
        for arch in ("aarch64", "armv7l", "arm64", "riscv64", ""):
            assert not llm.is_x86(arch), arch

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
        self.verdict = "yes"
        self.checked = None

    def choose(self, messages, choices, deadline_seconds, grace_seconds=None):
        self.checked = messages
        self.check_time = (deadline_seconds, grace_seconds)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict

    def ensure(self, spec, threads=0, repack=False, idle_unload_seconds=300, n_ctx=512):
        self.ensured = (spec, threads, repack)
        self.n_ctx = n_ctx
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
        assert await self._prompt(monkeypatch, {}) == _filled(
            llm.CATALOG_BY_KEY[llm.DEFAULT_MODEL].system_prompt
        )
        for spec in llm.CATALOG:
            prompt = await self._prompt(monkeypatch, {"model": spec.key})
            assert prompt == _filled(spec.system_prompt), spec.key

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
        assert empty == _filled(llm.CATALOG_BY_KEY[llm.DEFAULT_MODEL].system_prompt)

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


class TestModelDetails:
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

        def create_chat_completion(self, messages, grammar=None, **kwargs):
            text = messages[-1]["content"]
            if grammar is not None:
                # The grammar's first alternative, as a deterministic pick.
                pick = grammar.choices[0] if "pick-first" in text else grammar.choices[-1]
                return {"choices": [{"message": {"content": pick}}]}
            if text == "too long":
                raise ValueError("Requested tokens (900) exceed context window of 512")
            if text == "die":
                os.kill(os.getpid(), 9)  # what the OOM killer does
            if text == "hang":
                time.sleep(60)
            if text == "config":
                words = [str(self.kwargs["n_ctx"]), " ", str(self.repacking)]
            elif text == "slow":
                words = iter(lambda: time.sleep(0.05) or "a", None)
            else:
                words = ["Hel", "lo"]
            return ({"choices": [{"delta": {"content": w}}]} for w in words)

    class LlamaGrammar:
        @classmethod
        def from_string(cls, text, verbose=True):
            import json, re
            grammar = cls()
            grammar.choices = [json.loads(c) for c in re.findall(r'"[^"]*"', text)]
            return grammar
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
        runtime._state, runtime._load_key = "ready", (spec, 0, False, llm.CONTEXT_TOKENS)
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


ALICE = "ab" * 32
BOB = "cd" * 32


async def _store(sender, rows):
    """Store a DM conversation as the server would: (text, outgoing, age_s)."""
    from app.repository import MessageRepository

    now = int(time.time())
    for n, (text, outgoing, age) in enumerate(rows):
        await MessageRepository.create(
            msg_type="PRIV",
            text=text,
            received_at=now - age,
            conversation_key=sender,
            sender_timestamp=now - age + n,
            outgoing=outgoing,
            sender_key=None if outgoing else sender,
        )


class TestDmMemory:
    """DM memory is read back from the stored conversation."""

    async def _dm(self, monkeypatch, runtime, text, sender=ALICE, settings=None):
        # The triggering message is stored before the bot runs, as in the server.
        await _store(sender, [(text, False, 0)])
        return await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text=text, is_dm=True, sender_key=sender),
            settings=settings,
        )

    async def test_follow_ups_see_the_earlier_turns(self, test_db, monkeypatch):
        await _store(
            ALICE,
            [
                ("ask my name is Ada", False, 60),
                ("Nice to meet you, Ada.", True, 59),
                ("did you get my photo?", False, 40),  # ordinary chat, not to the bot
                ("yes!", True, 39),
            ],
        )
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask what is my name")
        sent = runtime.asked[0][0]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[1]["content"] == "my name is Ada"
        assert sent[2]["content"] == "Nice to meet you, Ada."
        assert sent[3]["content"] == "what is my name"

    async def test_each_conversation_is_separate_and_channels_have_none(self, test_db, monkeypatch):
        await _store(ALICE, [("ask I am Alice", False, 60), ("Hi Alice.", True, 59)])
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask who am I", sender=BOB)
        assert len(runtime.asked[0][0]) == 2
        await _run(monkeypatch, runtime, BotTestRequest(text="ask hi", sender_key=ALICE))
        assert len(runtime.asked[1][0]) == 2

    async def test_channels_and_rooms_never_get_past_messages(self, test_db, monkeypatch, tmp_path):
        """Not from stored DMs, not from a transcript, not even to steer the
        notes search: outside a DM the model sees only this question."""
        from app.bots.bots_utils.tinyllm import llm_docs

        (tmp_path / "radio.md").write_text(_NOTES)
        index = llm_docs.DocsIndex(tmp_path)
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: index)
        await _store(ALICE, [("ask tx power please", False, 60), ("Use set tx.", True, 59)])
        transcript = [
            {"text": "ask tx power please", "outgoing": False},
            {"text": "Use set tx.", "outgoing": True},
        ]
        for where in ({}, {"is_room": True}):
            runtime = _HistoryAwareRuntime()
            await _run(
                monkeypatch,
                runtime,
                BotTestRequest(
                    text="ask and how do I raise it",
                    sender_key=ALICE,
                    transcript=transcript,
                    **where,
                ),
            )
            sent = runtime.asked[0][0]
            assert [m["role"] for m in sent] == ["system", "user"], where
            assert "Use set tx" not in sent[0]["content"]
            assert "Transmit power" not in sent[0]["content"], (
                "the earlier question must not steer the notes search"
            )

    async def test_the_setting_limits_and_disables_it(self, test_db, monkeypatch):
        await _store(
            ALICE,
            [
                ("ask q1", False, 60),
                ("a1.", True, 59),
                ("ask q2", False, 50),
                ("a2.", True, 49),
            ],
        )
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask q3", settings={"history_messages": 2})
        assert [m["content"] for m in runtime.asked[0][0][1:]] == ["q2", "a2.", "q3"]
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask q4", settings={"history_messages": 0})
        assert len(runtime.asked[0][0]) == 2

    async def test_too_long_with_history_retries_without_it(self, test_db, monkeypatch):
        await _store(ALICE, [("ask first", False, 60), ("Answer 0.", True, 59)])
        runtime = _HistoryAwareRuntime(refuse_history=True)
        replies = await self._dm(monkeypatch, runtime, "ask second", sender=ALICE)
        assert replies == ["Answer 1."]
        assert len(runtime.asked[-1][0]) == 2

    async def test_reset_replies(self, test_db, monkeypatch):
        replies = await self._dm(monkeypatch, _HistoryAwareRuntime(), "ask reset")
        assert replies == ["🤖 Conversation forgotten; starting fresh."]


_ROW_IDS = itertools.count(1)


def _row(text, outgoing, at):
    from types import SimpleNamespace

    return SimpleNamespace(text=text, outgoing=outgoing, received_at=at, id=next(_ROW_IDS))


class TestConversationTurns:
    def _turns(self, rows, now=None):
        # Default "now": just after the last row, well inside the idle hour.
        now = max(r.received_at for r in rows) + 10 if now is None else now
        return _bot_namespace()["conversation_turns"](rows, now)

    def test_pairs_questions_with_the_answers_sent_after_them(self):
        rows = [
            _row("!ask hi", False, 100),
            _row("(1/2) Hello there,", True, 101),
            _row("(2/2) friend.", True, 102),
            _row("@[bot] llm how are you", False, 200),
            _row("\U0001f916 Busy answering someone else, try again shortly.", True, 201),
            _row("tinyllm and now?", False, 300),
            _row("Fine.", True, 301),
        ]
        assert self._turns(rows) == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello there, friend."},
            {"role": "user", "content": "and now?"},
            {"role": "assistant", "content": "Fine."},
        ]

    def test_ordinary_chat_is_ignored(self):
        rows = [
            _row("hey, coffee later?", False, 100),
            _row("sure", True, 101),
            _row("asking for a friend", False, 102),  # "ask" must be a whole word
        ]
        assert self._turns(rows) == []

    def test_reset_and_silence_start_a_new_conversation(self):
        rows = [
            _row("ask a", False, 100),
            _row("A.", True, 101),
            _row("ask reset", False, 200),
            _row("\U0001f916 Conversation forgotten; starting fresh.", True, 201),
            _row("ask b", False, 300),
            _row("B.", True, 301),
        ]
        assert [m["content"] for m in self._turns(rows)] == ["b", "B."]
        gap = [_row("ask a", False, 100), _row("A.", True, 101), _row("ask b", False, 5000)]
        assert self._turns(gap) == []
        assert self._turns([_row("ask a", False, 100), _row("A.", True, 101)], now=9000) == []

    def test_only_the_bots_prompt_reply_is_its_answer(self):
        rows = [
            _row("ask a", False, 100),
            _row("A.", True, 102),
            _row("lol, typed by hand later", True, 400),  # outside the answer window
            _row("ask b", False, 500),
            _row("did you see that?", False, 501),  # ordinary chat ends the answer
            _row("yes", True, 502),
        ]
        assert [m["content"] for m in self._turns(rows)] == ["a", "A."]

    def test_the_current_question_is_left_out(self):
        rows = [_row("ask a", False, 100), _row("A.", True, 101), _row("ask b", False, 102)]
        assert [m["content"] for m in self._turns(rows, now=103)] == ["a", "A."]


def _filled(prompt, sender="TestUser", max_chars=120):
    """A prompt as the bot sends it in tests: no radio, so "tinyllm"; the
    default 40 answer tokens ask for 120 characters."""
    return (
        prompt.replace("{radio_name}", "tinyllm")
        .replace("{sender}", sender)
        .replace("{max_chars}", str(max_chars))
    )


class TestPromptPlaceholders:
    def _fill(self, text, **values):
        spec = llm.CATALOG_BY_KEY[llm.DEFAULT_MODEL]
        settings = {"prompt_mode": "custom", "system_prompt": text}
        return _bot_namespace()["system_prompt_for"](settings, spec, values)

    def test_known_placeholders_are_filled(self):
        out = self._fill(
            "I am {radio_name}; hi {sender}, it is {time} on {date}.",
            radio_name="Hilltop",
            sender="Ada",
            time="12:30",
            date="2026-10-01",
        )
        assert out == "I am Hilltop; hi Ada, it is 12:30 on 2026-10-01."

    def test_other_braces_are_left_alone(self):
        assert (
            self._fill('Reply as JSON: {"a": 1} {unknown}') == 'Reply as JSON: {"a": 1} {unknown}'
        )

    def test_the_bigger_models_get_context_and_the_tiny_ones_do_not(self):
        small = [k for k, s in llm.CATALOG_BY_KEY.items() if s.system_prompt == llm.TINY_PROMPT]
        assert set(small) == {"smollm2-135m-q4", "smollm2-135m", "smollm2-360m", "lfm2-350m"}
        for key in ("qwen2.5-0.5b", "llama3.2-1b", "qwen2.5-1.5b", "gemma3-1b", "qwen2.5-3b"):
            prompt = llm.CATALOG_BY_KEY[key].system_prompt
            assert "{radio_name}" in prompt and "MeshCore" in prompt, key
        for key in ("smollm2-135m-q4", "smollm2-135m", "smollm2-360m", "gemma3-270m"):
            prompt = llm.CATALOG_BY_KEY[key].system_prompt
            assert "{" not in prompt and "MeshCore" not in prompt, key

    def test_max_chars_follows_the_answer_tokens(self):
        ns = _bot_namespace()
        assert ns["answer_chars"](40) == 120
        assert ns["answer_chars"](35) == 100
        assert ns["answer_chars"](16) == 40
        assert self._fill("under {max_chars} chars", max_chars=ns["answer_chars"](80)) == (
            "under 240 chars"
        )
        # A little under what the token cap allows (~4 chars a token).
        assert all(ns["answer_chars"](n) < n * 4 for n in range(16, 161))
        assert "{max_chars}" in llm.LARGE_PROMPT and "140" not in llm.LARGE_PROMPT

    async def test_the_prompt_states_the_configured_answer_length(self, test_db, monkeypatch):
        runtime = _FakeRuntime()
        await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask hi"),
            settings={"model": "qwen2.5-1.5b", "max_tokens": 25},
        )
        system = runtime.asked[0][0][0]["content"]
        assert "under 70 characters" in system and "{max_chars}" not in system

    async def test_the_radio_name_and_sender_reach_the_model(self, test_db, monkeypatch):
        from types import SimpleNamespace

        from app.radio import radio_manager

        monkeypatch.setattr(
            type(radio_manager),
            "meshcore",
            property(lambda self: SimpleNamespace(self_info={"name": "Hilltop"})),
        )
        runtime = _FakeRuntime()
        await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask hi", sender_name="Ada"),
            settings={"model": "qwen2.5-1.5b"},
        )
        system = runtime.asked[0][0][0]["content"]
        assert system.startswith("You are Hilltop,") and "talking with Ada" in system


_NOTES = """# Radio
## TX Power (get tx / set tx)
Transmit power in dBm. Higher reaches further but uses more battery.
## Spreading factor
Longer range, slower airtime.
# Fruit
## Bananas
Yellow.
"""


class TestReferenceNotes:
    @pytest.fixture
    def notes(self, tmp_path, monkeypatch):
        from app.bots.bots_utils.tinyllm import llm_docs

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "radio.md").write_text(_NOTES)
        index = llm_docs.DocsIndex(folder)
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: index)
        return folder

    async def test_matching_notes_go_into_the_system_prompt(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        await _run(monkeypatch, runtime, BotTestRequest(text="ask how do I change tx power"))
        system = runtime.asked[0][0][0]["content"]
        assert "Reference notes" in system and "Transmit power in dBm" in system
        assert "Yellow" not in system

    async def test_no_match_no_notes_and_they_can_be_turned_off(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        await _run(monkeypatch, runtime, BotTestRequest(text="ask tell me a joke"))
        assert "Reference notes" not in runtime.asked[0][0][0]["content"]
        runtime = _FakeRuntime()
        await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask tx power"),
            settings={"use_docs": False},
        )
        assert "Reference notes" not in runtime.asked[0][0][0]["content"]

    async def test_a_follow_up_searches_with_the_previous_question(
        self, test_db, monkeypatch, notes
    ):
        await _store(ALICE, [("ask what is tx power", False, 60), ("It is dBm.", True, 59)])
        runtime = _HistoryAwareRuntime()
        await TestDmMemory()._dm(monkeypatch, runtime, "ask and how do I raise it")
        assert "Transmit power in dBm" in runtime.asked[0][0][0]["content"]

    async def test_a_bigger_context_leaves_room_for_more(self, test_db, monkeypatch, notes):
        (notes / "big.md").write_text(
            "".join(f"## TX power note {n}\n{'tx power detail ' * 30}\n" for n in range(12))
        )
        sizes = {}
        for n_ctx in ("512", "2048"):
            runtime = _FakeRuntime()
            await _run(
                monkeypatch,
                runtime,
                BotTestRequest(text="ask tx power"),
                settings={"context_tokens": n_ctx, "notes_max_chars": 3000},
            )
            sizes[n_ctx] = len(runtime.asked[0][0][0]["content"])
            assert runtime.n_ctx == int(n_ctx)
        assert sizes["2048"] > sizes["512"] * 2

    async def test_notes_are_capped_so_the_model_has_less_to_read(
        self, test_db, monkeypatch, notes
    ):
        (notes / "big.md").write_text(
            "".join(f"## TX power note {n}\n{'tx power detail ' * 30}\n" for n in range(12))
        )
        sent = {}
        for cap in (None, 1500):
            runtime = _FakeRuntime()
            settings = {"context_tokens": "2048"}
            if cap:
                settings["notes_max_chars"] = cap
            await _run(monkeypatch, runtime, BotTestRequest(text="ask tx power"), settings=settings)
            system = runtime.asked[0][0][0]["content"]
            sent[cap] = len(system[system.index("Reference notes") :])
        assert sent[None] <= 700 + 80, "the default cap holds with a big context"
        assert sent[1500] > sent[None]

    async def test_overflow_drops_history_then_notes(self, test_db, monkeypatch, notes):
        class Picky(_FakeRuntime):
            def generate(self, messages, **kwargs):
                self.asked.append((messages, kwargs))
                if len(messages) > 2 or "Reference notes" in messages[0]["content"]:
                    raise llm.LlmPromptTooLongError("exceed context window")
                return "Plain."

        await _store(ALICE, [("ask tx power?", False, 60), ("dBm.", True, 59)])
        runtime = Picky()
        replies = await TestDmMemory()._dm(monkeypatch, runtime, "ask tx power again")
        assert replies == ["Plain."]
        shapes = [(len(m), "Reference notes" in m[0]["content"]) for m, _ in runtime.asked]
        assert shapes == [(4, True), (2, True), (2, False)]


class TestDocsIndex:
    def test_sections_split_at_headings_and_long_paragraphs(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        text = "<!-- hidden -->\n# A\n## B\n" + "\n\n".join(["word " * 100] * 3)
        sections = llm_docs.parse_markdown(text, "x.md")
        assert all(s.title == "A > B" for s in sections)
        assert len(sections) > 1 and all(
            len(s.text) <= llm_docs.SECTION_MAX_CHARS for s in sections
        )
        assert "hidden" not in " ".join(s.text for s in sections)

    def test_dotted_setting_names_match_either_way(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        (tmp_path / "a.md").write_text(
            "## Flood advert\nUse flood.advert.interval hours.\n## Other\nx"
        )
        index = llm_docs.DocsIndex(tmp_path)
        assert index.search("flood.advert.interval", 500)[0].title == "Flood advert"
        assert index.search("advert interval", 500)[0].title == "Flood advert"

    def test_edits_are_picked_up(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        doc = tmp_path / "a.md"
        doc.write_text("## One\napples\n")
        index = llm_docs.DocsIndex(tmp_path)
        assert index.search("pears", 500) == []
        doc.write_text("## One\npears and more pears\n")
        assert index.search("pears", 500)[0].title == "One"

    def test_shipped_notes_are_seeded(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        folder = tmp_path / "docs"
        llm_docs.seed_docs(folder)
        shipped = {p.name for p in llm_docs.SHIPPED_DOCS_DIR.glob("*.md")}
        assert {p.name for p in folder.glob("*.md")} == shipped

    def test_shipped_files_are_overwritten_and_added_files_kept(self, tmp_path, monkeypatch):
        from app.bots.bots_utils.tinyllm import llm_docs

        shipped_dir = tmp_path / "shipped"
        shipped_dir.mkdir()
        (shipped_dir / "old.md").write_text("# Old\nshipped\n")
        (shipped_dir / "gone.md").write_text("# Gone\nshipped\n")
        (shipped_dir / "deleted.md").write_text("# Deleted\nshipped\n")
        monkeypatch.setattr(llm_docs, "SHIPPED_DOCS_DIR", shipped_dir)
        folder = tmp_path / "docs"
        llm_docs.seed_docs(folder)
        (folder / "old.md").write_text("# Old\nmy edit\n")
        (folder / "deleted.md").unlink()
        (folder / "mine.md").write_text("# Mine\nmy notes\n")

        # The next release updates one file, drops one and adds one.
        (shipped_dir / "old.md").write_text("# Old\nshipped, updated\n")
        (shipped_dir / "gone.md").unlink()
        (shipped_dir / "new.md").write_text("# New\nshipped later\n")
        llm_docs.seed_docs(folder)
        assert (folder / "old.md").read_text() == "# Old\nshipped, updated\n"
        assert (folder / "deleted.md").exists(), "a deleted shipped file comes back"
        assert (folder / "new.md").exists()
        assert not (folder / "gone.md").exists(), "a file no longer shipped is removed"
        assert (folder / "mine.md").read_text() == "# Mine\nmy notes\n"
        assert (folder / llm_docs.SHIPPED_MANIFEST).read_text() == "deleted.md\nnew.md\nold.md\n"

    def test_an_unchanged_shipped_file_is_not_rewritten(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        folder = tmp_path / "docs"
        llm_docs.seed_docs(folder)
        seeded = folder / "meshcore.md"
        os.utime(seeded, (1, 1))
        llm_docs.seed_docs(folder)
        assert seeded.stat().st_mtime == 1, "a rewrite would rebuild the search index"

    def test_the_starter_notes_answer_common_questions(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(llm_docs.SHIPPED_DOCS_DIR)
        cases = {
            "how do I change the tx power of my repeater": "transmit power",
            "what is a hashtag channel": "Channels",
            "repeater clock is ahead": "clock",
            "command for the firmware version": "firmware version",
            "how do I add a region": "Add a region",
            "how to remove a region": "Remove (delete) a region",
            "how to allow floods": "Allow flooding",
            "block flooding for a region": "Block flooding",
            "set home region": "home region",
            "how do I save regions": "Save region changes",
            "reboot the repeater": "Reboot",
        }
        for question, expected in cases.items():
            assert expected in index.search(question, 700)[0].title, question

    def test_the_emergency_notes_answer_common_questions(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(llm_docs.SHIPPED_DOCS_DIR)
        cases = {
            "how do I do CPR": "CPR",
            "someone is choking": "Choking",
            "how to use a tourniquet": "tourniquets",
            "how to treat a burn": "Burns",
            "signs of a stroke": "Stroke",
            "how much bleach to purify water": "Purify water with bleach",
            "how long is food safe in the fridge without power": "Fridge and freezer",
            "can I run a generator in the garage": "Generator safety",
            "I smell gas": "Gas leak",
            "what to do during a tornado": "Tornado",
            "what channel is the marine emergency": "Marine VHF",
            "weather radio frequency": "Weather and emergency broadcast radio",
            "how to call mayday": "Mayday",
            "how to find north without a compass": "Find north",
            "how to send an emergency message on the mesh": "emergency message on the mesh",
        }
        for question, expected in cases.items():
            assert expected in index.search(question, 700)[0].title, question

    def test_the_reference_notes_answer_common_questions(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(llm_docs.SHIPPED_DOCS_DIR)
        cases = {
            # meshcore-hardware.md
            "what antenna should I use": "Choosing an antenna",
            "how high should the antenna be": "How high to put the antenna",
            "where should I put my repeater": "Where to put a repeater",
            "solar panel for repeater": "Solar power for a repeater",
            "can I charge lithium battery in the cold": "Battery types and safety",
            # emergency-psychological-first-aid.md
            "someone is panicking": "panicking",
            "my friend is suicidal": "suicidal",
            "I want to kill myself": "crisis lines",
            "I feel suicidal": "crisis lines",
            "how to help kids after a disaster": "children",
            "what to do with my pets in an evacuation": "Pets in an emergency",
            # radio-reference.md
            "morse code for S": "Morse code",
            "what does QTH mean": "Q-codes",
            "convert eastern time to UTC": "UTC and time zones",
            "what is my grid square": "Maidenhead",
            # practical-repairs.md
            "how to jump start a car": "Jump-starting",
            "how do I change a flat tire": "flat car tire",
            "my pipes are frozen": "Frozen pipes",
            "breaker keeps tripping": "Tripped breaker",
            # knots.md
            "how to tie a bowline": "bowline",
            "how to join two ropes": "sheet bend",
            "how to tighten a tarp line": "taut-line",
            # cooking-staples.md
            "how to cook rice": "white rice",
            "how to cook beans": "dried beans",
            "how to make bread without yeast": "soda bread",
            "what temperature is chicken safe": "Safe cooking temperatures",
            # remoteterm-*.md (converted READMEs)
            "how do I install remoteterm with docker": "Docker",
            "how to set up https": "HTTPS",
            "how to enable the virtual node": "Virtual Companion Node",
            # pi-linux-troubleshooting.md
            "how do I see the logs": "logs",
            "how to find the pi ip address": "IP address",
            "radio not detected": "Radio not detected",
            "disk is full": "Disk full",
            "how to kill a process": "kill a process",
            "how to back up the database": "Back up",
            # electronics-and-power.md
            "what is ohm's law": "Ohm",
            "how long will my battery last": "how long a battery lasts",
            "resistor color code": "Resistor colour code",
            "how to use a multimeter": "multimeter",
            # unit-conversions.md
            "how many km in a mile": "Length",
            "convert fahrenheit to celsius": "Temperature conversion",
            "mpg to l/100km": "Fuel economy",
            # weather-reading.md
            "is the barometer falling": "Barometer",
            "is a storm coming": "storm",
            "what do cirrus clouds mean": "Cloud types",
            # world-facts.md
            "capital of France": "France",
            "what is the capital of australia": "Australia",
            "currency of japan": "Japan",
            "phone code for germany": "Germany",
            "capital of quebec": "Quebec",
            "capital of texas": "Texas",
            "how to call internationally": "international phone number",
            # food-preservation.md
            "how to make sauerkraut": "sauerkraut",
            "is home canning safe": "canning",
            "how to store potatoes": "store potatoes",
            "how long does rice keep": "Shelf life",
            # gardening.md
            "when to plant tomatoes": "When to plant",
            "how to compost": "Composting",
            # bike-and-sewing.md
            "how do I fix a flat on my bike": "bicycle tire",
            "how to sew a button": "button",
            "how to fix a zipper": "zipper",
            # mesh-etiquette.md
            "can I use bots on public": "Which channel",
            "how often should I send adverts": "how often",
            # french-english.md
            # French questions reach the English guides through accents and
            # the starter synonyms (ajouter = add, redemarrer = reboot).
            "comment ajouter une région": "Add a region",
            "comment redémarrer le répéteur": "Reboot",
            "au secours": "phrases d'urgence",
            "how do you say help in french": "Everyday phrases",
        }
        for question, expected in cases.items():
            assert expected in index.search(question, 700)[0].title, question

    def test_small_talk_finds_no_notes(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(llm_docs.SHIPPED_DOCS_DIR)
        for chat in (
            "hello",
            "hi there",
            "thanks!",
            "good morning",
            "what is the power of friendship?",
            "tell me a joke",
            "write me a poem about the sea",
            "who won the hockey game",
            "bonjour",
            "merci beaucoup",
        ):
            assert index.search(chat, 700) == [], chat

    def test_a_best_match_too_big_for_the_budget_is_cut_to_fit(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        body = " ".join(f"Sentence {n} about zeppelins." for n in range(60))
        (tmp_path / "notes.md").write_text(f"## Zeppelins\n{body}\n")
        index = llm_docs.DocsIndex(tmp_path)
        assert all(len(s.text) <= llm_docs.SECTION_MAX_CHARS for s in index._sections), (
            "an oversized paragraph is split at sentences"
        )
        found = index.search("zeppelins", 200)
        assert len(found) == 1 and found[0].text.endswith("…")
        assert len(found[0].render()) < 200
        assert index.search("zeppelins", 60) == [], "too little room for a useful cut"

    def test_accents_fold_so_french_questions_match(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        (tmp_path / "notes.md").write_text("## Ajouter une région\nregion put <nom>\n")
        index = llm_docs.DocsIndex(tmp_path)
        assert index.search("comment ajouter une region", 500)
        assert index.search("ajouter une région", 500)

    def test_word_forms_meet(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        assert llm_docs._fold("flooding") == llm_docs._fold("floods") == llm_docs._fold("flooded")
        assert llm_docs._fold("regions") == llm_docs._fold("region")
        assert llm_docs._fold("frequencies") == llm_docs._fold("frequency")
        assert llm_docs._fold("tomatoes") == llm_docs._fold("tomato")
        assert llm_docs._fold("address") == "address"


class TestUpdateTinyllmDocs:
    """scripts/build/update_tinyllm_docs.py turns MeshCore's docs into notes."""

    def _convert(self, markdown):
        import importlib.util

        path = Path(__file__).resolve().parents[1] / "scripts" / "build" / "update_tinyllm_docs.py"
        spec = importlib.util.spec_from_file_location("update_tinyllm_docs", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.convert(markdown, "Title")

    def test_keeps_placeholders_and_drops_markup(self):
        out = self._convert(
            "# Doc\n## Navigation\n- [Regions](#regions)\n---\n"
            "### 3.1. **Add** a [region](#x)\n**Usage:** `region put <name> [parent]`<br>\n"
        )
        assert "Navigation" not in out and "---" not in out
        assert "### Add a region" in out
        assert "region put <name> [parent]" in out

    def test_code_lines_never_become_headings(self):
        out = self._convert("# Doc\n## Load\n```\n#Europe F\n```\n")
        from app.bots.bots_utils.tinyllm import llm_docs

        assert "    #Europe F" in out
        assert [s.title for s in llm_docs.parse_markdown(out)] == ["Title > Load"]


class TestPanelMemory:
    """Bots › Test stores no messages, so a test DM remembers the conversation
    from the transcript the panel sends with each run."""

    async def test_a_test_dm_uses_the_panel_transcript(self, test_db, monkeypatch):
        runtime = _HistoryAwareRuntime()
        request = BotTestRequest(
            text="ask what is my name",
            is_dm=True,
            transcript=[
                {"text": "ask my name is Ada", "outgoing": False},
                {"text": "Nice to meet you, Ada.", "outgoing": True},
                {"text": "\U0001f916 Busy answering someone else", "outgoing": True},
            ],
        )
        await _run(monkeypatch, runtime, request)
        sent = runtime.asked[0][0]
        assert [m["content"] for m in sent[1:]] == [
            "my name is Ada",
            "Nice to meet you, Ada.",
            "what is my name",
        ]

    async def test_reset_and_channels_in_the_panel(self, test_db, monkeypatch):
        after_reset = BotTestRequest(
            text="ask who am I",
            is_dm=True,
            transcript=[
                {"text": "ask I am Ada", "outgoing": False},
                {"text": "Hi Ada.", "outgoing": True},
                {"text": "ask reset", "outgoing": False},
                {"text": "\U0001f916 Conversation forgotten; starting fresh.", "outgoing": True},
            ],
        )
        runtime = _HistoryAwareRuntime()
        await _run(monkeypatch, runtime, after_reset)
        assert len(runtime.asked[0][0]) == 2
        channel = BotTestRequest(
            text="ask who am I",
            transcript=[{"text": "ask I am Ada"}, {"text": "Hi.", "outgoing": True}],
        )
        runtime = _HistoryAwareRuntime()
        await _run(monkeypatch, runtime, channel)
        assert len(runtime.asked[0][0]) == 2


class TestRelevanceGate:
    def _index(self, tmp_path, text):
        from app.bots.bots_utils.tinyllm import llm_docs

        (tmp_path / "notes.md").write_text(text)
        return llm_docs.DocsIndex(tmp_path)

    def test_one_shared_word_is_not_enough(self, tmp_path):
        index = self._index(tmp_path, _NOTES)
        assert index.search("what is the power of love", 800) == []
        assert index.search("tx power", 800)[0].title == "Radio > TX Power (get tx / set tx)"

    def test_one_word_questions_need_a_heading_or_a_rare_word(self, tmp_path):
        text = "".join(f"## Topic {n}\nThe common word is here.\n" for n in range(40))
        text += "## Apples\nRed fruit.\n## Misc\nA zeppelin is an airship.\n"
        index = self._index(tmp_path, text)
        assert index.search("apples", 800)[0].title == "Apples"
        assert index.search("zeppelin", 800)[0].title == "Misc"
        assert index.search("common", 800) == []

    def test_weak_matches_trail_off(self, tmp_path):
        text = (
            "## Solar panel wiring\nSolar panel wiring for a solar repeater site.\n"
            "## Garden\nA panel of wood and some wiring.\n"
        )
        titles = [s.title for s in self._index(tmp_path, text).search("solar panel wiring", 800)]
        assert titles == ["Solar panel wiring"]

    def test_notes_about_anything_work(self, tmp_path):
        """The notes are not MeshCore-only: any topic an operator documents."""
        index = self._index(
            tmp_path,
            "# Local trails\n## Mount Royal loop\nA 5 km loop, about 90 minutes, "
            "open dawn to dusk.\n## Lachine canal path\nFlat, 14 km, paved; bikes welcome.\n",
        )
        hit = index.search("how long is the mount royal loop", 800)
        assert hit and hit[0].title.endswith("Mount Royal loop")
        assert index.search("can I bike on the canal path", 800)[0].title.endswith("canal path")
        assert index.search("what is the capital of France", 800) == []


class TestModelCheck:
    @pytest.fixture
    def notes(self, tmp_path, monkeypatch):
        from app.bots.bots_utils.tinyllm import llm_docs

        folder = tmp_path / "docs"
        folder.mkdir()
        (folder / "radio.md").write_text(_NOTES)
        index = llm_docs.DocsIndex(folder)
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: index)

    async def _ask(self, monkeypatch, runtime, text="ask how do I change tx power", **settings):
        await _run(monkeypatch, runtime, BotTestRequest(text=text), settings=settings)
        return runtime.asked[0][0][0]["content"]

    async def test_off_by_default(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        runtime.verdict = "no"
        assert "Reference notes" in await self._ask(monkeypatch, runtime)
        assert runtime.checked is None

    async def test_no_drops_the_notes_and_yes_keeps_them(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        runtime.verdict = "no"
        system = await self._ask(monkeypatch, runtime, check_notes_with_model=True)
        assert "Reference notes" not in system
        runtime = _FakeRuntime()
        system = await self._ask(monkeypatch, runtime, check_notes_with_model=True)
        assert "Reference notes" in system

    async def test_the_check_gets_its_setting_within_the_runs_deadline(
        self, test_db, monkeypatch, notes
    ):
        # The test bot has the stock 10 s Time limit and a 3 s margin: 7 s for
        # the run, of which the answer keeps at least 3.
        runtime = _FakeRuntime()
        await self._ask(monkeypatch, runtime, check_notes_with_model=True, notes_check_seconds=2)
        assert runtime.check_time == (2, 2)
        runtime = _FakeRuntime()
        await self._ask(monkeypatch, runtime, check_notes_with_model=True, notes_check_seconds=30)
        deadline, grace = runtime.check_time
        assert 3.5 < deadline <= 10 - 3 - 3, "capped to leave the answer its time"

    async def test_no_time_left_skips_the_check_and_keeps_the_notes(
        self, test_db, monkeypatch, notes
    ):
        runtime = _FakeRuntime()
        runtime.verdict = "no"
        system = await self._ask(
            monkeypatch, runtime, check_notes_with_model=True, stop_before_limit_seconds=7
        )
        assert runtime.checked is None
        assert "Reference notes" in system

    async def test_the_check_is_generic_and_shows_the_headings(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        await self._ask(monkeypatch, runtime, check_notes_with_model=True)
        check = " ".join(m["content"] for m in runtime.checked)
        assert "TX Power" in check and "how do I change tx power" in check
        assert "MeshCore" not in check

    async def test_no_notes_no_check(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        await self._ask(
            monkeypatch, runtime, text="ask tell me a joke", check_notes_with_model=True
        )
        assert runtime.checked is None

    async def test_a_failed_check_keeps_the_notes(self, test_db, monkeypatch, notes):
        runtime = _FakeRuntime()
        runtime.verdict = llm.LlmBusyError("busy")
        assert "Reference notes" in await self._ask(
            monkeypatch, runtime, check_notes_with_model=True
        )

    def test_choose_through_the_model_process(self, tmp_path, fake_llama):
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime, idle_unload_seconds=0)
        ask = [{"role": "user", "content": "pick-first please"}]
        assert runtime.choose(ask, ("yes", "no"), 2) == "yes"
        assert runtime.choose([{"role": "user", "content": "x"}], ("yes", "no"), 2) == "no"
        # A check never unloads an unload-after-every-answer model: the answer follows.
        assert runtime.status()["state"] == "ready"
        runtime.generate(
            [{"role": "user", "content": "hi"}], max_tokens=4, temperature=0, deadline_seconds=2
        )
        assert runtime.status()["unloaded"]


class TestReviewFixes:
    def test_a_stuck_model_reloads_on_the_next_question(self, tmp_path, fake_llama):
        """A timeout is not a crash: no 60 s retry pause, just a reload."""
        runtime = _runtime(tmp_path, fake_llama)
        _load(runtime)
        with pytest.raises(llm.LlmWorkerTimeoutError):
            _ask(runtime, "hang", deadline=0.1)
        status = runtime.status()
        assert status["state"] == "idle" and status["unloaded"]
        assert _load(runtime) == "ready"
        assert _ask(runtime, "hi") == "Hello"

    async def test_history_keeps_its_room_when_no_notes_match(self, test_db, monkeypatch):
        long_turns = []
        for n in range(3):
            long_turns += [
                (f"ask question {n} " + "x" * 200, False, 60 - n * 5),
                (f"answer {n} " + "y" * 60, True, 59 - n * 5),
            ]
        await _store(ALICE, long_turns)
        runtime = _HistoryAwareRuntime()
        await TestDmMemory()._dm(monkeypatch, runtime, "ask and now")
        # All three earlier turns fit; before, half the room sat reserved for notes.
        assert len(runtime.asked[0][0]) == 1 + 6 + 1

    async def test_the_notes_folder_exists_after_the_first_run(
        self, test_db, monkeypatch, tmp_path
    ):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(tmp_path / "docs")
        seen = []
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: seen.append(1) or index)
        await _run(monkeypatch, _FakeRuntime(state="downloading"), BotTestRequest(text="ask"))
        assert seen, "a bare `ask` creates the notes folder"
        assert (tmp_path / "docs" / llm_docs.BOTS_PAGE).is_file()

    async def test_the_bots_page_lists_this_nodes_bots_but_not_private_ones(
        self, test_db, monkeypatch, tmp_path
    ):
        from app.bots.bots_utils.tinyllm import llm_docs
        from app.repository.bots import BotRepository

        code = (
            "from remoteterm import bot\n"
            "@bot.on_keyword('{kw}')\n"
            "async def h(ctx, msg):\n"
            "    await ctx.reply('ok')\n"
        )
        await BotRepository.create(
            name="weatherish",
            description="Weather for a place",
            code=code.format(kw="wx"),
            enabled=True,
        )
        await BotRepository.create(
            name="wardriving",
            description="Logs where packets were heard",
            code=code.format(kw="wardrive"),
            enabled=True,
            private=True,
        )
        index = llm_docs.DocsIndex(tmp_path / "docs")
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: index)
        from app.bots.engine import bot_engine

        await bot_engine.reload_all()
        try:
            await _run(monkeypatch, _FakeRuntime(state="downloading"), BotTestRequest(text="ask"))
        finally:
            bot_engine.bots.clear()
        page = (tmp_path / "docs" / llm_docs.BOTS_PAGE).read_text()
        assert "## weatherish bot: Weather for a place" in page
        assert "Commands: wx." in page
        assert "wardriv" not in page, "a private bot never reaches the notes"
        assert index.search("how do I get the weather", 700)[0].title.endswith(
            "weatherish bot: Weather for a place"
        )

    def test_the_bots_page_is_only_rewritten_when_it_changes(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        bots = [{"name": "ping", "category": "Basic", "description": "Pong", "keywords": ["ping"]}]
        llm_docs.write_bots_page(tmp_path, bots)
        page = tmp_path / llm_docs.BOTS_PAGE
        os.utime(page, (1, 1))
        llm_docs.write_bots_page(tmp_path, bots)
        assert page.stat().st_mtime == 1
        llm_docs.write_bots_page(tmp_path, [])
        assert "ping" not in page.read_text()

    async def test_engine_start_warms_the_notes_index(self, test_db, monkeypatch, tmp_path):
        import asyncio

        from app.bots.bots_utils.tinyllm import llm_docs
        from app.bots.engine import BotEngine
        from app.repository.bots import BotRepository

        built = []
        index = llm_docs.DocsIndex(tmp_path / "docs")
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: built.append(1) or index)
        entry = get_library_entry("tinyllm")
        await BotRepository.create(
            name="tinyllm-warm", code=entry["code"], enabled=True, builtin_key="tinyllm"
        )
        engine = BotEngine()
        await engine.start()
        try:
            for _ in range(50):
                if built:
                    break
                await asyncio.sleep(0.01)
        finally:
            await engine.stop()
        assert built, "the index is built at startup, not on the first question"

    def test_settings_are_grouped_and_ordered(self):
        schema = get_library_entry("tinyllm")["settings_schema"]
        sections = [f["label"] for f in schema if f["type"] == "section"]
        assert sections == [
            "Model",
            "Prompt",
            "Answers",
            "Memory & reference notes",
            "Performance & memory use",
        ]
        by_key = {f["key"]: f for f in schema}
        assert by_key["system_prompt"]["type"] == "textarea"
        assert by_key["check_notes_with_model"]["show_when"] == {"key": "use_docs", "value": "true"}
        assert schema[0]["type"] == "section"


class TestSynonyms:
    """synonyms.txt in the docs folder: the operator's own word groups."""

    def test_parsing_keeps_single_word_groups(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        path = tmp_path / "synonyms.txt"
        path.write_text(
            "# comment line\n"
            "reboot, restart  # trailing comment\n"
            "wardrive = wardriving\n"
            "power outage, blackout\n"  # a phrase is skipped, leaving one word
            "the, a\n"  # stopwords only
        )
        synonyms = llm_docs.load_synonyms(path)
        assert synonyms["reboot"] == {"restart"} and synonyms["restart"] == {"reboot"}
        assert synonyms["wardrive"] == {"wardriv"}
        assert "blackout" not in synonyms
        assert "the" not in synonyms
        assert llm_docs.load_synonyms(tmp_path / "missing.txt") == {}

    def test_a_synonym_finds_the_note_and_edits_apply_at_once(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        (tmp_path / "notes.md").write_text("## Wardriving logger\nLogs where packets were heard.\n")
        index = llm_docs.DocsIndex(tmp_path)
        assert index.search("wardrive", 500) == []
        (tmp_path / "synonyms.txt").write_text("wardrive, wardriving\n")
        assert index.search("wardrive", 500)[0].title == "Wardriving logger"

    def test_a_rare_synonym_does_not_outweigh_the_asked_word(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        (tmp_path / "en.md").write_text("## Add a region\nregion put adds a region.\n")
        (tmp_path / "fr.md").write_text(
            "## French words\najouter une region = add a region, ajouter ajouter.\n"
        )
        (tmp_path / "synonyms.txt").write_text("ajouter, add\n")
        index = llm_docs.DocsIndex(tmp_path)
        assert index.search("how do I add a region", 500)[0].title == "Add a region"

    def test_the_starter_list_is_created_once_and_never_overwritten(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        folder = tmp_path / "docs"
        llm_docs.seed_docs(folder)
        mine = folder / llm_docs.SYNONYMS_FILE
        assert "reboot, restart" in mine.read_text()
        mine.write_text("wardrive, wardriving\n")
        llm_docs.seed_docs(folder)
        assert mine.read_text() == "wardrive, wardriving\n"

    def test_the_starter_list_never_steers_toward_a_factory_reset(self):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(llm_docs.SHIPPED_DOCS_DIR)
        for question in ("how do I restart the repeater", "how do I delete a region"):
            titles = [s.title for s in index.search(question, 700)]
            assert titles and not any("Factory" in t for t in titles), question


class TestMissedQuestions:
    def test_counting_skips_small_talk_and_keeps_the_most_asked(self, monkeypatch):
        from app.bots.bots_utils.tinyllm import llm_docs

        missed = {}
        assert not llm_docs.record_missed(missed, "hello", 1)
        assert llm_docs.record_missed(missed, "Who runs the hill repeater?", 1)
        assert llm_docs.record_missed(missed, "who runs   the hill repeater", 2)
        assert missed == {"who runs the hill repeater": [2, 2]}

        monkeypatch.setattr(llm_docs, "MISSED_MAX", 3)
        for n in range(5):
            llm_docs.record_missed(missed, f"rare question {n}", 10 + n)
        assert len(missed) == 3
        assert "who runs the hill repeater" in missed, "the most asked survives"
        assert [q for q, _, _ in llm_docs.top_missed(missed)][0] == "who runs the hill repeater"

    def test_the_file_lists_them_most_asked_first_and_is_not_searched(self, tmp_path):
        from app.bots.bots_utils.tinyllm import llm_docs

        llm_docs.write_missed_file(tmp_path, {"b question": [1, 0], "a question": [3, 0]})
        lines = (tmp_path / llm_docs.MISSED_FILE).read_text().splitlines()
        body = [line for line in lines if line and not line.startswith("#")]
        assert body[0].startswith("3x  a question") and body[1].startswith("1x  b question")
        assert llm_docs.DocsIndex(tmp_path).search("question", 500) == []

    async def test_a_question_without_notes_is_recorded(self, tmp_path, monkeypatch):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(tmp_path)
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: index)
        ns = load_bot_code(get_library_entry("tinyllm")["code"]).namespace

        class Ctx:
            state = {}

        await ns["note_missed"](Ctx, "who runs the hill repeater")
        assert Ctx.state[ns["MISSED_STATE"]]["who runs the hill repeater"][0] == 1
        assert "who runs the hill repeater" in (tmp_path / llm_docs.MISSED_FILE).read_text()

    async def test_admins_list_and_clear_them_by_dm(self, test_db, monkeypatch):
        admin = "ef" * 32
        runtime = _FakeRuntime()
        replies = await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask missed", is_dm=True, sender_key=admin),
            admin_key=admin,
        )
        assert replies == ["🤖 No missed questions yet: every question found notes."]
        assert runtime.asked == [], "the command never reaches the model"

        stranger = await _run(
            monkeypatch,
            _FakeRuntime(),
            BotTestRequest(text="ask missed", is_dm=True, sender_key="12" * 32),
            admin_key=admin,
        )
        assert stranger == ["Paris is the capital of France."], "anyone else gets an answer"

    async def test_the_list_shows_the_most_asked_and_clears(self, tmp_path, monkeypatch):
        from app.bots.bots_utils.tinyllm import llm_docs

        index = llm_docs.DocsIndex(tmp_path)
        monkeypatch.setattr(llm_docs, "docs_index", lambda folder=None: index)
        ns = load_bot_code(get_library_entry("tinyllm")["code"]).namespace
        sent = []

        class Ctx:
            state = {ns["MISSED_STATE"]: {"a question": [3, 5], "b question": [1, 9]}}

            @staticmethod
            async def reply(text):
                sent.append(text)

            reply_split = reply

        await ns["show_missed"](Ctx)
        assert "3x a question\n1x b question" in sent[-1]
        await ns["show_missed"](Ctx, clear=True)
        assert Ctx.state[ns["MISSED_STATE"]] == {}
        assert sent[-1] == "🤖 Missed questions cleared."


class TestTimeLimits:
    async def _ask(self, monkeypatch, timeout, settings=None):
        from app.repository.bots import BotRepository

        runtime = _FakeRuntime()
        monkeypatch.setattr(llm, "llm_runtime", runtime)
        entry = get_library_entry("tinyllm")
        bot = await BotRepository.create(
            name=f"tinyllm-limit-{timeout}-{len(settings or {})}",
            code=entry["code"],
            timeout_seconds=timeout,
            settings=settings or {},
        )
        response = await BotEngine().test_run(bot, BotTestRequest(text="ask hi"))
        assert response.error is None, response.error
        return runtime.asked[0][1], response

    async def test_the_answer_ends_the_margin_before_the_time_limit(self, test_db, monkeypatch):
        kwargs, response = await self._ask(monkeypatch, 30, {"stop_before_limit_seconds": 2})
        # At most 28 s from the start of the run, minus what it already used.
        assert 27 < kwargs["deadline_seconds"] <= 28
        # A hung model process is stopped inside the margin, after sending room.
        assert kwargs["grace_seconds"] == 1
        assert kwargs["deadline_seconds"] + kwargs["grace_seconds"] < 30
        assert any("2.0 s before the 30 s Time limit" in line for line in response.logs)

    async def test_the_default_margin_follows_the_time_limit(self, test_db, monkeypatch):
        kwargs, _ = await self._ask(monkeypatch, 60)
        assert 56 < kwargs["deadline_seconds"] <= 60 - 3

    def test_the_deadline_covers_reading_the_prompt(self):
        """Timed from the request: a slow prompt read counts against it."""

        class SlowReader:
            def create_chat_completion(self, **kwargs):
                time.sleep(0.2)  # reading the prompt
                for word in ["one ", "two ", "three "]:
                    yield {"choices": [{"delta": {"content": word}}]}

        base = {"messages": [], "max_tokens": 10, "temperature": 0}
        assert llm._stream_answer(SlowReader(), {**base, "deadline": 5}) == "one two three "
        assert llm._stream_answer(SlowReader(), {**base, "deadline": 0.1}) == "one "


class TestKeywordless:
    """Contacts listed in ``keywordless_contacts`` DM questions without ``ask``.

    The engine's ``on_unmatched`` fallback picks the run (tests/test_bots_unmatched.py);
    these pin the bot's side: the answer, and DM memory reading old ``ask ...``
    history and new keyword-free messages the same way.
    """

    LISTED = {"keywordless_contacts": [ALICE]}

    def _turns(self, rows, keywordless):
        now = max(r.received_at for r in rows) + 10
        return _bot_namespace()["conversation_turns"](rows, now, keywordless)

    async def _dm(self, monkeypatch, runtime, text, sender=ALICE, settings=LISTED):
        await _store(sender, [(text, False, 0)])
        return await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text=text, is_dm=True, sender_key=sender),
            settings=settings,
        )

    def test_old_ask_history_and_plain_messages_read_the_same(self):
        rows = [
            _row("ask my name is Ada", False, 100),
            _row("Nice to meet you, Ada.", True, 101),
            _row("I live in Lyon", False, 200),
            _row("Lyon is lovely.", True, 201),
        ]
        assert self._turns(rows, keywordless=True) == [
            {"role": "user", "content": "my name is Ada"},
            {"role": "assistant", "content": "Nice to meet you, Ada."},
            {"role": "user", "content": "I live in Lyon"},
            {"role": "assistant", "content": "Lyon is lovely."},
        ]

    def test_without_the_option_plain_chat_is_still_ignored(self):
        rows = [
            _row("ask my name is Ada", False, 100),
            _row("Nice to meet you, Ada.", True, 101),
            _row("I live in Lyon", False, 200),
            _row("Lyon is lovely.", True, 201),
        ]
        assert [m["content"] for m in self._turns(rows, keywordless=False)] == [
            "my name is Ada",
            "Nice to meet you, Ada.",
        ]

    def test_bare_reset_clears_and_other_bots_are_remembered(self):
        rows = [
            _row("I am Ada", False, 100),
            _row("Hi Ada.", True, 101),
            _row("reset", False, 150),
            _row("\U0001f916 Conversation forgotten; starting fresh.", True, 151),
            _row("hello", False, 200),
            _row("Hello there!", True, 201),  # the hello bot's answer
        ]
        assert self._turns(rows, keywordless=True) == [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "Hello there!"},
        ]

    async def test_answers_a_plain_dm_with_the_earlier_ask_turns(self, test_db, monkeypatch):
        await _store(
            ALICE, [("ask my name is Ada", False, 60), ("Nice to meet you, Ada.", True, 59)]
        )
        runtime = _HistoryAwareRuntime()
        replies = await self._dm(monkeypatch, runtime, "what is my name")
        assert replies == ["Answer 1."]
        assert [m["content"] for m in runtime.asked[0][0][1:]] == [
            "my name is Ada",
            "Nice to meet you, Ada.",
            "what is my name",
        ]

    async def test_ask_still_works_for_a_listed_contact(self, test_db, monkeypatch):
        await _store(ALICE, [("I am Ada", False, 60), ("Hi Ada.", True, 59)])
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask who am I")
        assert [m["content"] for m in runtime.asked[0][0][1:]] == [
            "I am Ada",
            "Hi Ada.",
            "who am I",
        ]

    async def test_bare_reset_replies(self, test_db, monkeypatch):
        replies = await self._dm(monkeypatch, _HistoryAwareRuntime(), "reset")
        assert replies == ["🤖 Conversation forgotten; starting fresh."]

    async def test_unlisted_contacts_keep_the_old_memory(self, test_db, monkeypatch):
        await _store(BOB, [("I am Bob", False, 60), ("Hi Bob.", True, 59)])
        runtime = _HistoryAwareRuntime()
        await self._dm(monkeypatch, runtime, "ask who am I", sender=BOB)
        assert len(runtime.asked[0][0]) == 2

    async def test_a_plain_dm_from_an_unlisted_contact_is_not_answered(self, test_db):
        from app.repository.bots import BotRepository

        entry = get_library_entry("tinyllm")
        bot = await BotRepository.create(
            name="tinyllm-unlisted", code=entry["code"], settings=self.LISTED
        )
        response = await BotEngine().test_run(
            bot, BotTestRequest(text="what is LoRa", is_dm=True, sender_key=BOB)
        )
        assert response.matched is False
