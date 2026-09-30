"""Tiny on-device language models for the ``tinyllm`` bot.

The built-in ``tinyllm`` bot (``library/code/tinyllm.py``) answers questions with a
small GGUF model run by ``llama-cpp-python`` — no Ollama, no server, no GPU.
This module owns what must outlive a single bot run:

* **The catalog** (:data:`CATALOG`): every model the bot's Settings dropdown
  offers, with its download size, memory footprint, speed and quality notes.
  The dropdown labels and the per-option descriptions are generated from it
  (:func:`model_options`), so the numbers the operator chooses by and the file
  that gets downloaded can never disagree.
* **The loaded model** (:data:`llm_runtime`): a process-wide singleton. Bot code
  is re-exec'd whenever the operator edits or reconfigures it, so a model held in
  the bot's own namespace would be reloaded on every settings save.

The model itself runs in a **child process** (``python -m app.bots.llm``, see
:func:`_worker_main`), never in the server: the child raises its own
``oom_score_adj`` to the maximum, so when memory runs out the kernel kills the
model and the radio server keeps running. The server never imports llama.cpp.

It is also **unloaded when idle** (the child exits) after a configurable number
of minutes, so the memory is only taken while someone is actually talking to
the bot; the next question reloads it, which is quick once the file is in the
page cache.

Every bot run is killed after ``BOT_EXECUTION_TIMEOUT`` (10 s), while a first
download is hundreds of MB. So preparation (download, then load) always runs in
a background thread and a run only *starts* it and reports progress; generation
streams tokens and stops at a deadline the caller keeps inside the timeout.

``llama-cpp-python`` is the optional ``llm`` extra (``uv sync --extra llm``).
Only the child process imports it, so the app runs without it.
"""

from __future__ import annotations

import contextlib
import importlib
import importlib.util
import json
import logging
import os
import select
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK_BYTES = 1 << 20
# Headroom kept free after a download, so a model never fills the disk the
# SQLite database lives on (the same concern as the AEIC bundle on an SD card).
DISK_HEADROOM_BYTES = 512 * 1024 * 1024
# Memory settings. Measured on a model with SmolLM2 135M Q8_0's exact shape:
# ~200 MB in total, of which only ~45 MB cannot be reclaimed.
#
# Context window: a mesh prompt (system prompt + a question clipped to 500
# characters) plus a 160-token answer fits in 512 tokens; the KV cache scales
# with it, and 1024 cost 16 MB more for nothing.
CONTEXT_TOKENS = 512
# Prompt batch size: the compute buffer scales with it. Mesh prompts are short,
# so small batches cost no speed worth noticing.
BATCH_TOKENS = 64
# The weights are memory-mapped (llama.cpp's default, kept explicit): they sit
# in the page cache, which the kernel can drop and re-read under pressure,
# instead of in process memory. Reading them in instead turned 156 MB of
# reclaimable cache into 198 MB of memory nothing can take back.
USE_MMAP = True
# Free memory a load must leave behind, on top of the model file and its own
# overhead, so the radio server and the OS never get squeezed. A load that does
# not fit is refused with a reply rather than risking the OOM killer (which
# takes the whole server with it) or a Pi thrashing its SD card.
LOAD_OVERHEAD_MB = 64
MEMORY_RESERVE_MB = 128


@contextlib.contextmanager
def _weight_repacking(enabled: bool) -> Iterator[None]:
    """Let llama.cpp repack weights at load time, or keep them as the file has them.

    On ARM CPUs with dot-product instructions (a Pi 5; not a Pi 4), llama.cpp
    rewrites Q8_0, Q4_0 and Q4_K weights into an interleaved layout that
    multiplies faster -- in a second, anonymous copy, while the memory-mapped
    original stays resident too. That doubles the unreclaimable footprint of
    the very models meant for small boards (x86 does the same for Q4_0: a
    stand-in SmolLM2 135M Q4_0 measured 102 MB unreclaimable with repacking,
    45 MB without). Off by default; the bot's "faster ARM layout" setting
    turns it back on.

    ``Llama()`` takes no argument for it, but builds its model params from
    ``llama_model_default_params()`` at load, so that one call is wrapped for
    the duration of the load. Loads are serialized under the generate lock.
    """
    import llama_cpp.llama_cpp as low  # type: ignore[import-not-found]

    original = low.llama_model_default_params
    fields = {name for name, *_ in getattr(low.llama_model_params, "_fields_", ())}
    if enabled or "use_extra_bufts" not in fields:
        yield
        return

    def without_repacking() -> Any:
        params = original()
        params.use_extra_bufts = False
        return params

    low.llama_model_default_params = without_repacking
    try:
        yield
    finally:
        low.llama_model_default_params = original


# A failed download/load is retried on the next question after this long.
RETRY_AFTER_SECONDS = 60

