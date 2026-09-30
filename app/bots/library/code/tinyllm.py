"""tinyllm: ask a tiny on-device language model — ``ask``, ``ai``, ``llm`` or ``tinyllm``.

Answers with a small GGUF model run on this server by llama-cpp-python —
no Ollama, no cloud API, nothing to host. Pick the model in Settings: the
dropdown shows each one's download size, RAM use, speed and quality, from the
catalog in ``app/bots/bots_utils/tinyllm/llm.py``.

The first question after enabling (or after switching models) starts a one-time
download plus load in the background and says so; ``ask`` on its own reports
progress. Bot runs are cut off at 10 s, so answers are generated against a
deadline and whatever was produced by then is sent. One question is answered
at a time.

Needs the optional ``llm`` extra: ``uv sync --extra llm``.
"""

import asyncio
import re
import time

from app.bots.bots_utils.tinyllm.llm import (
    CONTEXT_CHOICES,
    CONTEXT_TOKENS,
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
# DM memory is read back from the conversation itself (the messages table):
# incoming messages that were questions to this bot, and the answers it sent
# right after them. `ask reset` / `ask forget` is a message too, so history
# simply stops there; so does an hour of silence.
KEYWORDS = ("ask", "ai", "llm", "tinyllm")
RESET_WORDS = frozenset({"reset", "forget"})
SESSION_IDLE_SECONDS = 3600
# The most history text sent to the model: the context window is 512 tokens,
# and on a Pi every token of prompt costs time out of the 10 s run.
HISTORY_MAX_CHARS = 1000
# The bot answers inside its 10 s run (a multi-part answer adds ~2 s a part),
# so only what was sent this soon after a question can be its answer -- not
# something the operator typed into the same DM later.
ANSWER_WINDOW_SECONDS = 30
# Rough prompt budgeting without a tokenizer in the server (only the model
# process has one): ~3 characters per token is conservative for English, and
# the chat template adds a little. What is left of the context after the
# prompt, question and answer goes to DM history (up to half) and reference
# notes (the rest); a prompt that still overflows is retried without history,
# then without notes.
CHARS_PER_TOKEN = 3
TEMPLATE_OVERHEAD_CHARS = 200
# The optional model check of the notes: its own time cap, and the least time
# that must be left in the run to try it at all (the answer still follows).
NOTES_CHECK_SECONDS = 2
NOTES_CHECK_MIN_SECONDS = 4
NOTES_HEADER = "\n\nReference notes (use them only if they answer the question):\n"
_PLACEHOLDER_RE = re.compile(r"\{(radio_name|sender|time|date)\}")
# A leading command prefix (!, ?, ...) or @[mention], then a trigger word.
_COMMAND_RE = re.compile(
    r"^\W*(?:@\[[^\]]*\]\s*)?(" + "|".join(KEYWORDS) + r")\b\s*(.*)$",
    re.IGNORECASE | re.DOTALL,
)
_PART_RE = re.compile(r"^\(\d+/\d+\)\s*")

# The system prompt is either the selected model's own (llm.CATALOG: each is
# sized to what that model can follow, and follows it when the model changes)
# or the operator's custom text, which is never touched.
PROMPT_MATCH_MODEL = "model"
PROMPT_CUSTOM = "custom"

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
    "version": "1.4.0",
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
                "it off. Read back from the DM conversation itself, so it survives "
                "restarts; it starts over after an hour of silence or on `ask reset`. "
                "Channels and rooms are never remembered. More memory makes a Pi slower "
                "to answer."
            ),
        },
        {
            "key": "use_docs",
            "label": "Look up reference notes",
            "type": "bool",
            "default": True,
            "help": (
                "Search the markdown files in the tinyllm-docs folder (beside the database; "
                "seeded with MeshCore basics and every repeater setting) and give the best "
                "matches to the model with each question. Edit or add .md files there; "
                "each heading starts a searchable section."
            ),
        },
        {
            "key": "check_notes_with_model",
            "label": "Ask the model whether the notes fit (slower)",
            "type": "bool",
            "default": False,
            "help": (
                "When the keyword search finds notes, first ask the model a quick yes/no: do "
                "they help answer this question? Filters matches that share words but not "
                "meaning. Costs one short extra model pass (under a second on a Pi 5 with a "
                "tiny model, more with bigger ones), skipped when time is short. Worth it with "
                "Qwen2.5 1.5B or Llama 3.2 1B; the tiny models judge this poorly."
            ),
        },
        {
            "key": "context_tokens",
            "label": "Context size",
            "type": "select",
            "default": str(CONTEXT_TOKENS),
            "options": [
                {
                    "value": "512",
                    "label": "512 tokens (smallest, fastest)",
                    "description": (
                        "Room for the question plus a little DM history and a few reference "
                        "notes. Best on a Pi."
                    ),
                },
                {
                    "value": "1024",
                    "label": "1024 tokens (~16 MB more memory)",
                    "description": (
                        "About twice the history and notes. Slower to answer on a Pi, where "
                        "every prompt token takes time."
                    ),
                },
                {
                    "value": "2048",
                    "label": "2048 tokens (~48 MB more memory)",
                    "description": (
                        "Plenty of room for notes and history; for a desktop-class CPU. On a "
                        "Pi a long prompt may not finish inside the 10 s bot limit."
                    ),
                },
            ],
            "help": "Changing it reloads the model.",
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
        "use_docs": True,
        "check_notes_with_model": False,
        "context_tokens": str(CONTEXT_TOKENS),
        "temperature": 0.7,
        "time_limit_seconds": 6,
        "threads": 0,
        "unload_after_minutes": 5,
        "fast_arm_layout": False,
    },
}


