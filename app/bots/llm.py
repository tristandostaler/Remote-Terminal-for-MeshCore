"""Tiny on-device language models for the ``ask`` bot.

The built-in ``ask`` bot (``library/code/ask.py``) answers questions with a
small GGUF model run in-process by ``llama-cpp-python`` — no Ollama, no server,
no GPU. This module owns what must outlive a single bot run:

* **The catalog** (:data:`CATALOG`): every model the bot's Settings dropdown
  offers, with its download size, memory footprint, speed and quality notes.
  The dropdown labels and the per-option descriptions are generated from it
  (:func:`model_options`), so the numbers the operator chooses by and the file
  that gets downloaded can never disagree.
* **The loaded model** (:data:`llm_runtime`): a process-wide singleton. Bot code
  is re-exec'd whenever the operator edits or reconfigures it, so a model held in
  the bot's own namespace would be reloaded — and a few hundred MB re-read —
  on every settings save. Here it is loaded once and swapped only when the
  selected model changes.

Every bot run is killed after ``BOT_EXECUTION_TIMEOUT`` (10 s), while a first
download is hundreds of MB. So preparation (download, then load) always runs in
a background thread and a run only *starts* it and reports progress; generation
streams tokens and stops at a deadline the caller keeps inside the timeout.

``llama-cpp-python`` is the optional ``llm`` extra (``uv sync --extra llm``).
Nothing imports it until a model is loaded, so the app runs without it.
"""

from __future__ import annotations

import importlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DOWNLOAD_CHUNK_BYTES = 1 << 20
# Headroom kept free after a download, so a model never fills the disk the
# SQLite database lives on (the same concern as the AEIC bundle on an SD card).
DISK_HEADROOM_BYTES = 512 * 1024 * 1024
# Context window. Mesh prompts and replies are a couple hundred characters, so
# 1024 tokens leaves room for the system prompt and keeps the KV cache small.
CONTEXT_TOKENS = 1024
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
            f"Source: huggingface.co/{self.repo}"
        )


