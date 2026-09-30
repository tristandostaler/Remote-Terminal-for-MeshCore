"""The ``tinyllm`` bot and its tiny-LLM runtime (``app/bots/llm.py``).

No test downloads or runs a real model: the runtime singleton is stubbed for
the bot tests, and the runtime tests feed it a fake llama object.
"""

import pytest

from app.bots import llm
from app.bots.engine import BotEngine
from app.bots.library import get_library_entry
from app.bots.runtime import load_bot_code
from app.models import BotTestRequest


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

    def ensure(self, spec, threads=0):
        return self.state

    def describe(self):
        return "Downloading Qwen2.5 0.5B: 42% of 468 MB"

    def generate(self, messages, **kwargs):
        self.asked.append((messages, kwargs))
        return self.answer


async def _run(monkeypatch, runtime, request, settings=None):
    from app.repository.bots import BotRepository

    monkeypatch.setattr(llm, "llm_runtime", runtime)
    entry = get_library_entry("tinyllm")
    name, suffix = "tinyllm-test", 2
    while await BotRepository.name_exists(name):
        name, suffix = f"tinyllm-test-{suffix}", suffix + 1
    bot = await BotRepository.create(name=name, code=entry["code"], settings=settings or {})
    response = await BotEngine().test_run(bot, request)
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

    async def test_system_prompt_asks_for_under_140_characters(self, test_db, monkeypatch):
        runtime = _FakeRuntime()
        await _run(monkeypatch, runtime, BotTestRequest(text="ask hi"))
        assert "under 140 characters" in runtime.asked[0][0][0]["content"]

    async def test_old_default_prompt_is_upgraded_but_a_custom_one_is_kept(
        self, test_db, monkeypatch
    ):
        """A version refresh never rewrites stored settings, so an install seeded
        with the old 200-character default must still get the new prompt."""
        old = (
            "You are a helpful assistant on a low-bandwidth mesh radio network. "
            "Answer in one or two short sentences, plain text, no markdown, "
            "under 200 characters."
        )
        runtime = _FakeRuntime()
        await _run(
            monkeypatch, runtime, BotTestRequest(text="ask hi"), settings={"system_prompt": old}
        )
        assert "under 140 characters" in runtime.asked[0][0][0]["content"]

        runtime = _FakeRuntime()
        await _run(
            monkeypatch,
            runtime,
            BotTestRequest(text="ask hi"),
            settings={"system_prompt": "Talk like a pirate."},
        )
        assert runtime.asked[0][0][0]["content"] == "Talk like a pirate."

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

    async def test_long_answer_is_split_not_truncated(self, test_db, monkeypatch):
        answer = " ".join(["word"] * 80)
        replies = await _run(
            monkeypatch, _FakeRuntime(answer=answer), BotTestRequest(text="ask x", is_dm=True)
        )
        assert len(replies) > 1
        assert all(len(r.encode()) <= 156 for r in replies)


class _FakeLlama:
    def __init__(self, pieces):
        self.pieces = pieces

    def create_chat_completion(self, **kwargs):
        assert kwargs["stream"] is True
        for piece in self.pieces:
            yield {"choices": [{"delta": {"content": piece}}]}


class TestRuntime:
    def _ready(self, tmp_path, pieces):
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        runtime._llm = _FakeLlama(pieces)
        runtime._spec = llm.CATALOG[0]
        runtime._state = "ready"
        return runtime

    def test_generate_joins_the_stream(self, tmp_path):
        runtime = self._ready(tmp_path, ["Hel", "lo", " mesh"])
        out = runtime.generate([], max_tokens=10, temperature=0.5, deadline_seconds=5)
        assert out == "Hello mesh"

    def test_generate_stops_at_the_deadline(self, tmp_path):
        runtime = self._ready(tmp_path, ["a"] * 1000)
        out = runtime.generate([], max_tokens=10, temperature=0.5, deadline_seconds=0)
        assert out == "a"

    def test_generate_refuses_while_busy(self, tmp_path):
        runtime = self._ready(tmp_path, ["a"])
        runtime._generate_lock.acquire()
        try:
            with pytest.raises(llm.LlmBusyError):
                runtime.generate([], max_tokens=10, temperature=0.5, deadline_seconds=5)
        finally:
            runtime._generate_lock.release()

    def test_missing_llama_cpp_is_reported(self, tmp_path, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def no_llama(name, *args, **kwargs):
            if name == "llama_cpp":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_llama)
        runtime = llm.LlmRuntime(model_dir=tmp_path)
        assert runtime.ensure(llm.CATALOG[0]) == "downloading"
        runtime._worker.join(timeout=5)
        status = runtime.status()
        assert status["state"] == "error"
        assert "uv sync --extra llm" in status["error"]
        # Not retried on every message.
        assert runtime.ensure(llm.CATALOG[0]) == "error"

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
        assert "still being installed" in runtime._missing_package_reason()