def system_prompt_for(settings, spec, values=None):
    """The prompt in effect -- the operator's custom text, or the model's own --
    with its placeholders ({radio_name}, {sender}, {time}, {date}) filled in.
    A plain substitution, so any other braces in a custom prompt are left be."""
    custom = str(settings.get("system_prompt") or "").strip()
    text = custom if settings.get("prompt_mode") == PROMPT_CUSTOM and custom else spec.system_prompt
    values = values or {}
    return _PLACEHOLDER_RE.sub(lambda m: str(values.get(m.group(1)) or m.group(0)), text)


def _radio_name():
    """This radio's advertised name, read without taking the radio lock."""
    try:
        from app.radio import radio_manager

        mc = radio_manager.meshcore
        return str((mc.self_info or {}).get("name") or "") if mc else ""
    except Exception:  # noqa: BLE001 - a name is a nicety, never a failure
        return ""


def prompt_values(sender_name):
    now = time.localtime()
    return {
        "radio_name": _radio_name() or "tinyllm",
        "sender": sender_name or "someone",
        "time": time.strftime("%H:%M", now),
        "date": time.strftime("%Y-%m-%d", now),
    }


def reference_notes(query, max_chars):
    """Reference notes for ``query`` within ``max_chars``: (rendered text,
    section headings), or ("", []) when nothing is relevant enough."""
    from app.bots.bots_utils.tinyllm.llm_docs import docs_index

    budget = max_chars - len(NOTES_HEADER)
    sections = docs_index().search(query, budget) if budget > 0 else []
    if not sections:
        return "", []
    text = NOTES_HEADER + "\n".join(s.render() for s in sections)
    return text, [s.title for s in sections]


def notes_check_messages(question, titles):
    """The yes/no question put to the model before using the notes. Generic on
    purpose: the notes can be about anything the operator documents."""
    return [
        {
            "role": "system",
            "content": "You decide if reference notes are useful. Answer yes or no.",
        },
        {
            "role": "user",
            "content": (
                f"Notes about: {'; '.join(titles)}\n"
                f"Question: {question}\n"
                "Do these notes help answer the question?"
            ),
        },
    ]


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


def conversation_turns(messages, now):
    """Rebuild question/answer turns from a DM conversation, oldest first.

    ``messages`` are the conversation's stored rows (any order). A turn is an
    incoming question to this bot plus the answer sent right after it: the
    bot's own notices (they start with the robot emoji) are not answers, and a
    multi-part answer's "(i/n)" parts are joined. Messages that were not
    questions to the bot -- ordinary chat with this contact -- are ignored.
    History starts after the last `ask reset` and after any gap of an hour.
    The question being answered right now is already stored; it is left out.
    """
    turns = []  # [question, [answer parts], received_at]
    answering = False  # still collecting the latest question's answer
    last_seen = None
    for message in sorted(messages, key=lambda m: (m.received_at, m.id)):
        if last_seen is not None and message.received_at - last_seen > SESSION_IDLE_SECONDS:
            turns = []
        last_seen = message.received_at
        if not message.outgoing:
            # Any incoming message ends the previous answer, question or not.
            answering = False
            match = _COMMAND_RE.match(message.text or "")
            if not match:
                continue
            question = match.group(2).strip()
            if question.lower() in RESET_WORDS:
                turns = []
            elif question:
                turns.append([question, [], message.received_at])
                answering = True
        elif (
            answering
            and message.received_at - turns[-1][2] <= ANSWER_WINDOW_SECONDS
            and not (message.text or "").startswith("\U0001f916")
        ):
            turns[-1][1].append(_PART_RE.sub("", message.text or "").strip())
    if last_seen is not None and now - last_seen > SESSION_IDLE_SECONDS:
        return []
    # The current question has no answer yet; so has any the bot never answered.
    history = []
    for question, parts, _ in turns:
        if parts:
            history.append({"role": "user", "content": question})
            history.append({"role": "assistant", "content": " ".join(parts)})
    return history


def _trimmed(history, limit, max_chars):
    """The last ``limit`` messages, within ``max_chars``, starting with a question."""
    history = history[-limit:]
    while history and sum(len(m["content"]) for m in history) > max_chars:
        history.pop(0)
    while history and history[0]["role"] != "user":
        history.pop(0)
    return history


