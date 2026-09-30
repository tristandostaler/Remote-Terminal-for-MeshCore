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
    llm_runtime,
    model_options,
    resolve_spec,
)
from remoteterm import bot

# How long a question waits for a reload before answering "warming up", and
# the time a run may use before the engine's 10 s timeout, leaving room to send.
RELOAD_WAIT_SECONDS = 4
RUN_BUDGET_SECONDS = 7.5

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant on a low-bandwidth mesh radio network. "
    "Answer in one or two short sentences, plain text, no markdown, under 140 characters."
)
# Earlier shipped defaults. A version refresh updates a built-in's code but never
# its stored settings, so an install seeded before a prompt change still holds
# the old default there. Reading those as "not customized" delivers the new one;
# a prompt the operator actually wrote is never matched and never replaced.
_PREVIOUS_DEFAULT_PROMPTS = frozenset(
    {
        "You are a helpful assistant on a low-bandwidth mesh radio network. "
        "Answer in one or two short sentences, plain text, no markdown, under 200 characters.",
    }
)

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
    "version": "1.1.0",
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
            "key": "system_prompt",
            "label": "System prompt",
            "type": "text",
            "default": DEFAULT_SYSTEM_PROMPT,
            "help": "The bot's personality and rules. Keep it short: tiny models follow short prompts best.",
        },
        {
            "key": "max_tokens",
            "label": "Max answer length (tokens)",
            "type": "int",
            "default": 64,
            "min": 16,
            "max": 160,
            "help": "About 4 characters per token. 64 is roughly one or two mesh messages.",
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
        "system_prompt": DEFAULT_SYSTEM_PROMPT,
        "max_tokens": 64,
        "temperature": 0.7,
        "time_limit_seconds": 6,
        "threads": 0,
        "unload_after_minutes": 5,
        "fast_arm_layout": False,
    },
}


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
    if state != "ready":
        if state == "error":
            await ctx.reply_split(f"🤖 {llm_runtime.describe()}")
        elif question:
            await ctx.reply(f"🤖 Warming up — {llm_runtime.describe()}. Ask again in a minute.")
        else:
            await ctx.reply(f"🤖 {llm_runtime.describe()}")
        return
    if not question:
        await ctx.reply(f"🤖 {spec.name} is ready. Usage: {ctx.command_prefix}ask <question>")
        return

    system_prompt = str(ctx.settings.get("system_prompt") or "").strip()
    if not system_prompt or system_prompt in _PREVIOUS_DEFAULT_PROMPTS:
        system_prompt = DEFAULT_SYSTEM_PROMPT
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question[:500]},
    ]
    try:
        answer = await asyncio.to_thread(
            llm_runtime.generate,
            messages,
            max_tokens=int(_number(ctx, "max_tokens", 64, 16, 160)),
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
        await ctx.reply("🤖 Sorry, the model failed to answer.")
        return

    answer = " ".join(answer.split()) or "(no answer)"
    if not msg.is_dm and msg.sender_name:
        # @[name] is the mention syntax mesh clients highlight.
        answer = f"@[{msg.sender_name}] {answer}"
    await ctx.reply_split(answer)
