"""tinyllm: ask a tiny on-device language model — ``ask``, ``ai``, ``llm`` or ``tinyllm``.

Answers with a small GGUF model run on this server by llama-cpp-python —
no Ollama, no cloud API, nothing to host. Pick the model in Settings: the
dropdown shows each one's download size, RAM use, speed and quality, from the
catalog in ``app/bots/llm.py``.

The first question after enabling (or after switching models) starts a one-time
download plus load in the background and says so; ``ask`` on its own reports
progress. Bot runs are cut off at 10 s, so answers are generated against a
deadline and whatever was produced by then is sent. One question is answered
at a time.

Needs the optional ``llm`` extra: ``uv sync --extra llm``.
"""

import asyncio
import time

from app.bots.llm import (
    CUSTOM_MODEL,
    DEFAULT_MODEL,
    LlmBusyError,
    LlmPromptTooLongError,
    LlmWorkerDiedError,
    dm_sessions,
    llm_runtime,
    model_options,
    resolve_spec,
)
from remoteterm import bot

# How long a question waits for a reload before answering "warming up", and
# the time a run may use before the engine's 10 s timeout, leaving room to send.
RELOAD_WAIT_SECONDS = 4
RUN_BUDGET_SECONDS = 7.5
# `ask reset` / `ask forget` in a DM clears that sender's conversation memory.
RESET_WORDS = frozenset({"reset", "forget"})

# The system prompt is either the selected model's own (llm.CATALOG: each is
# sized to what that model can follow, and follows it when the model changes)
# or the operator's custom text, which is never touched.
PROMPT_MATCH_MODEL = "model"
PROMPT_CUSTOM = "custom"

# Every default prompt shipped before per-model prompts. Stored settings are
# not rewritten by a version refresh, so an install still holding one of these
# was never customized: migrate_settings turns it into "match the model". A
# prompt the operator wrote is never in this set and becomes "custom".
_PREVIOUS_DEFAULT_PROMPTS = frozenset(
    {
        "You are a helpful assistant on a low-bandwidth mesh radio network. "
        "Answer in one or two short sentences, plain text, no markdown, under 200 characters.",
        "You are a helpful assistant on a low-bandwidth mesh radio network. "
        "Answer in one or two short sentences, plain text, no markdown, under 140 characters.",
    }
)
# The answer-length default before 1.2.0; still holding it means never changed.
_PREVIOUS_DEFAULT_MAX_TOKENS = 64