INSTALL_HINT = (
    "llama-cpp-python is not installed on this server: set MESHCORE_ENABLE_LLM=true "
    "(Docker / Home Assistant) or run `uv sync --extra llm`"
)


@dataclass(frozen=True)
class ModelSpec:
    """One downloadable GGUF model and what it costs to run."""

    key: str
    name: str
    repo: str
    filename: str
    params: str
    quant: str
    download_mb: int
    ram_mb: int
    speed: str
    quality: str
    notes: str
    # The prompt the bot uses with this model unless the operator picks
    # "Custom". Sized to what the model can follow: the smallest ones repeat
    # whatever the prompt says about them, so theirs says almost nothing.
    system_prompt: str

    @property
    def url(self) -> str:
        return f"https://huggingface.co/{self.repo}/resolve/main/{self.filename}?download=true"

    @property
    def option_label(self) -> str:
        return (
            f"{self.name} — {_fmt_mb(self.download_mb)} download, "
            f"~{_fmt_mb(self.ram_mb)} RAM, {self.quality.split(':')[0].lower()}"
        )

    @property
    def option_description(self) -> str:
        return (
            f"{self.params} parameters, {self.quant}. Download {_fmt_mb(self.download_mb)} "
            f"(once, to the server's model folder); about {_fmt_mb(self.ram_mb)} of RAM while "
            f"loaded. Speed: {self.speed}. Quality: {self.quality}. {self.notes} "
            f"Default prompt: \u201c{self.system_prompt}\u201d. "
            f"Source: huggingface.co/{self.repo}"
        )


def _fmt_mb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1000 else f"{mb} MB"


# Sizes are the published GGUF file sizes; RAM is file + KV cache for
# CONTEXT_TOKENS + runtime overhead, rounded up (the two SmolLM2 135M rows are
# measured). Speeds are rough CPU figures for a Raspberry Pi 5 / an x86
# mini-PC; a Pi 4 is about half.
# Default system prompts, per model size. A 135-360M model cannot follow
# instructions so much as continue text: told it is "an assistant on a
# low-bandwidth mesh radio network", it answers "hello" by describing itself as
# one (seen on a real node). So the tiny ones get one plain instruction, and
# nothing about their setting; a character limit is meaningless to them, and
# answer length is capped in code anyway. Larger models can use a little more.
TINY_PROMPT = "You are a friendly chatbot. Reply with one short sentence."
GEMMA_PROMPT = "Reply with one short, friendly sentence."
SMALL_PROMPT = (
    "You are a helpful assistant in a radio chat. "
    "Answer in one or two short sentences of plain text."
)
LARGE_PROMPT = (
    "You are a helpful assistant in a radio chat. Answer in one or two short "
    "sentences of plain text, under 140 characters."
)
CUSTOM_MODEL_PROMPT = "You are a friendly chatbot. Reply with one or two short sentences."

CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="smollm2-135m-q4",
        name="SmolLM2 135M (Q4, smallest)",
        repo="bartowski/SmolLM2-135M-Instruct-GGUF",
        filename="SmolLM2-135M-Instruct-Q4_K_M.gguf",
        params="135M",
        quant="Q4_K_M",
        download_mb=105,
        ram_mb=180,
        speed="very fast (~45 tok/s Pi 5, 100+ tok/s x86)",
        quality="Toy: grammatical but often wrong or off-topic",
        notes=(
            "The lightest option, for a Pi with 1 GB or less: the Q8 build's model, "
            "compressed harder, so answers are slightly rougher."
        ),
        system_prompt=TINY_PROMPT,
    ),
    ModelSpec(
        key="smollm2-135m",
        name="SmolLM2 135M",
        repo="HuggingFaceTB/SmolLM2-135M-Instruct-GGUF",
        filename="smollm2-135m-instruct-q8_0.gguf",
        params="135M",
        quant="Q8_0",
        download_mb=145,
        ram_mb=210,
        speed="very fast (~40 tok/s Pi 5, 100+ tok/s x86)",
        quality="Toy: grammatical but often wrong or off-topic",
        notes="Smallest option; fine for playful one-liners, not for facts.",
        system_prompt=TINY_PROMPT,
    ),
    ModelSpec(
        key="gemma3-270m",
        name="Gemma 3 270M",
        repo="unsloth/gemma-3-270m-it-GGUF",
        filename="gemma-3-270m-it-Q8_0.gguf",
        params="270M",
        quant="Q8_0",
        download_mb=292,
        ram_mb=450,
        speed="fast (~25 tok/s Pi 5, 70+ tok/s x86)",
        quality="Basic: short friendly chat, weak on facts",
        notes="Google's tiny model; follows a system prompt surprisingly well for its size.",
        system_prompt=GEMMA_PROMPT,
    ),
    ModelSpec(
        key="smollm2-360m",
        name="SmolLM2 360M",
        repo="HuggingFaceTB/SmolLM2-360M-Instruct-GGUF",
        filename="smollm2-360m-instruct-q8_0.gguf",
        params="360M",
        quant="Q8_0",
        download_mb=386,
        ram_mb=550,
        speed="fast (~20 tok/s Pi 5, 60 tok/s x86)",
        quality="Basic: decent small talk",
        notes="English only.",
        system_prompt=TINY_PROMPT,
    ),
    ModelSpec(
        key="qwen2.5-0.5b",
        name="Qwen2.5 0.5B",
        repo="Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        filename="qwen2.5-0.5b-instruct-q4_k_m.gguf",
        params="494M",
        quant="Q4_K_M",
        download_mb=491,
        ram_mb=650,
        speed="good (~15 tok/s Pi 5, 50 tok/s x86)",
        quality="Recommended: best of the tiny tier, simple facts and instructions",
        notes="Multilingual. The default: the best answers that still fit a Pi 4.",
        system_prompt=SMALL_PROMPT,
    ),
    ModelSpec(
        key="llama3.2-1b",
        name="Llama 3.2 1B",
        repo="bartowski/Llama-3.2-1B-Instruct-GGUF",
        filename="Llama-3.2-1B-Instruct-Q4_K_M.gguf",
        params="1.2B",
        quant="Q4_K_M",
        download_mb=808,
        ram_mb=1100,
        speed="moderate (~8 tok/s Pi 5, 30 tok/s x86)",
        quality="Good: noticeably smarter, better general knowledge",
        notes="Needs a Pi 5 or better to answer inside the 10 s bot limit.",
        system_prompt=SMALL_PROMPT,
    ),
    ModelSpec(
        key="qwen2.5-1.5b",
        name="Qwen2.5 1.5B",
        repo="Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        filename="qwen2.5-1.5b-instruct-q4_k_m.gguf",
        params="1.5B",
        quant="Q4_K_M",
        download_mb=1120,
        ram_mb=1500,
        speed="slow on a Pi (~5 tok/s Pi 5, 20 tok/s x86)",
        quality="Best: the most capable option here",
        notes="Best on an x86 host; on a Pi replies are cut short by the time limit.",
        system_prompt=LARGE_PROMPT,
    ),
)

CATALOG_BY_KEY = {spec.key: spec for spec in CATALOG}
DEFAULT_MODEL = "qwen2.5-0.5b"
CUSTOM_MODEL = "custom"


def model_options() -> list[dict[str, str]]:
    """Settings dropdown options: every catalog model plus "custom"."""
    options = [
        {"value": spec.key, "label": spec.option_label, "description": spec.option_description}
        for spec in CATALOG
    ]
    options.append(
        {
            "value": CUSTOM_MODEL,
            "label": "Custom GGUF from Hugging Face",
            "description": (
                "Any GGUF chat model: set the repository and file name below. Pick one under "
                "~1 GB — the whole file is loaded into RAM, and larger models cannot answer "
                "inside the 10 s bot time limit on a Pi."
            ),
        }
    )
    return options


def resolve_spec(settings: dict[str, Any]) -> ModelSpec:
    """The model the bot's settings select. Raises ``ValueError`` when unusable."""
    key = str(settings.get("model") or DEFAULT_MODEL).strip()
    if key != CUSTOM_MODEL:
        spec = CATALOG_BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"unknown model {key!r}; pick one in the bot's Settings")
        return spec
    repo = str(settings.get("custom_repo") or "").strip().strip("/")
    filename = str(settings.get("custom_file") or "").strip()
    if repo.count("/") != 1 or not filename.lower().endswith(".gguf") or "/" in filename:
        raise ValueError("custom model needs a repository (owner/name) and a .gguf file name")
    return ModelSpec(
        key=f"custom:{repo}/{filename}",
        name=filename.removesuffix(".gguf"),
        repo=repo,
        filename=filename,
        params="?",
        quant="?",
        download_mb=0,
        ram_mb=0,
        speed="?",
        quality="?",
        notes="",
        system_prompt=CUSTOM_MODEL_PROMPT,
    )


def _plain_reason(exc: BaseException) -> str:
    """A short, non-technical reason for an unexpected failure, for anyone.

    The raw exception (paths, library names) stays in the server log and is
    shown only to admins who ask in a DM.
    """
    if isinstance(exc, (LlmUserError, LlmWorkerDiedError)):
        return str(exc)
    text = str(exc)
    if "shared object" in text or "Failed to load shared library" in text:
        return (
            "llama.cpp can't start on this server (a system library is missing); restart to repair"
        )
    if type(exc).__module__.startswith("httpx"):
        return "the model could not be downloaded (network error)"
    return "the model failed to load"