async def dm_history(sender_key, limit, max_chars=HISTORY_MAX_CHARS):
    """The last ``limit`` messages of this DM conversation with the bot, read
    back from the stored messages."""
    from app.repository import MessageRepository

    rows = await MessageRepository.get_all(
        limit=min(200, limit * 4 + 10), msg_type="PRIV", conversation_key=sender_key
    )
    return _trimmed(conversation_turns(rows, time.time()), limit, max_chars)


def panel_history(transcript, limit, max_chars=HISTORY_MAX_CHARS):
    """The same, from the Bots › Test tab's own transcript: test runs store no
    messages, so the panel sends its earlier exchanges with each run."""
    from types import SimpleNamespace

    now = time.time()
    rows = [
        SimpleNamespace(
            text=m.get("text", ""), outgoing=bool(m.get("outgoing")), received_at=now, id=n
        )
        for n, m in enumerate(transcript)
    ]
    return _trimmed(conversation_turns(rows, now), limit, max_chars)


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

    if msg.is_dm and msg.arg_text.strip().lower() in RESET_WORDS:
        # The reset message itself is the marker: history stops at it.
        await ctx.reply("🤖 Conversation forgotten; starting fresh.")
        return
    started = time.monotonic()
    try:
        n_ctx = int(ctx.settings.get("context_tokens") or CONTEXT_TOKENS)
    except (TypeError, ValueError):
        n_ctx = CONTEXT_TOKENS
    n_ctx = n_ctx if n_ctx in CONTEXT_CHOICES else CONTEXT_TOKENS
    state = llm_runtime.ensure(
        spec,
        threads=int(_number(ctx, "threads", 0, 0, 32)),
        repack=bool(ctx.settings.get("fast_arm_layout", False)),
        idle_unload_seconds=int(_number(ctx, "unload_after_minutes", 5, 0, 1440) * 60),
        n_ctx=n_ctx,
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
    max_tokens = int(_number(ctx, "max_tokens", 40, 16, 160))
    prompt = system_prompt_for(ctx.settings, spec, prompt_values(msg.sender_name))
    # What the context has room for once the prompt, question and answer are in.
    free_chars = (
        n_ctx * CHARS_PER_TOKEN
        - len(prompt)
        - len(question)
        - max_tokens * CHARS_PER_TOKEN
        - TEMPLATE_OVERHEAD_CHARS
    )
    use_docs = bool(ctx.settings.get("use_docs", True))
    history_room = free_chars // 2 if use_docs else free_chars
    history_room = min(max(0, history_room), HISTORY_MAX_CHARS * n_ctx // CONTEXT_TOKENS)
    history = []
    if msg.is_dm and history_limit and history_room > 0:
        if ctx.is_test and ctx.test_transcript:
            history = panel_history(ctx.test_transcript, history_limit, history_room)
        elif session:
            history = await dm_history(session, history_limit, history_room)
    notes = ""
    if use_docs:
        # The previous question too, so a follow-up ("and how do I set it?")
        # still finds the section the conversation is about.
        earlier = next((m["content"] for m in reversed(history) if m["role"] == "user"), "")
        room = free_chars - sum(len(m["content"]) for m in history)
        notes, titles = await asyncio.to_thread(reference_notes, f"{question} {earlier}", room)
        # Optional second opinion: the keyword gate matches words, not meaning
        # ("what time is it" matches a Clock section). A quick yes/no from the
        # model itself filters those -- only when there are notes to judge,
        # and only with time to spare.
        time_left = RUN_BUDGET_SECONDS - (time.monotonic() - started)
        if (
            notes
            and ctx.settings.get("check_notes_with_model")
            and time_left >= NOTES_CHECK_MIN_SECONDS
        ):
            try:
                verdict = await asyncio.to_thread(
                    llm_runtime.choose,
                    notes_check_messages(question, titles),
                    ("yes", "no"),
                    NOTES_CHECK_SECONDS,
                )
                if verdict != "yes":
                    notes = ""
            except LlmWorkerDiedError as exc:
                await ctx.reply_split(f"🤖 Sorry, {exc}. Try again, or pick a smaller model.")
                return
            except Exception as exc:  # noqa: BLE001 - a failed check keeps the notes
                ctx.log(f"notes check skipped: {exc}", "WARNING")

    def ask_model(earlier, with_notes):
        system = {"role": "system", "content": prompt + (notes if with_notes else "")}
        return asyncio.to_thread(
            llm_runtime.generate,
            [system, *earlier, {"role": "user", "content": question}],
            max_tokens=max_tokens,
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

    # Budgeting is an estimate; if the prompt still overflows the context, drop
    # the history, then the notes, rather than not answering at all.
    attempts = []
    for attempt in ((history, bool(notes)), ([], bool(notes)), ([], False)):
        if attempt not in attempts:
            attempts.append(attempt)
    try:
        for n, (earlier, with_notes) in enumerate(attempts):
            try:
                answer = await ask_model(earlier, with_notes)
                break
            except LlmPromptTooLongError:
                if n == len(attempts) - 1:
                    raise
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