BOT_META = {
    "key": "tinyllm",
    "name": "tinyllm",
    "category": "Fun",
    "description": "Ask a tiny on-device AI model a question (no cloud, no Ollama)",
    "long_description": (
        "`ask <question>` (or `ai`, `llm` or `tinyllm <question>`) answers with a small language model that runs "
        "on this server — nothing is sent to a cloud service. Choose the model below; each "
        "option lists its download size and the RAM it uses while loaded. The first question "
        "downloads and loads the model in the background (`ask` alone shows progress). Needs "
        "`uv sync --extra llm` on the server. Small models are chatty and often wrong: treat "
        "answers as entertainment, not facts."
    ),
    "version": "1.3.0",
    "cooldown_seconds": 3,
    "per_user_cooldown_seconds": 20,
    "settings_schema": [
        {
            "key": "model",
            "label": "Model",
            "type": "select",
            "default": DEFAULT_MODEL,
            "options": model_options(),
            "help": (
                "Downloaded once from Hugging Face on first use, then loaded into RAM and kept "
                "there while the server runs. Switching models frees the previous one."
            ),
        },
        {
            "key": "custom_repo",
            "label": "Custom model repository",
            "type": "text",
            "default": "",
            "help": "Hugging Face repository holding the GGUF file, e.g. Qwen/Qwen2.5-0.5B-Instruct-GGUF",
            "show_when": {"key": "model", "value": CUSTOM_MODEL},
        },
        {
            "key": "custom_file",
            "label": "Custom model file",
            "type": "text",
            "default": "",
            "help": "GGUF file name in that repository, e.g. qwen2.5-0.5b-instruct-q4_k_m.gguf",
            "show_when": {"key": "model", "value": CUSTOM_MODEL},
        },
        {
            "key": "prompt_mode",
            "label": "System prompt",
            "type": "select",
            "default": PROMPT_MATCH_MODEL,
            "options": [
                {
                    "value": PROMPT_MATCH_MODEL,
                    "label": "Match the selected model (changes with it)",
                    "description": (
                        "Each model comes with a prompt sized to what it can follow -- it is "
                        "shown in the model's details above. Picking another model switches "
                        "to its prompt."
                    ),
                },
                {
                    "value": PROMPT_CUSTOM,
                    "label": "Custom",
                    "description": (
                        "Your own prompt, below. It is kept exactly as written whatever "
                        "model you pick."
                    ),
                },
            ],
        },
        {
            "key": "system_prompt",
            "label": "Custom system prompt",
            "type": "text",
            "default": "",
            "help": (
                "The bot's personality and rules. Keep it short and plain: the smallest "
                "models repeat what the prompt says about them rather than follow it."
            ),
            "show_when": {"key": "prompt_mode", "value": PROMPT_CUSTOM},
        },
        {
            "key": "max_tokens",
            "label": "Max answer length (tokens)",
            "type": "int",
            "default": 40,
            "min": 16,
            "max": 160,
            "help": "About 4 characters per token. Shorter is also faster.",
        },
        {
            "key": "max_messages",
            "label": "Max messages per answer",
            "type": "int",
            "default": 1,
            "min": 1,
            "max": 4,
            "help": (
                "A hard cap, whatever the model writes: a longer answer is cut back to "
                "whole sentences that fit."
            ),
        },
        {
            "key": "history_messages",
            "label": "DM memory (previous messages included)",
            "type": "int",
            "default": 10,
            "min": 0,
            "max": 20,
            "help": (
                "In a DM the bot remembers the conversation: this many earlier messages "
                "(questions and answers) go to the model with each new question. 0 turns "
                "it off. A conversation is forgotten after an hour of silence or on "
                "`ask reset`; channels and rooms are never remembered. More memory makes a "
                "Pi slower to answer."
            ),
        },
        {
            "key": "temperature",
            "label": "Temperature",
            "type": "float",
            "default": 0.7,
            "min": 0,
            "max": 1.5,
            "help": "Lower is more predictable, higher more creative.",
        },
        {
            "key": "time_limit_seconds",
            "label": "Answer time limit (seconds)",
            "type": "float",
            "default": 6,
            "min": 2,
            "max": 7,
            "help": (
                "Generation stops here and the text so far is sent. Bot runs end at 10 s, and "
                "the reply still has to go out, so this stays at 7 or less."
            ),
        },
        {
            "key": "threads",
            "label": "CPU threads",
            "type": "int",
            "default": 0,
            "min": 0,
            "max": 32,
            "help": (
                "0 uses half the cores, which keeps a Pi responsive and off the edge of its "
                "power supply. Changing it reloads the model."
            ),
        },
        {
            "key": "unload_after_minutes",
            "label": "Unload the model after (minutes idle)",
            "type": "int",
            "default": 5,
            "min": 0,
            "max": 1440,
            "help": (
                "Frees the model's memory when nobody has asked anything for this long; the "
                "next question reloads it (about a second, a few on a Pi). 0 unloads right "
                "after every answer, so the memory is only used while answering."
            ),
        },
        {
            "key": "fast_arm_layout",
            "label": "Faster ARM weight layout (uses about twice the memory)",
            "type": "bool",
            "default": False,
            "help": (
                "On a Pi 5 and other recent ARM CPUs, llama.cpp can rearrange the model in "
                "memory so answers come faster -- but it keeps a second copy of the model to "
                "do it, which can run a small board out of memory. Leave off unless you have "
                "RAM to spare. Changing it reloads the model."
            ),
        },
    ],
    "settings": {
        "model": DEFAULT_MODEL,
        "custom_repo": "",
        "custom_file": "",
        "prompt_mode": PROMPT_MATCH_MODEL,
        "system_prompt": "",
        "max_tokens": 40,
        "max_messages": 1,
        "history_messages": 10,
        "temperature": 0.7,
        "time_limit_seconds": 6,
        "threads": 0,
        "unload_after_minutes": 5,
        "fast_arm_layout": False,
    },
}


def migrate_settings(settings):
    """Stored settings from before per-model prompts (run by library seeding on
    refresh, and by every run so both agree): an untouched default prompt
    becomes "match the model", a hand-written one becomes "custom"."""
    if "prompt_mode" not in settings:
        text = str(settings.get("system_prompt") or "").strip()
        if text and text not in _PREVIOUS_DEFAULT_PROMPTS:
            settings["prompt_mode"] = PROMPT_CUSTOM
        else:
            settings["prompt_mode"] = PROMPT_MATCH_MODEL
            settings["system_prompt"] = ""
    if settings.get("max_tokens") == _PREVIOUS_DEFAULT_MAX_TOKENS:
        settings["max_tokens"] = 40
    return settings


def system_prompt_for(settings, spec):
    """The prompt in effect: the operator's custom text, or the model's own."""
    settings = migrate_settings(dict(settings))
    custom = str(settings.get("system_prompt") or "").strip()
    if settings.get("prompt_mode") == PROMPT_CUSTOM and custom:
        return custom
    return spec.system_prompt


def fit_messages(text, budget_bytes, max_messages):
    """Cut ``text`` back to whole sentences that fit ``max_messages`` messages.

    The model is asked for short answers but tiny ones do not reliably obey, so
    this is the hard limit. Multi-part replies carry a "(i/n) " prefix each.
    """
    allowed = budget_bytes * max_messages - (8 * max_messages if max_messages > 1 else 0)
    if len(text.encode()) <= allowed:
        return text
    cut = text.encode()[: allowed - 3].decode(errors="ignore")
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if cut[-1:] in ".!?":
        end = len(cut) - 1
    if end >= len(cut) * 0.4:
        return cut[: end + 1]
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut).rstrip(" ,;:") + "\u2026"