def _describe_install(marker: Path) -> str:
    """What the background install (run.sh) is doing, from its marker file.

    Short on purpose: it rides in one mesh message behind the model's name.
    """
    fields: dict[str, str] = {}
    with contextlib.suppress(OSError):
        for line in marker.read_text().splitlines():
            key, _, value = line.partition("=")
            fields[key.strip()] = value.strip()
    pid = fields.get("pid", "")
    # A job that died (container stopped mid-install, killed) leaves its marker
    # behind; without this check the bot would say "installing" forever.
    if pid.isdigit() and Path("/proc").is_dir() and not Path(f"/proc/{pid}").exists():
        return "the llama-cpp-python install stopped before finishing; restart to retry"
    try:
        started = float(fields["started"])
    except (KeyError, ValueError):
        with contextlib.suppress(OSError):
            started = marker.stat().st_mtime
    elapsed = max(0.0, time.time() - started) if "started" in locals() else 0.0
    so_far = f"{int(elapsed)} s" if elapsed < 90 else f"{int(elapsed // 60)} min"
    phase = fields.get("phase")
    if phase == "compiling":
        return f"compiling llama.cpp ({so_far} so far, up to an hour on a Pi)"
    if phase == "waiting":
        return "llama-cpp-python installs once the server is up; try again shortly"
    return f"installing llama-cpp-python ({so_far} so far, usually under a minute)"


def available_memory_mb() -> int | None:
    """Memory a new allocation can get, in MB, or ``None`` when unknown.

    The lower of the kernel's ``MemAvailable`` (free + reclaimable cache; swap is
    deliberately not counted) and what is left under a container memory limit
    (cgroup v2, then v1): in Docker the host can have plenty free while the
    container is one allocation away from being OOM-killed.
    """
    candidates: list[int] = []
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                candidates.append(int(line.split()[1]) >> 10)
                break
    except (OSError, ValueError, IndexError):
        pass
    for limit_file, usage_file in (
        ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory.current"),
        (
            "/sys/fs/cgroup/memory/memory.limit_in_bytes",
            "/sys/fs/cgroup/memory/memory.usage_in_bytes",
        ),
    ):
        try:
            limit = Path(limit_file).read_text().strip()
            usage = int(Path(usage_file).read_text().strip())
        except (OSError, ValueError):
            continue
        # "max" (v2) or a huge sentinel (v1) means no limit.
        if limit.isdigit() and int(limit) < (1 << 60):
            candidates.append(max(0, int(limit) - usage) >> 20)
        break
    return min(candidates) if candidates else None


class LlmUserError(RuntimeError):
    """A failure whose message is already written for the mesh: short, plain,
    nothing internal in it (not enough memory, model not found, ...). Anything
    else is summarized by :func:`_plain_reason` and its raw text is kept for
    admins only."""


class _PackageMissingError(LlmUserError):
    """llama_cpp is not importable (yet). Rechecked on every question -- no
    retry pause -- so the bot answers the moment a background install ends."""


class LlmBusyError(RuntimeError):
    """Another question is being answered; the model runs one at a time."""


class LlmPromptTooLongError(RuntimeError):
    """The prompt does not fit the context window (emoji cost several tokens)."""


class LlmWorkerDiedError(RuntimeError):
    """The model process stopped mid-answer; the message says why, in plain words."""


# The child process: how long a load may take (a cold read of a 1 GB model from
# an SD card) and how long past its own deadline an answer may be late before
# the child is presumed stuck and killed.
LOAD_TIMEOUT_SECONDS = 120
ANSWER_GRACE_SECONDS = 3
# How often the idle reaper looks for a model to unload.
IDLE_CHECK_SECONDS = 10
DEFAULT_IDLE_UNLOAD_SECONDS = 300

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _exit_reason(returncode: int | None) -> str:
    """Why the model process ended, for a mesh reply."""
    if returncode == -9:
        return "the model was stopped by the system, most likely because memory ran out"
    if returncode == -4:
        return "the model crashed: this llama.cpp build uses CPU instructions this machine lacks"
    if returncode is not None and returncode < 0:
        return f"the model process was killed by signal {-returncode}"
    return f"the model process exited unexpectedly (code {returncode})"


