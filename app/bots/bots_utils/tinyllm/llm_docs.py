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
import threading
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
        return f"[{self.title}] {self.text}"


def _fold(word: str) -> str:
    """Crude suffix folding so word forms meet: "lists"/"list",
    "flooding"/"flooded"/"floods"/"flood". Applied to the notes and the question
    alike, so the stems only have to agree, not be real words."""
    if len(word) > 5 and word.endswith("ing"):
        word = word[:-3]
    elif len(word) > 4 and word.endswith("ed") and not word.endswith("eed"):
        word = word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        word = word[:-1]
    return word


def _tokens(text: str) -> list[str]:
    """Lowercase words, plurals folded; dotted names (``flood.advert.interval``)
    also count as their parts, so either form of a setting name matches."""
    out: list[str] = []
    for word in _WORD.findall(text.lower()):
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
            if chunk and len(chunk) + len(paragraph) + 1 > SECTION_MAX_CHARS:
                sections.append(Section(title, chunk, source))
                chunk = ""
            chunk = f"{chunk} {paragraph}".strip()
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

    def _files(self) -> list[Path]:
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.rglob("*.md") if p.is_file())

    def _refresh(self) -> None:
        files = self._files()
        signature = tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in files)
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
        self._signature = signature

    def _relevant(self, i: int, terms: set[str]) -> bool:
        """The coverage part of the relevance gate (see MIN_COVERAGE)."""
        # Words are what the asker typed: a dotted name also contributes its
        # parts, which must not count as extra words matched.
        words = {t for t in terms if "." not in t} or terms
        matched = {t for t in words if t in self._docs[i]}
        if len(words) == 1:
            # One word: in the heading, or rare enough to be what the section
            # is about (a distinctive name, not a word every section uses).
            (word,) = words
            rare = self._df[word] <= max(2, len(self._docs) * RARE_WORD_SHARE)
            return bool(matched) and (word in self._titles[i] or rare)
        return len(matched) >= MIN_MATCHED_TERMS and len(matched) >= MIN_COVERAGE * len(words)

    def search(self, query: str, max_chars: int) -> list[Section]:
        """The relevant sections, best first, together at most ``max_chars``
        when rendered. Empty when nothing is relevant enough."""
        terms = set(_tokens(query))
        if not terms or max_chars <= 0:
            return []
        with self._lock:
            self._refresh()
            count = len(self._docs)
            if not count:
                return []
            average = sum(self._lengths) / count
            scored: list[tuple[float, int]] = []
            for i, doc in enumerate(self._docs):
                score = 0.0
                for term in terms:
                    tf = doc.get(term, 0)
                    if not tf:
                        continue
                    df = self._df[term]
                    idf = math.log(1 + (count - df + 0.5) / (df + 0.5))
                    norm = _K1 * (1 - _B + _B * self._lengths[i] / average)
                    score += idf * tf * (_K1 + 1) / (tf + norm)
                if score > 0 and self._relevant(i, terms):
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
                if used + size > max_chars:
                    continue
                picked.append(section)
                used += size
            return picked


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
        manifest.write_text("".join(f"{name}\n" for name in sorted(shipped)))
    except OSError as exc:
        logger.warning("tinyllm docs: cannot seed %s: %s", folder, exc)


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