def _number(ctx, key, default, low, high):
    try:
        value = float(ctx.settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


@bot.on_keyword()
@bot.on_keyword("ask", "ai", "llm", "tinyllm")
async def ask(ctx, msg):
    try:
        spec = resolve_spec(ctx.settings)
    except ValueError as exc:
        await ctx.reply(f"🤖 ask: {exc}")
        return

    if msg.is_dm and msg.sender_key and msg.arg_text.strip().lower() in RESET_WORDS:
        forgot = dm_sessions.forget(msg.sender_key)
        await ctx.reply("🤖 Conversation forgotten." if forgot else "🤖 Nothing to forget.")
        return
    started = time.monotonic()
    state = llm_runtime.ensure(
        spec,
        threads=int(_number(ctx, "threads", 0, 0, 32)),
        repack=bool(ctx.settings.get("fast_arm_layout", False)),
        idle_unload_seconds=int(_number(ctx, "unload_after_minutes", 5, 0, 1440) * 60),
    )
    question = msg.arg_text.strip()
    if state in ("downloading", "loading"):
        # A model unloaded while idle reloads in about a second (a few on a Pi
        # reading from SD): wait for it and answer in this same run.
        state = await asyncio.to_thread(llm_runtime.wait_ready, RELOAD_WAIT_SECONDS)
    # Raw errors (library paths, exception text) go only to an admin in a DM;
    # everyone else, and every channel, gets the plain one-line reason.
    detailed = bool(msg.is_dm and getattr(ctx, "sender_is_admin", False))
    if state != "ready":
        if state == "error":
            await ctx.reply_split(f"🤖 {llm_runtime.describe(detailed=detailed)}")
        elif question:
            await ctx.reply(f"🤖 Warming up — {llm_runtime.describe()}. Ask again in a minute.")
        else:
            await ctx.reply(f"🤖 {llm_runtime.describe()}")
        return
    if not question:
        await ctx.reply(f"🤖 {spec.name} is ready. Usage: {ctx.command_prefix}ask <question>")
        return

    question = question[:500]
    # DM memory: earlier turns with this sender, never in channels or rooms.
    session = msg.sender_key if (msg.is_dm and msg.sender_key) else None
    history_limit = int(_number(ctx, "history_messages", 10, 0, 20))
    history = dm_sessions.history(session, history_limit) if session else []
    system = {"role": "system", "content": system_prompt_for(ctx.settings, spec)}

    def ask_model(earlier):
        return asyncio.to_thread(
            llm_runtime.generate,
            [system, *earlier, {"role": "user", "content": question}],
            max_tokens=int(_number(ctx, "max_tokens", 40, 16, 160)),
            temperature=_number(ctx, "temperature", 0.7, 0.0, 1.5),
            # The whole run must end inside the engine's 10 s: time spent
            # reloading comes out of the answer's budget.
            deadline_seconds=max(
                1.5,
                min(
                    _number(ctx, "time_limit_seconds", 6, 2, 7),
                    RUN_BUDGET_SECONDS - (time.monotonic() - started),
                ),
            ),
        )

    try:
        try:
            answer = await ask_model(history)
        except LlmPromptTooLongError:
            if not history:
                raise
            # The conversation outgrew the context window: answer this one
            # question on its own rather than not at all.
            answer = await ask_model([])
    except LlmBusyError:
        await ctx.reply("🤖 Busy answering someone else, try again shortly.")
        return
    except LlmWorkerDiedError as exc:
        await ctx.reply_split(f"🤖 Sorry, {exc}. Try again, or pick a smaller model.")
        return
    except LlmPromptTooLongError:
        await ctx.reply("🤖 That question is too long for this model, try a shorter one.")
        return
    except Exception as exc:  # noqa: BLE001 - the mesh gets a line, the log gets the rest
        ctx.log(f"generation failed: {exc}", "WARNING")
        if detailed:
            await ctx.reply_split(f"🤖 The model failed to answer: {exc}")
        else:
            await ctx.reply("🤖 Sorry, the model failed to answer.")
        return

    answer = " ".join(answer.split()) or "(no answer)"
    if not msg.is_dm and msg.sender_name:
        # @[name] is the mention syntax mesh clients highlight.
        answer = f"@[{msg.sender_name}] {answer}"
    budget = await ctx.reply_budget()
    answer = fit_messages(answer, budget, int(_number(ctx, "max_messages", 1, 1, 4)))
    await ctx.reply_split(answer)
    if session and history_limit:
        # What was actually sent, so the model sees the conversation as it went.
        dm_sessions.record(session, question, answer)