class _ModelProcess:
    """One loaded model, in a child process spoken to over JSON lines.

    Requests go to the child's stdin, one per line; each gets exactly one reply
    line. A reply that never comes -- EOF, or silence past the timeout -- means
    the child is dead or stuck, and it is killed so the next load starts clean.
    """

    def __init__(
        self, path: Path, *, threads: int, repack: bool, log_path: Path, env: dict | None
    ) -> None:
        child_env = dict(os.environ if env is None else env)
        child_env["PYTHONPATH"] = os.pathsep.join(
            p for p in (str(_REPO_ROOT), child_env.get("PYTHONPATH", "")) if p
        )
        self._log = log_path.open("w")
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "app.bots.llm", "--worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            cwd=str(_REPO_ROOT),
            env=child_env,
            text=True,
            bufsize=1,
        )
        reply = self._request(
            {
                "cmd": "load",
                "path": str(path),
                "n_ctx": CONTEXT_TOKENS,
                "n_batch": BATCH_TOKENS,
                "threads": threads,
                "use_mmap": USE_MMAP,
                "repack": repack,
            },
            LOAD_TIMEOUT_SECONDS,
        )
        if not reply.get("ok"):
            self.close()
            raise RuntimeError(reply.get("error") or "the model failed to load")

    @property
    def pid(self) -> int:
        return self._proc.pid

    def _request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        stdin, stdout = self._proc.stdin, self._proc.stdout
        assert stdin is not None and stdout is not None
        try:
            stdin.write(json.dumps(payload) + "\n")
            stdin.flush()
            ready, _, _ = select.select([stdout], [], [], timeout)
            line = stdout.readline() if ready else None
        except (BrokenPipeError, OSError, ValueError):
            line = ""
        if line is None:
            self.close()
            raise LlmWorkerDiedError(f"the model did not answer within {timeout:.0f} s")
        if not line:
            returncode = self._wait()
            self.close()
            raise LlmWorkerDiedError(_exit_reason(returncode))
        return json.loads(line)

    def _wait(self) -> int | None:
        try:
            return self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return None

    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        deadline: float,
    ) -> dict[str, Any]:
        return self._request(
            {
                "cmd": "generate",
                "messages": messages,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "deadline": deadline,
            },
            deadline + ANSWER_GRACE_SECONDS,
        )

    def close(self) -> None:
        """Stop the child (closing stdin is enough; kill if it lingers)."""
        with contextlib.suppress(OSError, ValueError):
            if self._proc.stdin:
                self._proc.stdin.close()
        if self._proc.poll() is None:
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._wait()
        with contextlib.suppress(OSError, ValueError):
            if self._proc.stdout:
                self._proc.stdout.close()
        self._log.close()


