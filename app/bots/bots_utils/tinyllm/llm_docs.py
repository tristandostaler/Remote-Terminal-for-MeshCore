"""Reference notes the ``tinyllm`` bot looks up for knowledge questions.

The operator keeps plain markdown files in a folder (``settings.llm_docs_dir``,
``data/tinyllm-docs`` beside the database by default). Every heading starts a
section; long sections are split at paragraphs. For each question the bot runs
a BM25 keyword search over all sections and puts the best matches into the
prompt as reference notes, as many as the model's context has room for.

Keyword search rather than embeddings on purpose: it needs no second model
(no extra RAM on a Pi), and the questions it has to get right -- "what's the
command for X", "how do I set the TX power" -- hinge on exact words like
command and setting names, which is what keyword scoring matches best.

The starter notes shipped in ``library/docs/`` are synced into the folder
the first time the bot runs after each server start: they belong to the repository, so an edit to one
is overwritten and a deleted one comes back. Any other ``.md`` file in the
folder is the operator's and is never touched. The index is rebuilt
whenever a file's size or modification time changes.
"""

from __future__ import annotations

import logging
import math
import re
import shutil
import threading
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SHIPPED_DOCS_DIR = Path(__file__).resolve().parents[2] / "library" / "docs"
# A section longer than this is split at paragraph breaks, so one match does
# not use the whole budget on text that was not the answer.
SECTION_MAX_CHARS = 800
# BM25 parameters (the usual defaults).
_K1 = 1.2
_B = 0.75
# Title words count this many times: a section headed "TX Power" is about TX
# power even if its body says "transmit".
_TITLE_WEIGHT = 3
# Relevance gate. BM25 alone scores any shared word, so "what is the power of
# love?" would pull in the TX Power section. A section only counts when it
# matches at least half of the question's meaningful words and at least two of
# them -- or, for a one-word question, when that word is in its heading or rare
# across the notes -- and
# scores at least half as well as the best match.
MIN_COVERAGE = 0.5
MIN_MATCHED_TERMS = 2
RELATIVE_FLOOR = 0.5
RARE_WORD_SHARE = 0.05
# A best match cut to fit the budget is only sent with at least this much text.
SHORTENED_MIN_CHARS = 120

_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_WORD = re.compile(r"[a-z0-9]+(?:\.[a-z0-9]+)*")
# Words that say nothing about what is being asked. The second group is
# small talk: a greeting is never a lookup, even when "hello" happens to be in
# the notes (it is the default room guest password).
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "its",
        "me",
        "my",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "you",
        "your",
        "please",
        "tell",
        "about",
        "there",
        "get",
        "set",
        "someone",
        "somebody",
        "anyone",
        "anybody",
        "something",
        "during",
        "up",
        "see",
        "use",
        "make",
        "should",
        "could",
        "would",
        "mean",
        "means",
        "meaning",
        # French function words, so a French question is judged on the words
        # that carry its meaning (accents are folded first: "très" -> "tres").
        "le",
        "la",
        "les",
        "un",
        "une",
        "des",
        "de",
        "du",
        "au",
        "aux",
        "et",
        "ou",
        "est",
        "sont",
        "en",
        "dans",
        "sur",
        "pour",
        "par",
        "avec",
        "sans",
        "que",
        "qui",
        "quoi",
        "comment",
        "quel",
        "quelle",
        "quels",
        "quelles",
        "ce",
        "cet",
        "cette",
        "ces",
        "je",
        "tu",
        "il",
        "elle",
        "nous",
        "vous",
        "ils",
        "elles",
        "mon",
        "ma",
        "mes",
        "ton",
        "ta",
        "tes",
        "sa",
        "ses",
        "leur",
        "se",
        "ne",
        "pas",
        "tres",
        "faire",
        "fait",
        "peux",
        "peut",
        "dois",
        "doit",
        "ca",
        "qu",
        "c",
        "d",
        "j",
        "l",
        "n",
        "bonjour",
        "salut",
        "merci",
        # Small talk: "hello" alone must not pull in the one note that happens
        # to mention it.
        "hello",
        "hi",
        "hey",
        "hiya",
        "thanks",
        "thank",
        "thx",
        "ok",
        "okay",
        "yes",
        "yeah",
        "no",
        "nope",
        "bye",
        "cheers",
        "good",
        "morning",
        "afternoon",
        "evening",
        "night",
        "lol",
    ]
)


