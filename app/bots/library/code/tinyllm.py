"""tinyllm: ask a tiny on-device language model — ``ask``, ``ai`` or ``llm <question>``.

Answers with a small GGUF model run inside this server by llama-cpp-python —
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

from app.bots.llm import (
    CUSTOM_MODEL,
    DEFAULT_MODEL,
    LlmBusyError,
    llm_runtime,
    model_options,
    resolve_spec,
)
from remoteterm import bot

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant on a low-bandwidth mesh radio network. "
    "Answer in one or two short sentences, plain text, no markdown, under 200 characters."
)

BOT_META = {
    "key": "tinyllm",
    "name": "tinyllm",
    "category": "Fun",
    "description": "Ask a tiny on-device AI model a question (no cloud, no Ollama)",
    "long_description": (
        "`ask <question>` (or `ai` / `llm <question>`) answers with a small language model that runs "
        "inside this server — nothing is sent to a cloud service. Choose the model below; each "
        "option lists its download size and the RAM it uses while loaded. The first question "
        "downloads and loads the model in the background (`ask` alone shows progress). Needs "
        "`uv sync --extra llm` on the server. Small models are chatty and often wrong: treat "
        "answers as entertainment, not facts."
    ),
    "version": "1.0.0",
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
            "help": "0 lets llama.cpp choose. Takes effect the next time a model is loaded.",
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
    },
}


def _number(ctx, key, default, low, high):
    try:
        value = float(ctx.settings.get(key, default))
    except (TypeError, ValueError):
        value = default
    return min(high, max(low, value))


@bot.on_keyword()
@bot.on_keyword("ask", "ai", "llm")
async def ask(ctx, msg):
    try:
        spec = resolve_spec(ctx.settings)
    except ValueError as exc:
        await ctx.reply(f"🤖 ask: {exc}")
        return

    state = llm_runtime.ensure(spec, threads=int(_number(ctx, "threads", 0, 0, 32)))
    question = msg.arg_text.strip()
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

    messages = [
        {
            "role": "system",
            "content": str(ctx.settings.get("system_prompt") or DEFAULT_SYSTEM_PROMPT),
        },
        {"role": "user", "content": question[:500]},
    ]
    try:
        answer = await asyncio.to_thread(
            llm_runtime.generate,
            messages,
            max_tokens=int(_number(ctx, "max_tokens", 64, 16, 160)),
            temperature=_number(ctx, "temperature", 0.7, 0.0, 1.5),
            deadline_seconds=_number(ctx, "time_limit_seconds", 6, 2, 7),
        )
    except LlmBusyError:
        await ctx.reply("🤖 Busy answering someone else, try again shortly.")
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