class LlmRuntime:
    """The process-wide model: background preparation, serialized generation,
    a child process holding the weights, and unloading when idle."""

    def __init__(self, model_dir: str | Path | None = None, worker_env: dict | None = None) -> None:
        self._model_dir = Path(model_dir) if model_dir is not None else None
        # Environment for the model process (tests put a fake llama_cpp first).
        self._worker_env = worker_env
        self._state_lock = threading.Lock()
        self._state_changed = threading.Condition(self._state_lock)
        # One request at a time to the model process.
        self._generate_lock = threading.Lock()
        self._proc: _ModelProcess | None = None
        self._spec: ModelSpec | None = None
        # idle | downloading | loading | ready | error
        self._state = "idle"
        self._error = ""
        # The raw exception behind _error, for admins asking in a DM.
        self._error_detail = ""
        self._done_bytes = 0
        self._total_bytes = 0
        self._worker: threading.Thread | None = None
        # (spec, threads, repack) of the current/last load: a change reloads.
        self._load_key: tuple[ModelSpec, int, bool] | None = None
        self._failed_at = 0.0
        self._last_used = 0.0
        self._unloaded = False
        self._idle_unload_seconds = DEFAULT_IDLE_UNLOAD_SECONDS
        self._reaper: threading.Thread | None = None

    # -- inspection ------------------------------------------------------
    @property
    def model_dir(self) -> Path:
        if self._model_dir is not None:
            return self._model_dir
        from app.config import settings

        return Path(settings.llm_model_dir)

    def status(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "state": self._state,
                "model": self._spec.key if self._spec else None,
                "model_name": self._spec.name if self._spec else None,
                "error": self._error,
                "error_detail": self._error_detail,
                "downloaded_bytes": self._done_bytes,
                "total_bytes": self._total_bytes,
                "unloaded": self._unloaded,
                "worker_pid": self._proc.pid if self._proc else None,
            }

    def describe(self, detailed: bool = False) -> str:
        """One short line for a mesh reply. ``detailed`` swaps a plain error
        for the raw exception: only for admins, only in a DM."""
        s = self.status()
        name = s["model_name"] or "no model"
        if s["state"] == "downloading":
            if s["total_bytes"]:
                pct = 100 * s["downloaded_bytes"] // s["total_bytes"]
                return f"Downloading {name}: {pct}% of {s['total_bytes'] // (1 << 20)} MB"
            return f"Downloading {name}: {s['downloaded_bytes'] // (1 << 20)} MB so far"
        if s["state"] == "loading":
            return f"Loading {name} into memory"
        if s["state"] == "ready":
            return f"{name} is ready"
        if s["state"] == "error":
            return f"{name} unavailable: {s['error_detail'] if detailed else s['error']}"
        if s["unloaded"]:
            return f"{name} is unloaded to save memory; the next question reloads it"
        return "No model loaded"

    # -- preparation -----------------------------------------------------
    def ensure(
        self,
        spec: ModelSpec,
        threads: int = 0,
        repack: bool = False,
        idle_unload_seconds: int = DEFAULT_IDLE_UNLOAD_SECONDS,
    ) -> str:
        """Make ``spec`` the loaded model, in the background. Returns the state.

        ``ready`` means :meth:`generate` can be called now. A preparation that is
        already running is never interrupted: asking for another model meanwhile
        returns the current state, and the switch happens on a later call.
        Changing ``threads`` or ``repack`` reloads the model with them. An
        unloaded model is reloaded. ``idle_unload_seconds`` is how long an unused
        model stays loaded (0: unload right after each answer).
        """
        load_key = (spec, threads, repack)
        with self._state_lock:
            self._idle_unload_seconds = max(0, int(idle_unload_seconds))
            busy = self._state in ("downloading", "loading")
            if busy or (self._state == "ready" and self._load_key == load_key):
                return self._state
            # Retry a failed model, but not on every message.
            failed_recently = time.monotonic() - self._failed_at < RETRY_AFTER_SECONDS
            if self._state == "error" and self._load_key == load_key and failed_recently:
                return self._state
            self._spec = spec
            self._load_key = load_key
            self._state = "downloading"
            self._error = ""
            self._error_detail = ""
            self._done_bytes = 0
            self._total_bytes = spec.download_mb << 20
            self._worker = threading.Thread(
                target=self._prepare,
                args=(spec, threads, repack),
                name="llm-prepare",
                daemon=True,
            )
            self._worker.start()
            return self._state

    def wait_ready(self, timeout: float) -> str:
        """Block until a download/load settles or ``timeout`` passes; the state."""
        with self._state_changed:
            self._state_changed.wait_for(
                lambda: self._state not in ("downloading", "loading"), timeout
            )
            return self._state

    def _set(self, **fields: Any) -> None:
        with self._state_changed:
            for name, value in fields.items():
                setattr(self, f"_{name}", value)
            self._state_changed.notify_all()

    def _prepare(self, spec: ModelSpec, threads: int, repack: bool = False) -> None:
        try:
            # The package may have been installed since the server started
            # (run.sh compiles it in the background), so drop stale finder caches.
            # Only looked up here: the child process is what imports it.
            importlib.invalidate_caches()
            # (A test's worker_env brings its own llama_cpp for the child.)
            if importlib.util.find_spec("llama_cpp") is None and self._worker_env is None:
                raise _PackageMissingError(self._missing_package_reason())
            path = self._download(spec)
            self._set(state="loading")
            # Drop the previous model first, so a switch never holds two in RAM.
            with self._generate_lock:
                self._stop_process()
                self._check_memory(spec, path, repack)
                # One thread count for both phases: llama.cpp otherwise runs the
                # prompt on every core, and a Pi pinned at 100% on a marginal
                # power supply browns out and reboots.
                n_threads = threads or max(1, (os.cpu_count() or 2) // 2)
                proc = _ModelProcess(
                    path,
                    threads=n_threads,
                    repack=repack,
                    log_path=self.model_dir / ".worker.log",
                    env=self._worker_env,
                )
                self._proc = proc
            self._set(state="ready", unloaded=False, last_used=time.monotonic())
            self._start_reaper()
            logger.info("tinyllm bot: %s loaded in process %d", spec.name, proc.pid)
        except Exception as exc:  # noqa: BLE001 - surfaced through status()
            logger.warning("tinyllm bot: preparing %s failed: %s", spec.name, exc)
            failed_at = 0.0 if isinstance(exc, _PackageMissingError) else time.monotonic()
            self._set(
                state="error",
                error=_plain_reason(exc)[:200],
                error_detail=(str(exc) or type(exc).__name__)[:500],
                failed_at=failed_at,
            )

    def _check_memory(self, spec: ModelSpec, path: Path, repack: bool = False) -> None:
        """Refuse a load that would not leave the system room to breathe.

        With repacking on, the weights may be held twice (see _weight_repacking).
        """
        available = available_memory_mb()
        if available is None:
            return
        weights_mb = (path.stat().st_size >> 20) * (2 if repack else 1)
        needed = weights_mb + LOAD_OVERHEAD_MB + MEMORY_RESERVE_MB
        if available < needed:
            raise LlmUserError(
                f"not enough free memory for {spec.name}: needs about {needed} MB, "
                f"{available} MB available. Pick a smaller model"
            )

    def _missing_package_reason(self) -> str:
        """Why llama_cpp will not import, using the markers ``run.sh`` leaves.

        In Docker, ``MESHCORE_ENABLE_LLM`` compiles llama-cpp-python in the
        background after the server starts; the package appears mid-run and is
        picked up by the next retry, so "still installing" is worth saying.
        """
        marker = self.model_dir / ".installing"
        if marker.exists():
            return _describe_install(marker)
        if (self.model_dir / ".install-failed").exists():
            return "installing llama-cpp-python failed; see .install.log in the model folder"
        return INSTALL_HINT

    def _download(self, spec: ModelSpec) -> Path:
        import httpx

        target_dir = self.model_dir / spec.repo.replace("/", "__")
        target = target_dir / spec.filename
        if target.exists() and target.stat().st_size > 0:
            self._set(done_bytes=target.stat().st_size, total_bytes=target.stat().st_size)
            return target
        target_dir.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            with client.stream("GET", spec.url) as response:
                if response.status_code == 404:
                    raise LlmUserError(f"{spec.repo}/{spec.filename} not found on Hugging Face")
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                free = shutil.disk_usage(target_dir).free
                if total and free - total < DISK_HEADROOM_BYTES:
                    raise LlmUserError(
                        f"not enough disk: needs {total >> 20} MB plus "
                        f"{DISK_HEADROOM_BYTES >> 20} MB headroom, {free >> 20} MB free"
                    )
                self._set(total_bytes=total, done_bytes=0)
                done = 0
                with partial.open("wb") as handle:
                    for chunk in response.iter_bytes(DOWNLOAD_CHUNK_BYTES):
                        handle.write(chunk)
                        done += len(chunk)
                        self._set(done_bytes=done)
        if total and done != total:
            partial.unlink(missing_ok=True)
            raise LlmUserError(f"download incomplete ({done} of {total} bytes)")
        os.replace(partial, target)
        return target

    # -- unloading -------------------------------------------------------
    def _stop_process(self) -> None:
        """Stop the model process, if any. Caller holds the generate lock."""
        proc, self._proc = self._proc, None
        if proc is not None:
            proc.close()

    def unload_if_idle(self, now: float | None = None) -> bool:
        """Unload a model unused for the idle timeout. Returns whether it did."""
        now = time.monotonic() if now is None else now
        with self._state_lock:
            idle_for = now - self._last_used
            due = (
                self._state == "ready"
                and self._idle_unload_seconds > 0
                and idle_for >= self._idle_unload_seconds
            )
        # Never wait on an answer in progress: it will be idle again later.
        if not due or not self._generate_lock.acquire(blocking=False):
            return False
        try:
            # A reload may have started between the check and the lock.
            if self.status()["state"] != "ready":
                return False
            self._stop_process()
            self._set(state="idle", unloaded=True)
        finally:
            self._generate_lock.release()
        logger.info("tinyllm bot: model unused for %d s, unloaded to free memory", idle_for)
        return True

    def _start_reaper(self) -> None:
        with self._state_lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap, name="llm-idle", daemon=True)
            self._reaper.start()

    def _reap(self) -> None:
        while True:
            time.sleep(IDLE_CHECK_SECONDS)
            with contextlib.suppress(Exception):
                self.unload_if_idle()

    # -- generation ------------------------------------------------------
    def generate(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        temperature: float,
        deadline_seconds: float,
    ) -> str:
        """Answer ``messages`` (blocking). Stops at ``deadline_seconds``.

        Tokens are streamed in the model process so a slow host still returns
        what it produced in time rather than nothing. Raises
        :class:`LlmBusyError` when another answer is in progress,
        :class:`LlmPromptTooLongError` for a prompt over the context window,
        :class:`LlmWorkerDiedError` when the model process died (out of memory,
        typically), and ``RuntimeError`` when no model is ready.
        """
        if not self._generate_lock.acquire(timeout=1.0):
            raise LlmBusyError("busy answering someone else")
        try:
            proc = self._proc
            if proc is None or self.status()["state"] != "ready":
                raise RuntimeError("model is not loaded")
            try:
                reply = proc.generate(
                    messages,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    deadline=deadline_seconds,
                )
            except LlmWorkerDiedError as exc:
                self._proc = None
                self._set(
                    state="error",
                    error=str(exc)[:200],
                    error_detail=str(exc)[:500],
                    failed_at=time.monotonic(),
                )
                logger.warning("tinyllm bot: %s", exc)
                raise
            if reply.get("kind") == "too_long":
                raise LlmPromptTooLongError(reply.get("error", ""))
            if not reply.get("ok"):
                raise RuntimeError(reply.get("error") or "generation failed")
            return str(reply.get("text", "")).strip()
        finally:
            self._set(last_used=time.monotonic())
            if self._idle_unload_seconds == 0 and self._proc is not None:
                self._stop_process()
                self._set(state="idle", unloaded=True)
            self._generate_lock.release()