@dataclass(frozen=True)
class Section:
    title: str
    text: str
    source: str

    def render(self) -> str:
        # The last two heading levels say enough ("Regions > Add a region");
        # the full path repeats a file's top heading in every note the model
        # has to read.
        return f"[{' > '.join(self.title.split(' > ')[-2:])}] {self.text}"


def _fold(word: str) -> str:
    """Crude suffix folding so word forms meet: "lists"/"list",
    "flooding"/"flooded"/"floods"/"flood". Applied to the notes and the question
    alike, so the stems only have to agree, not be real words."""
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed") and not word.endswith("eed"):
        word = word[:-2]
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith("oes"):
        word = word[:-2]  # tomatoes -> tomato
    elif len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    """Lowercase words, plurals folded; dotted names (``flood.advert.interval``)
    also count as their parts, so either form of a setting name matches."""
    out: list[str] = []
    # Accents folded to plain letters: "région" is one word, and meets "region".
    text = unicodedata.normalize("NFKD", text.lower()).encode("ascii", "ignore").decode()
    for word in _WORD.findall(text):
        if "." in word:
            out.extend(_fold(part) for part in word.split(".") if part not in _STOPWORDS)
        if word not in _STOPWORDS:
            out.append(word if "." in word else _fold(word))
    return out


def parse_markdown(text: str, source: str = "") -> list[Section]:
    """Split a markdown file into sections at headings, then at paragraphs."""
    text = _COMMENT.sub("", text)
    sections: list[Section] = []
    # The open headings, (level, title), outermost first.
    path: list[tuple[int, str]] = []
    body: list[str] = []

    def flush() -> None:
        content = "\n".join(body).strip()
        body.clear()
        if not content:
            return
        title = " > ".join(t for _, t in path) or Path(source).stem
        chunk = ""
        for paragraph in re.split(r"\n\s*\n", content):
            paragraph = " ".join(paragraph.split())
            # An oversized paragraph is split at sentences, so no section is
            # much bigger than a small model's notes budget.
            pieces = (
                re.split(r"(?<=[.!?])\s+", paragraph)
                if len(paragraph) > SECTION_MAX_CHARS
                else [paragraph]
            )
            for piece in pieces:
                if chunk and len(chunk) + len(piece) + 1 > SECTION_MAX_CHARS:
                    sections.append(Section(title, chunk, source))
                    chunk = ""
                chunk = f"{chunk} {piece}".strip()
        if chunk:
            sections.append(Section(title, chunk, source))

    for line in text.splitlines():
        match = _HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            # A heading closes every open heading at its level or deeper, so
            # siblings never nest -- whatever level a file starts at.
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, match.group(2)))
        else:
            body.append(line)
    flush()
    return sections