def _fmt_mb(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1000 else f"{mb} MB"


# Sizes are the published GGUF file sizes; RAM is file + KV cache for
# CONTEXT_TOKENS + runtime overhead, rounded up. Speeds are rough CPU figures
# for a Raspberry Pi 5 / an x86 mini-PC; a Pi 4 is about half.
CATALOG: tuple[ModelSpec, ...] = (
    ModelSpec(
        key="smollm2-135m",
        name="SmolLM2 135M",
        repo="HuggingFaceTB/SmolLM2-135M-Instruct-GGUF",
        filename="smollm2-135m-instruct-q8_0.gguf",
        params="135M",
        quant="Q8_0",
        download_mb=145,
        ram_mb=250,
        speed="very fast (~40 tok/s Pi 5, 100+ tok/s x86)",
        quality="Toy: grammatical but often wrong or off-topic",
        notes="Smallest option; fine for playful one-liners, not for facts.",
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
    )


class LlmBusyError(RuntimeError):
    """Another question is being answered; the model runs one at a time."""


class LlmRuntime:
    """The process-wide model: background preparation, serialized generation."""

    def __init__(self, model_dir: str | Path | None = None) -> None:
        self._model_dir = Path(model_dir) if model_dir is not None else None
        self._state_lock = threading.Lock()
        # llama.cpp contexts are not safe to share between threads.
        self._generate_lock = threading.Lock()
        self._llm: Any = None
        self._spec: ModelSpec | None = None
        self._state = "idle"  # idle | downloading | loading | ready | error
        self._error = ""
        self._done_bytes = 0
        self._total_bytes = 0
        self._worker: threading.Thread | None = None
        self._failed_at = 0.0

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
                "downloaded_bytes": self._done_bytes,
                "total_bytes": self._total_bytes,
            }

    def describe(self) -> str:
        """One short line for a mesh reply."""
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
            return f"{name} unavailable: {s['error']}"
        return "No model loaded"

    # -- preparation -----------------------------------------------------
    def ensure(self, spec: ModelSpec, threads: int = 0) -> str:
        """Make ``spec`` the loaded model, in the background. Returns the state.

        ``ready`` means :meth:`generate` can be called now. A preparation that is
        already running is never interrupted: asking for another model meanwhile
        returns the current state, and the switch happens on a later call.
        """
        with self._state_lock:
            busy = self._state in ("downloading", "loading")
            if busy or (self._state == "ready" and self._spec == spec):
                return self._state
            # Retry a failed model, but not on every message.
            failed_recently = time.monotonic() - self._failed_at < RETRY_AFTER_SECONDS
            if self._state == "error" and self._spec == spec and failed_recently:
                return self._state
            self._spec = spec
            self._state = "downloading"
            self._error = ""
            self._done_bytes = 0
            self._total_bytes = spec.download_mb << 20
            self._worker = threading.Thread(
                target=self._prepare, args=(spec, threads), name="llm-prepare", daemon=True
            )
            self._worker.start()
            return self._state

    def _set(self, **fields: Any) -> None:
        with self._state_lock:
            for name, value in fields.items():
                setattr(self, f"_{name}", value)

    def _prepare(self, spec: ModelSpec, threads: int) -> None:
        try:
            # The package may have been installed since the server started
            # (run.sh compiles it in the background), so drop stale finder caches.
            importlib.invalidate_caches()
            try:
                from llama_cpp import Llama  # type: ignore[import-not-found]
            except ImportError as exc:
                raise RuntimeError(self._missing_package_reason()) from exc
            path = self._download(spec)
            self._set(state="loading")
            # Drop the previous model first, so a switch never holds two in RAM.
            with self._generate_lock:
                self._llm = None
                llm = Llama(
                    model_path=str(path),
                    n_ctx=CONTEXT_TOKENS,
                    n_threads=threads or None,
                    verbose=False,
                )
                self._llm = llm
            self._set(state="ready")
            logger.info("ask bot: %s loaded from %s", spec.name, path)
        except Exception as exc:  # noqa: BLE001 - surfaced through status()
            logger.warning("ask bot: preparing %s failed: %s", spec.name, exc)
            self._set(state="error", error=str(exc)[:200], failed_at=time.monotonic())

    def _missing_package_reason(self) -> str:
        """Why llama_cpp will not import, using the markers ``run.sh`` leaves.

        In Docker, ``MESHCORE_ENABLE_LLM`` compiles llama-cpp-python in the
        background after the server starts; the package appears mid-run and is
        picked up by the next retry, so "still installing" is worth saying.
        """
        if (self.model_dir / ".installing").exists():
            return "llama-cpp-python is still being installed (compiling, up to 20 min on a Pi)"
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
                    raise RuntimeError(f"{spec.repo}/{spec.filename} not found on Hugging Face")
                response.raise_for_status()
                total = int(response.headers.get("content-length") or 0)
                free = shutil.disk_usage(target_dir).free
                if total and free - total < DISK_HEADROOM_BYTES:
                    raise RuntimeError(
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
            raise RuntimeError(f"download incomplete ({done} of {total} bytes)")
        os.replace(partial, target)
        return target

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

        Tokens are streamed so a slow host still returns what it produced in
        time rather than nothing. Raises :class:`LlmBusyError` when another
        answer is in progress and ``RuntimeError`` when no model is ready.
        """
        started = time.monotonic()
        if not self._generate_lock.acquire(timeout=1.0):
            raise LlmBusyError("busy answering someone else")
        try:
            llm = self._llm
            if llm is None or self.status()["state"] != "ready":
                raise RuntimeError("model is not loaded")
            pieces: list[str] = []
            stream = llm.create_chat_completion(
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                repeat_penalty=1.1,
                stream=True,
            )
            for chunk in stream:
                delta = chunk["choices"][0].get("delta", {}).get("content")
                if delta:
                    pieces.append(delta)
                if time.monotonic() - started > deadline_seconds:
                    break
            return "".join(pieces).strip()
        finally:
            self._generate_lock.release()


llm_runtime = LlmRuntime()