llm_runtime = LlmRuntime()


# -- DM conversation memory --------------------------------------------------

# A DM conversation with the bot is a session: it is forgotten after this long
# without a message, and only this many senders are remembered at once.
SESSION_IDLE_SECONDS = 3600
SESSION_MAX_SENDERS = 100
# The most messages kept per sender (the setting's maximum), and the most
# history text sent to the model: the context window is CONTEXT_TOKENS, and on
# a Pi every token of prompt costs time out of the 10 s run.
SESSION_MAX_MESSAGES = 20
HISTORY_MAX_CHARS = 1000


class DmSessions:
    """Recent question/answer turns per DM sender, in memory.

    Held here rather than in the bot's persisted ``state``: bot runs execute
    concurrently and each saves its own copy of ``state``, so two people asking
    at once could overwrite each other's turns. Here it survives settings saves
    (the bot's code is re-exec'd, this module is not); a server restart starts
    everyone afresh, which is what a session is.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: OrderedDict[str, tuple[float, list[dict[str, str]]]] = OrderedDict()

    def history(self, sender: str, limit: int, now: float | None = None) -> list[dict[str, str]]:
        """The last ``limit`` messages with ``sender``, oldest first, trimmed to
        HISTORY_MAX_CHARS and always starting with the sender's own message."""
        if limit <= 0:
            return []
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._sessions.get(sender)
            if entry is None:
                return []
            if now - entry[0] > SESSION_IDLE_SECONDS:
                del self._sessions[sender]
                return []
            messages = list(entry[1][-limit:])
        while messages and sum(len(m["content"]) for m in messages) > HISTORY_MAX_CHARS:
            messages.pop(0)
        while messages and messages[0]["role"] != "user":
            messages.pop(0)
        return messages

    def record(self, sender: str, question: str, answer: str, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            _, messages = self._sessions.pop(sender, (now, []))
            messages = messages + [
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            ]
            self._sessions[sender] = (now, messages[-SESSION_MAX_MESSAGES:])
            while len(self._sessions) > SESSION_MAX_SENDERS:
                self._sessions.popitem(last=False)

    def forget(self, sender: str) -> bool:
        with self._lock:
            return self._sessions.pop(sender, None) is not None


dm_sessions = DmSessions()


# -- the model process -------------------------------------------------------


def _stream_answer(llm: Any, request: dict[str, Any]) -> str:
    started = time.monotonic()
    pieces: list[str] = []
    try:
        # llama-cpp-python checks the prompt length on the first read; other
        # versions may do it on the call itself, so both sit inside the try.
        stream = llm.create_chat_completion(
            messages=request["messages"],
            max_tokens=request["max_tokens"],
            temperature=request["temperature"],
            repeat_penalty=1.1,
            stream=True,
        )
        for chunk in stream:
            delta = chunk["choices"][0].get("delta", {}).get("content")
            if delta:
                pieces.append(delta)
            if time.monotonic() - started > request["deadline"]:
                break
    except ValueError as exc:
        if "context window" in str(exc):
            raise LlmPromptTooLongError(str(exc)) from exc
        raise
    return "".join(pieces)


def _worker_main() -> int:
    """Entry point of the model process: ``python -m app.bots.llm --worker``."""
    # First in line for the OOM killer, so running out of memory costs the model,
    # not the radio server. Raising one's own score needs no privilege.
    with contextlib.suppress(OSError):
        Path("/proc/self/oom_score_adj").write_text("1000")
    # Replies go to the real stdout; anything a native library prints goes to
    # stderr (the worker log) instead of corrupting the protocol.
    replies = os.fdopen(os.dup(1), "w", buffering=1)
    os.dup2(2, 1)
    llm: Any = None
    for line in sys.stdin:
        request = json.loads(line)
        try:
            if request["cmd"] == "load":
                from llama_cpp import Llama  # type: ignore[import-not-found]

                with _weight_repacking(request["repack"]):
                    llm = Llama(
                        model_path=request["path"],
                        n_ctx=request["n_ctx"],
                        n_batch=request["n_batch"],
                        n_threads=request["threads"],
                        n_threads_batch=request["threads"],
                        use_mmap=request["use_mmap"],
                        use_mlock=False,
                        verbose=False,
                    )
                reply: dict[str, Any] = {"ok": True}
            elif request["cmd"] == "generate":
                if llm is None:
                    raise RuntimeError("model is not loaded")
                reply = {"ok": True, "text": _stream_answer(llm, request)}
            else:
                reply = {"ok": False, "error": f"unknown command {request['cmd']!r}"}
        except LlmPromptTooLongError as exc:
            reply = {"ok": False, "kind": "too_long", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - reported to the server
            reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
        replies.write(json.dumps(reply) + "\n")
    return 0


if __name__ == "__main__" and "--worker" in sys.argv:
    sys.exit(_worker_main())