class DocsIndex:
    """BM25 over every section of every ``*.md`` file in one folder."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self._lock = threading.Lock()
        self._signature: tuple = ()
        self._sections: list[Section] = []
        self._docs: list[Counter[str]] = []
        self._titles: list[set[str]] = []
        self._lengths: list[int] = []
        self._df: Counter[str] = Counter()
        self._synonyms: dict[str, set[str]] = {}

    def _files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.rglob("*.md") if p.is_file())

    def _refresh(self) -> None:
        files = self._files()
        synonyms_file = self.folder / SYNONYMS_FILE
        stats = [(p, p.stat()) for p in [*files, synonyms_file] if p.is_file()]
        signature = tuple((str(p), st.st_mtime_ns, st.st_size) for p, st in stats)
        if signature == self._signature:
            return
        sections: list[Section] = []
        for path in files:
            try:
                sections.extend(parse_markdown(path.read_text(errors="replace"), path.name))
            except OSError as exc:
                logger.warning("tinyllm docs: cannot read %s: %s", path, exc)
        docs = [Counter(_tokens(s.title) * _TITLE_WEIGHT + _tokens(s.text)) for s in sections]
        self._sections = sections
        self._docs = docs
        self._titles = [set(_tokens(s.title)) for s in sections]
        self._lengths = [sum(d.values()) for d in docs]
        self._df = Counter(term for d in docs for term in d)
        self._synonyms = load_synonyms(synonyms_file)
        self._signature = signature

    def _relevant(self, i: int, groups: dict[str, set[str]]) -> bool:
        """The coverage part of the relevance gate (see MIN_COVERAGE).

        ``groups`` maps each question word to itself plus its synonyms; a word
        is matched when any of them is in the section."""
        # Words are what the asker typed: a dotted name also contributes its
        # parts, which must not count as extra words matched.
        words = {t for t in groups if "." not in t} or set(groups)
        doc = self._docs[i]
        matched = {t for t in words if any(a in doc for a in groups[t])}
        if len(words) == 1:
            # One word: in the heading, or rare enough to be what the section
            # is about (a distinctive name, not a word every section uses).
            (word,) = words
            found = [a for a in groups[word] if a in doc]
            in_title = any(a in self._titles[i] for a in found)
            rare = any(self._df[a] <= max(2, len(self._docs) * RARE_WORD_SHARE) for a in found)
            return bool(found) and (in_title or rare)
        return len(matched) >= MIN_MATCHED_TERMS and len(matched) >= MIN_COVERAGE * len(words)

    def search(self, query: str, max_chars: int) -> list[Section]:
        """The relevant sections, best first, together at most ``max_chars``
        when rendered. Empty when nothing is relevant enough."""
        terms = set(_tokens(query))
        if not terms or max_chars <= 0:
            return []
        with self._lock:
            self._refresh()
            groups = {t: self._synonyms.get(t, set()) | {t} for t in terms}
            count = len(self._docs)
            if not count:
                return []
            average = sum(self._lengths) / count
            scored: list[tuple[float, int]] = []
            for i, doc in enumerate(self._docs):
                score = 0.0
                norm = _K1 * (1 - _B + _B * self._lengths[i] / average)
                for word, alternatives in groups.items():
                    # A word scores as its best-matching form, so a synonym
                    # can stand in for it but never counts twice -- and with
                    # the asked word's own rarity when the notes use it, so a
                    # rare synonym ("ajouter" for "add") cannot outweigh it.
                    asked_df = self._df[word]
                    best = 0.0
                    for term in alternatives:
                        tf = doc.get(term, 0)
                        if not tf:
                            continue
                        df = asked_df or self._df[term]
                        idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
                        best = max(best, idf * tf * (_K1 + 1) / (tf + norm))
                    score += best
                if score > 0 and self._relevant(i, groups):
                    scored.append((score, i))
            scored.sort(reverse=True)
            picked: list[Section] = []
            used = 0
            floor = scored[0][0] * RELATIVE_FLOOR if scored else 0.0
            for score, i in scored:
                if score < floor:
                    break
                section = self._sections[i]
                size = len(section.render()) + 1
                if used + size > max_chars and not picked:
                    # The best match does not fit a small budget whole: its
                    # start is better than no notes at all.
                    section = _shortened(section, max_chars - 1)
                    size = len(section.render()) + 1 if section else 0
                if not section or used + size > max_chars:
                    continue
                picked.append(section)
                used += size
            return picked


# Words that mean the same thing, one group per line, in the docs folder. The
# operator's file: created once from the shipped starter list, never
# overwritten, and re-read whenever it changes.
SYNONYMS_FILE = "synonyms.txt"


def load_synonyms(path: Path) -> dict[str, set[str]]:
    """``word -> every other word in its groups``, folded like the notes.

    One group per line, words separated by commas (or ``=``); ``#`` starts a
    comment. Only single words count: an entry that is several words or a
    stopword is skipped, because the search matches words, not phrases."""
    synonyms: dict[str, set[str]] = {}
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return synonyms
    for line in text.splitlines():
        line = line.split("#", 1)[0]
        group: set[str] = set()
        for entry in re.split(r"[,=]", line):
            tokens = _tokens(entry)
            if len(tokens) == 1:
                group.add(tokens[0])
        if len(group) < 2:
            continue
        for word in group:
            synonyms.setdefault(word, set()).update(group - {word})
    return synonyms


# Questions that found no notes, so the operator knows what to write next.
# Kept in the bot's state; this file in the docs folder is just their view.
MISSED_FILE = "missed-questions.txt"
MISSED_MAX = 200


def record_missed(missed: dict, question: str, now: float) -> bool:
    """Count ``question`` in ``missed`` (``text -> [count, last_seen]``).

    False when there is nothing to learn from it: small talk, or a question
    with no searchable words."""
    if not _tokens(question):
        return False
    text = " ".join(question.lower().split()).strip(" ?!.")[:120]
    count, _ = missed.get(text, (0, 0))
    missed[text] = [count + 1, int(now)]
    if len(missed) > MISSED_MAX:
        # Drop the least asked, oldest first.
        for key, _ in sorted(missed.items(), key=lambda kv: (kv[1][0], kv[1][1]))[
            : len(missed) - MISSED_MAX
        ]:
            del missed[key]
    return True


def top_missed(missed: dict, limit: int | None = None) -> list[tuple[str, int, int]]:
    """``(question, count, last_seen)``, most asked first, then most recent."""
    rows = sorted(
        ((q, int(v[0]), int(v[1])) for q, v in missed.items()), key=lambda r: (-r[1], -r[2])
    )
    return rows[:limit] if limit else rows


def write_missed_file(folder: Path, missed: dict) -> None:
    lines = [
        "# Questions the tinyllm bot was asked that found no reference notes,",
        "# most asked first. Write a .md note that answers them (use the words",
        "# people asked with), or add those words to synonyms.txt. Rewritten by",
        "# the bot; DM it 'ask missed clear' (admins) to start over.",
        "",
    ]
    for question, count, last_seen in top_missed(missed):
        day = time.strftime("%Y-%m-%d", time.localtime(last_seen))
        lines.append(f"{count}x  {question}  (last {day})")
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / MISSED_FILE).write_text("\n".join(lines) + "\n")
    except OSError as exc:
        logger.warning("tinyllm docs: cannot write %s: %s", MISSED_FILE, exc)


def _shortened(section: Section, max_chars: int) -> Section | None:
    """``section`` cut at a word so its rendering fits ``max_chars``, or None
    when too little of it would be left to be worth sending."""
    room = max_chars - len(section.render()) + len(section.text) - 1
    if room < SHORTENED_MIN_CHARS:
        return None
    text = section.text[:room].rsplit(" ", 1)[0] + "…"
    return Section(section.title, text, section.source)


# Lists the shipped files currently in a docs folder, one name per line, so a
# file dropped from a later release is removed there too, while files the
# operator added are never touched.
SHIPPED_MANIFEST = ".shipped"


def seed_docs(folder: Path) -> None:
    """Sync the shipped starter notes into the docs folder.

    Shipped files belong to the repository: each one is (re)written whenever
    it differs from the shipped copy, so an update always lands and a deleted
    one comes back, and one no longer shipped is removed. Every other file in
    the folder is the operator's and is left alone.
    """
    manifest = folder / SHIPPED_MANIFEST
    try:
        folder.mkdir(parents=True, exist_ok=True)
        previous = set(manifest.read_text().split()) if manifest.exists() else set()
        shipped = {p.name: p for p in SHIPPED_DOCS_DIR.glob("*.md")}
        for name, source in shipped.items():
            target = folder / name
            content = source.read_bytes()
            # Unchanged files are not rewritten, so their mtime -- and the
            # search index built from it -- stays put.
            if not target.is_file() or target.read_bytes() != content:
                target.write_bytes(content)
        for name in previous - shipped.keys():
            (folder / name).unlink(missing_ok=True)
        # The synonyms list is the operator's to edit: only ever created.
        starter = SHIPPED_DOCS_DIR / SYNONYMS_FILE
        if starter.is_file() and not (folder / SYNONYMS_FILE).exists():
            shutil.copyfile(starter, folder / SYNONYMS_FILE)
        manifest.write_text("".join(f"{name}\n" for name in sorted(shipped)))
    except OSError as exc:
        logger.warning("tinyllm docs: cannot seed %s: %s", folder, exc)


# Written by the tinyllm bot from this node's bots, not shipped: seeding
# never touches it, and it is rewritten whenever the bots change.
BOTS_PAGE = "this-node-bots.md"


def render_bots_page(bots: list[dict]) -> str:
    """Notes about the bots this node advertises, one section per bot.

    ``bots`` is ``ctx.get_enabled_bots()``: enabled and not private. Headings
    carry the name and one-liner, so "how do I get the weather?" finds the
    weather bot.
    """
    out = [
        "<!--",
        "Written by the tinyllm bot from this node's enabled bots and rewritten",
        "whenever they change: do not edit. Private bots are left out.",
        "-->",
        "",
        "# This node's bots",
        "",
        "## Which bots and commands this node has",
        "",
        "This node runs RemoteTerm. Its bots answer commands sent in a bot channel",
        "(#bot or #bots) or by direct message; a command is the first word of the",
        "message, for example: help. Send help for the list of commands, and help",
        "followed by a command for details on one.",
    ]
    if bots:
        out.append("Bots here: " + ", ".join(b["name"] for b in bots) + ".")
    for b in sorted(bots, key=lambda b: str(b["name"]).lower()):
        keywords = list(dict.fromkeys(b.get("keywords") or []))
        facts = [f"Category: {b.get('category') or 'Custom'}."]
        facts.append(
            f"Commands: {', '.join(keywords)}."
            if keywords
            else "It has no command of its own: it acts on its own triggers."
        )
        if b.get("admin_only"):
            facts.append("Only the node's admins can use it.")
        detail = " ".join(str(b.get("long_description") or "").replace("`", "").split())
        heading = (
            f"{b['name']} bot: {b['description']}" if b.get("description") else f"{b['name']} bot"
        )
        out += ["", f"## {' '.join(heading.split())}", "", " ".join([*facts, detail]).strip()]
    return "\n".join(out) + "\n"


def write_bots_page(folder: Path, bots: list[dict]) -> None:
    """Write :func:`render_bots_page` into the docs folder when it changed."""
    target = folder / BOTS_PAGE
    content = render_bots_page(bots)
    try:
        if not target.is_file() or target.read_text() != content:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
    except OSError as exc:
        logger.warning("tinyllm docs: cannot write %s: %s", target, exc)


_indexes: dict[Path, DocsIndex] = {}
_indexes_lock = threading.Lock()


def docs_index(folder: Path | None = None) -> DocsIndex:
    """The process-wide index for ``folder`` (default: the configured one),
    seeding the folder on first use."""
    if folder is None:
        from app.config import settings

        folder = Path(settings.llm_docs_dir)
    with _indexes_lock:
        index = _indexes.get(folder)
        if index is None:
            seed_docs(folder)
            index = _indexes[folder] = DocsIndex(folder)
        return index
