"""Python port of MeshCore Open Advanced's MCOtxt v1 text codec.

MCOtxt packs chat text so more of it fits in one LoRa packet, like MCMP, but
with a very different engine: instead of an arithmetic coder over a large
n-gram model it predicts the next letter from a frozen TOP-4 table per
language (a few hundred bytes each, small enough to run on the node's own
microcontroller) and spends a variable-length token on every character::

    00 / 010 / 0110 / 0111   the 1st..4th predicted letter    (2-4 bits)
    10 + id                  a letter the table did not guess  (7 bits)
    110 + id                 punctuation                       (8 bits)
    ...                      case, language switches, raw UTF-8 runs

Roughly 5.2-5.5 bits per character on ordinary chat, against 8 (Latin) or 16
(Cyrillic) for UTF-8. Seven languages ship (EN RU FR DE IT UK BE) and one
message may switch between them.

This module is a faithful port of HDDen/meshcore-open (MIT):

    lib/MCOtxt/mcotxt_codec.dart        -> the bitstream: header, tokens, planner
    lib/MCOtxt/mcotxt_frame.dart        -> the length-prefixed frame
    lib/helpers/mcotxt_app_codec.dart   -> the app container and ``mct:`` transport

and ``docs/MCOTXT_V1_PROTOCOL.md`` in that repository is the wire spec. The
language tables in ``models/mcotxt-v1.json`` were exported from the Dart
registry and are checked against the frozen SHA-256 wire hashes of the
upstream manifest every time they load, so a corrupted or hand-edited table
refuses to load rather than silently producing streams no other client reads.

Layers, outermost first::

    transport   "mct:" + basE91( 0x31 || container )      (text transport)
    container   flags | [timestamp] | [senderName] | [reply] | text
    string      mode byte (0 = MCOtxt frame, 1 = UTF-8)
    frame       varuint(bitLength) | ceil(bitLength / 8) bytes
    stream      version | generation | language header | tokens

The binary GROUP_DATA transport carries the same container behind the MCO
Advanced envelope; :mod:`app.imaging.aeic.channel_data_text` unwraps it and
calls :func:`decode_container`.

Unlike MCMP's arithmetic decoder, this one is strict and self-checking: a
malformed stream is rejected (:class:`MCOtxtError`), never decoded to noise.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from dataclasses import dataclass

from .mcmp import _b91_decode_v3 as _b91_decode
from .mcmp import encode_base91

logger = logging.getLogger(__name__)

PREFIX = "mct:"
"""Text transport prefix (channels, direct messages, room posts)."""

CODEC_VERSION = 1
SUBTYPE_ID = 0x03
"""MCO Advanced application subtype for MCOtxt."""
WIRE_REVISION = 0x01
SUBTYPE_VERSION = (SUBTYPE_ID << 4) | WIRE_REVISION
"""The packed ``0x31`` byte that opens every MCOtxt app payload."""

_MODEL_PATH = os.path.join(os.path.dirname(__file__), "models", "mcotxt-v1.json")

# --- header / token constants (mcotxt_codec.dart) -----------------------------

_HEADER_FIELD_BITS = 3
_HEADER_FIELD_ESCAPE = 7
_HEADER_FIELD_MAX = _HEADER_FIELD_ESCAPE + 0xFF
_LANGUAGE_BITS = 3
_NORMAL_HEADER_BITS = 12
_RAW_UTF8_PADDING_BITS = 4
_LANGUAGE_NONE_WIRE_ID = 7
_EXTENDED_HEADER_WIRE_ID = 7
_EXTENDED_PAIR8_FORMAT = 0
_RAW_UTF8_FORMAT = 1
_GLOBAL_LANGUAGE_NONE = 255

_EXT_PREFIX = 63  # 111111
_EXT_PREFIX_BITS = 6
_EXT_SUBOPCODE_BITS = 3
_SUB_SWITCH_OTHER_LANGUAGE = 0
_SUB_RESET_CONTEXT = 1
_SUB_UTF8_RUN = 2
_SUB_TOGGLE_CASE_MODE = 3
_UTF8_RUN_LENGTH_BITS = 5
_UTF8_RUN_MAX_BYTES = 32

_BITS_SHIFT = 5
_BITS_TOGGLE_LANGUAGE = 6
_BITS_SWITCH_OTHER = _EXT_PREFIX_BITS + _EXT_SUBOPCODE_BITS + 8  # 17
_BITS_TOGGLE_CASE = _EXT_PREFIX_BITS + _EXT_SUBOPCODE_BITS  # 9
_BITS_UTF8_RUN_OVERHEAD = _EXT_PREFIX_BITS + _EXT_SUBOPCODE_BITS + _UTF8_RUN_LENGTH_BITS  # 14
_TOP4_BITS = (2, 3, 4, 4)

# Shared punctuation page, fixed for v1 (punctuation.dart). Digits are absent
# on purpose: every language table carries them as symbols.
_PUNCTUATION = (
    0x20, 0x2E, 0x2C, 0x21, 0x3F, 0x3A, 0x3B, 0x2D,
    0x2014, 0x5F, 0x27, 0x22, 0xAB, 0xBB, 0x201C, 0x201D,
    0x201E, 0x2018, 0x2019, 0x28, 0x29, 0x5B, 0x5D, 0x2F,
    0x5C, 0x40, 0x23, 0x25, 0x26, 0x2B, 0x3D, 0x0A,
)  # fmt: skip
_PUNCTUATION_ID = {cp: i for i, cp in enumerate(_PUNCTUATION)}
_SPACE = 0x20
_LINE_FEED = 0x0A

# Prediction contexts. A context is a tuple: (_CTX_START, None),
# (_CTX_AFTER_PUNCT, None) or (_CTX_SYMBOL, previous_lowercase_symbol).
_CTX_START = 0
_CTX_AFTER_PUNCT = 1
_CTX_SYMBOL = 2
_START = (_CTX_START, None)
_AFTER_PUNCT = (_CTX_AFTER_PUNCT, None)

# Token kinds for the planner's output.
_T_TOP4 = 0
_T_PRIMARY = 1
_T_PUNCT = 2
_T_EXTENSION = 3
_T_SHIFT = 4
_T_TOGGLE_LANGUAGE = 5
_T_SWITCH_OTHER = 6
_T_TOGGLE_CASE = 7
_T_UTF8_RUN = 8

# Partial NFC applied before encoding (mcotxt_model_registry.dart): only these
# base + combining-mark pairs are composed, nothing else is touched.
_NFC_PAIRS = {
    (0x61, 0x300): 0xE0, (0x61, 0x302): 0xE2, (0x41, 0x300): 0xC0, (0x41, 0x302): 0xC2,
    (0x63, 0x327): 0xE7, (0x43, 0x327): 0xC7,
    (0x65, 0x300): 0xE8, (0x65, 0x301): 0xE9, (0x65, 0x302): 0xEA, (0x65, 0x308): 0xEB,
    (0x45, 0x300): 0xC8, (0x45, 0x301): 0xC9, (0x45, 0x302): 0xCA, (0x45, 0x308): 0xCB,
    (0x69, 0x300): 0xEC, (0x69, 0x302): 0xEE, (0x69, 0x308): 0xEF,
    (0x49, 0x300): 0xCC, (0x49, 0x302): 0xCE, (0x49, 0x308): 0xCF,
    (0x6F, 0x302): 0xF4, (0x6F, 0x308): 0xF6, (0x4F, 0x302): 0xD4, (0x4F, 0x308): 0xD6,
    (0x75, 0x300): 0xF9, (0x75, 0x302): 0xFB, (0x75, 0x308): 0xFC,
    (0x55, 0x300): 0xD9, (0x55, 0x302): 0xDB, (0x55, 0x308): 0xDC,
    (0x435, 0x308): 0x451, (0x415, 0x308): 0x401, (0x438, 0x306): 0x439, (0x418, 0x306): 0x419,
    (0x456, 0x308): 0x457, (0x406, 0x308): 0x407, (0x443, 0x306): 0x45E, (0x423, 0x306): 0x40E,
}  # fmt: skip


class MCOtxtError(Exception):
    """A stream, frame or container that is not valid MCOtxt v1."""


# --- language models ----------------------------------------------------------


class _Model:
    """One frozen language table (``MCOtxtLanguageModel``)."""

    __slots__ = (
        "language",
        "global_id",
        "primary",
        "extension",
        "start_top4",
        "punct_start_top4",
        "top4",
        "upper_to_lower",
        "lower_to_upper",
        "primary_id",
        "extension_id",
    )

    def __init__(self, raw: dict) -> None:
        symbols = list(raw["primary"]) + list(raw["extension"])
        self.language: str = raw["language"]
        self.global_id: int = raw["languageId"]
        self.primary: tuple[int, ...] = tuple(raw["primary"])
        self.extension: tuple[int, ...] = tuple(raw["extension"])
        self.start_top4 = tuple(symbols[i] for i in raw["startTop4"])
        self.punct_start_top4 = tuple(symbols[i] for i in raw["punctStartTop4"])
        flat = raw["top4"]
        self.top4: dict[int, tuple[int, ...]] = {
            sym: tuple(symbols[j] for j in flat[i * 4 : i * 4 + 4]) for i, sym in enumerate(symbols)
        }
        self.upper_to_lower = {upper: symbols[i] for upper, i in raw["uppercase"]}
        self.lower_to_upper = {lower: upper for upper, lower in self.upper_to_lower.items()}
        self.primary_id = {sym: i for i, sym in enumerate(self.primary)}
        self.extension_id = {sym: i for i, sym in enumerate(self.extension)}

    def normalize(self, cp: int) -> int | None:
        """The table's lowercase symbol for ``cp``, or None if it has none."""
        if cp in self.top4:
            return cp
        return self.upper_to_lower.get(cp)

    def predictions(self, context: tuple[int, int | None]) -> tuple[int, ...]:
        kind, previous = context
        if kind == _CTX_START:
            return self.start_top4
        if kind == _CTX_AFTER_PUNCT:
            return self.punct_start_top4
        return self.top4.get(previous, ())  # type: ignore[arg-type]


def _wire_hash(raw: dict) -> str:
    """SHA-256 of the canonical table identity (``model_wire_hash`` upstream)."""
    identity = {
        "codecVersion": CODEC_VERSION,
        "language": raw["language"],
        "languageId": raw["languageId"],
        "primarySymbols": raw["primary"],
        "extensionSymbols": raw["extension"],
        "startTop4Indexes": raw["startTop4"],
        "punctStartTop4Indexes": raw["punctStartTop4"],
        "top4Indexes": raw["top4"],
        "uppercaseMap": [
            {"uppercaseCodepoint": upper, "lowercaseSymbolIndex": i}
            for upper, i in raw["uppercase"]
        ],
    }
    canonical = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _ModelSet:
    """All tables of one model generation, in registry order (EN first)."""

    def __init__(self, raw: dict) -> None:
        if raw.get("codecVersion") != CODEC_VERSION:
            raise MCOtxtError("model file is for another MCOtxt codec version")
        self.generation: int = raw["generation"]
        models = []
        for entry in raw["models"]:
            if _wire_hash(entry) != entry["wireHash"]:
                raise MCOtxtError(f"MCOtxt {entry['language']} table does not match its wire hash")
            models.append(_Model(entry))
        models.sort(key=lambda m: m.global_id)
        self.models: tuple[_Model, ...] = tuple(models)
        self.by_id = {m.global_id: m for m in models}


_model_set: _ModelSet | None = None
_model_lock = threading.Lock()


def _models() -> _ModelSet:
    global _model_set
    if _model_set is None:
        with _model_lock:
            if _model_set is None:
                with open(_MODEL_PATH, encoding="utf-8") as fh:
                    _model_set = _ModelSet(json.load(fh))
    return _model_set


def normalize_text(text: str) -> str:
    """The input normalisation the encoder applies (and the decoder returns).

    CRLF and lone CR become LF, and a fixed set of base + combining-mark pairs
    are composed. The result is what a sender should treat as the text sent.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    i = 0
    while i < len(text):
        if i + 1 < len(text):
            composed = _NFC_PAIRS.get((ord(text[i]), ord(text[i + 1])))
            if composed is not None:
                out.append(chr(composed))
                i += 2
                continue
        out.append(text[i])
        i += 1
    return "".join(out)


# --- bit IO -------------------------------------------------------------------


class _BitWriter:
    __slots__ = ("_bytes", "_current", "_offset")

    def __init__(self) -> None:
        self._bytes = bytearray()
        self._current = 0
        self._offset = 0

    @property
    def bit_length(self) -> int:
        return len(self._bytes) * 8 + self._offset

    def write(self, value: int, count: int) -> None:
        for shift in range(count - 1, -1, -1):
            self._current |= ((value >> shift) & 1) << (7 - self._offset)
            self._offset += 1
            if self._offset == 8:
                self._bytes.append(self._current)
                self._current = 0
                self._offset = 0

    def to_bytes(self) -> bytes:
        if self._offset:
            return bytes(self._bytes) + bytes([self._current])
        return bytes(self._bytes)


class _BitReader:
    __slots__ = ("_data", "_bit_length", "_index")

    def __init__(self, data: bytes, bit_length: int) -> None:
        if bit_length < 0 or bit_length > len(data) * 8:
            raise MCOtxtError("invalid MCOtxt bit length")
        self._data = data
        self._bit_length = bit_length
        self._index = 0

    @property
    def remaining(self) -> int:
        return self._bit_length - self._index

    def read(self, count: int) -> int:
        if self._index + count > self._bit_length:
            raise MCOtxtError("unexpected end of MCOtxt bitstream")
        value = 0
        for _ in range(count):
            byte = self._data[self._index >> 3]
            value = (value << 1) | ((byte >> (7 - (self._index & 7))) & 1)
            self._index += 1
        return value


def _write_header_field(writer: _BitWriter, value: int) -> None:
    if value < 0 or value > _HEADER_FIELD_MAX:
        raise MCOtxtError(f"MCOtxt header field {value} is out of range")
    if value < _HEADER_FIELD_ESCAPE:
        writer.write(value, _HEADER_FIELD_BITS)
    else:
        writer.write(_HEADER_FIELD_ESCAPE, _HEADER_FIELD_BITS)
        writer.write(value - _HEADER_FIELD_ESCAPE, 8)


def _read_header_field(reader: _BitReader) -> int:
    inline = reader.read(_HEADER_FIELD_BITS)
    if inline != _HEADER_FIELD_ESCAPE:
        return inline
    return _HEADER_FIELD_ESCAPE + reader.read(8)


def _strict_utf8(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MCOtxtError(f"invalid UTF-8 in MCOtxt stream: {exc}") from exc


# --- decoder ------------------------------------------------------------------


def decode_stream(data: bytes, bit_length: int) -> str:
    """Decode one MCOtxt v1 stream of exactly ``bit_length`` bits.

    Strict: every condition the reference rejects raises :class:`MCOtxtError`
    (unknown version or generation, a language without tables, out-of-range
    table ids, SHIFT misuse, invalid UTF-8, a stream ending inside a token).
    """
    models = _models()
    reader = _BitReader(data, bit_length)
    if _read_header_field(reader) != CODEC_VERSION:
        raise MCOtxtError("unsupported MCOtxt codec version")
    generation = _read_header_field(reader)

    lang_a_field = reader.read(_LANGUAGE_BITS)
    lang_b_field = reader.read(_LANGUAGE_BITS)
    if lang_a_field == _EXTENDED_HEADER_WIRE_ID:
        if lang_b_field == _RAW_UTF8_FORMAT:
            # RAW_UTF8 uses no tables, so any generation decodes.
            if reader.read(_RAW_UTF8_PADDING_BITS) != 0:
                raise MCOtxtError("MCOtxt RAW_UTF8 padding bits must be zero")
            if reader.remaining % 8:
                raise MCOtxtError("MCOtxt RAW_UTF8 payload must be byte-aligned")
            return _strict_utf8(bytes(reader.read(8) for _ in range(reader.remaining // 8)))
        if lang_b_field != _EXTENDED_PAIR8_FORMAT:
            raise MCOtxtError("unsupported MCOtxt extended header")
        lang_a = reader.read(8)
        lang_b_global = reader.read(8)
        if lang_a == _GLOBAL_LANGUAGE_NONE or lang_a > 6:
            raise MCOtxtError("unknown MCOtxt language A")
        if lang_b_global != _GLOBAL_LANGUAGE_NONE and lang_b_global > 6:
            raise MCOtxtError("unknown MCOtxt language B")
        lang_b = None if lang_b_global == _GLOBAL_LANGUAGE_NONE else lang_b_global
    else:
        lang_a = lang_a_field
        lang_b = None if lang_b_field == _LANGUAGE_NONE_WIRE_ID else lang_b_field

    if generation != models.generation:
        raise MCOtxtError(f"MCOtxt model generation {generation} is not available")
    if lang_a not in models.by_id or (lang_b is not None and lang_b not in models.by_id):
        raise MCOtxtError("MCOtxt stream needs a language table this build lacks")

    current = lang_a
    context: tuple[int, int | None] = _START
    shift = False
    caps = False
    out: list[str] = []

    def emit(model: _Model, symbol: int) -> None:
        nonlocal context, shift
        upper = model.lower_to_upper.get(symbol)
        if shift and upper is None:
            raise MCOtxtError("MCOtxt SHIFT before a symbol without case")
        out.append(chr(upper if upper is not None and caps != shift else symbol))
        context = (_CTX_SYMBOL, symbol)
        shift = False

    def no_pending_shift() -> None:
        if shift:
            raise MCOtxtError("MCOtxt SHIFT must be followed by a language symbol")

    while reader.remaining > 0:
        model = models.by_id[current]
        if reader.read(1) == 0:  # TOP4
            if reader.read(1) == 0:
                rank = 0
            elif reader.read(1) == 0:
                rank = 1
            else:
                rank = 2 if reader.read(1) == 0 else 3
            row = model.predictions(context)
            if rank >= len(row):
                raise MCOtxtError("invalid MCOtxt TOP4 reference")
            emit(model, row[rank])
            continue
        if reader.read(1) == 0:  # PRIMARY
            index = reader.read(5)
            if index >= len(model.primary):
                raise MCOtxtError("invalid MCOtxt primary literal")
            emit(model, model.primary[index])
            continue
        if reader.read(1) == 0:  # PUNCTUATION
            no_pending_shift()
            cp = _PUNCTUATION[reader.read(5)]
            out.append(chr(cp))
            context = _context_after_punctuation(cp, context)
            continue
        if reader.read(1) == 0:  # EXTENSION
            index = reader.read(5)
            if index >= len(model.extension):
                raise MCOtxtError("invalid MCOtxt extension literal")
            emit(model, model.extension[index])
            continue
        if reader.read(1) == 0:  # SHIFT
            if shift:
                raise MCOtxtError("duplicate MCOtxt SHIFT")
            shift = True
            continue
        if reader.read(1) == 0:  # TOGGLE_LANGUAGE
            no_pending_shift()
            if lang_b is None:
                raise MCOtxtError("MCOtxt TOGGLE_LANGUAGE without language B")
            if current == lang_a:
                current = lang_b
            elif current == lang_b:
                current = lang_a
            else:
                raise MCOtxtError("MCOtxt TOGGLE_LANGUAGE outside the A/B pair")
            context = _START
            continue
        no_pending_shift()
        sub = reader.read(_EXT_SUBOPCODE_BITS)
        if sub == _SUB_SWITCH_OTHER_LANGUAGE:
            target = reader.read(8)
            if target == _GLOBAL_LANGUAGE_NONE or target > 6:
                raise MCOtxtError("invalid MCOtxt SWITCH_OTHER_LANGUAGE")
            if target not in models.by_id:
                raise MCOtxtError("MCOtxt switch to a language table this build lacks")
            current = target
            context = _START
        elif sub == _SUB_RESET_CONTEXT:
            context = _START
        elif sub == _SUB_UTF8_RUN:
            length = reader.read(_UTF8_RUN_LENGTH_BITS) + 1
            out.append(_strict_utf8(bytes(reader.read(8) for _ in range(length))))
            context = _START
        elif sub == _SUB_TOGGLE_CASE_MODE:
            caps = not caps
        else:
            raise MCOtxtError("unknown MCOtxt extended control")

    if shift:
        raise MCOtxtError("MCOtxt stream ends after SHIFT")
    return "".join(out)


def _context_after_punctuation(cp: int, context: tuple[int, int | None]) -> tuple[int, int | None]:
    """SPACE keeps a symbol context, LF restarts, anything else is AFTER_PUNCT."""
    if cp == _SPACE:
        return context if context[0] == _CTX_SYMBOL else _START
    if cp == _LINE_FEED:
        return _START
    return _AFTER_PUNCT


# --- encoder ------------------------------------------------------------------


def _case_plan(cps: list[int], models: _ModelSet) -> tuple[set[int], set[int]]:
    """Where to TOGGLE_CASE_MODE (before) and where to SHIFT, over the text.

    Case never changes the lowercase symbol predictions run on, so it is solved
    once with a two-state search (caps off / on) instead of multiplying the
    language/context search -- exactly as the reference does.
    """
    positions: list[int] = []
    wants_upper: list[bool] = []
    for pos, cp in enumerate(cps):
        for model in models.models:
            normalized = model.normalize(cp)
            if normalized is None or normalized not in model.lower_to_upper:
                continue
            positions.append(pos)
            wants_upper.append(normalized != cp)
            break
    if not positions:
        return set(), set()

    # Cost per resulting caps state: (bits, toggles, shifts).
    previous: list[tuple[int, int, int] | None] = [(0, 0, 0), None]
    backtrack: list[list[tuple[int, bool, bool] | None]] = []
    for desired_upper in wants_upper:
        nxt: list[tuple[int, int, int] | None] = [None, None]
        decisions: list[tuple[int, bool, bool] | None] = [None, None]
        for prev_state in (0, 1):
            prev_cost = previous[prev_state]
            if prev_cost is None:
                continue
            for state in (0, 1):
                toggled = prev_state != state
                shifted = desired_upper != (state == 1)
                cost = (
                    prev_cost[0] + (_BITS_TOGGLE_CASE if toggled else 0) + (5 if shifted else 0),
                    prev_cost[1] + toggled,
                    prev_cost[2] + shifted,
                )
                if nxt[state] is None or cost < nxt[state]:  # type: ignore[operator]
                    nxt[state] = cost
                    decisions[state] = (prev_state, toggled, shifted)
        previous = nxt
        backtrack.append(decisions)

    # State 0 only when strictly cheaper: the reference keeps caps on a full tie.
    state = 0 if previous[1] is None or previous[0] < previous[1] else 1  # type: ignore[operator]
    toggles: set[int] = set()
    shifts: set[int] = set()
    for i in range(len(positions) - 1, -1, -1):
        prev_state, toggled, shifted = backtrack[i][state]  # type: ignore[misc]
        if toggled:
            toggles.add(positions[i])
        if shifted:
            shifts.add(positions[i])
        state = prev_state
    return toggles, shifts


def _symbol_option(
    model: _Model, cp: int, context: tuple[int, int | None], shift: bool
) -> tuple[list[tuple[int, int]], int, int, bool] | None:
    """Tokens, bits and lowercase symbol for ``cp`` in ``model``, or None."""
    normalized = model.normalize(cp)
    if normalized is None:
        return None
    if shift and normalized not in model.lower_to_upper:
        return None
    prefix = [(_T_SHIFT, 0)] if shift else []
    shift_bits = _BITS_SHIFT if shift else 0
    row = model.predictions(context)
    if normalized in row:
        rank = row.index(normalized)
        return [*prefix, (_T_TOP4, rank)], _TOP4_BITS[rank] + shift_bits, normalized, True
    if normalized in model.primary_id:
        return (
            [*prefix, (_T_PRIMARY, model.primary_id[normalized])],
            7 + shift_bits,
            normalized,
            False,
        )
    if normalized in model.extension_id:
        return (
            [*prefix, (_T_EXTENSION, model.extension_id[normalized])],
            9 + shift_bits,
            normalized,
            False,
        )
    return None


# A plan's cost, compared lexicographically like ``_MCOtxtPlan.compare``
# (every complete plan encodes every character, so that leading term drops):
# (bits, tokens, language switches, -TOP4 hits).
_Cost = tuple[int, int, int, int]


def _plan(
    cps: list[int], lang_a: int, lang_b: int | None, models: _ModelSet
) -> tuple[_Cost, list[tuple]]:
    """Cheapest token sequence for ``cps`` with the declared pair (A, B).

    The reference's memoised dynamic programme over positions x (active
    language, prediction context), evaluated iteratively: a forward pass finds
    the reachable states, a backward pass prices them. Candidate order and the
    tie-break match the Dart planner, so equal-cost choices resolve the same way.
    """
    n = len(cps)
    toggle_before, shift_at = _case_plan(cps, models)
    supported = [
        cp in _PUNCTUATION_ID or any(m.normalize(cp) is not None for m in models.models)
        for cp in cps
    ]

    def utf8_run(pos: int) -> tuple[bytes, int]:
        data = bytearray()
        count = 0
        for i in range(pos, n):
            if supported[i]:
                break
            encoded = chr(cps[i]).encode("utf-8")
            if data and len(data) + len(encoded) > _UTF8_RUN_MAX_BYTES:
                break
            data += encoded
            count += 1
            if len(data) == _UTF8_RUN_MAX_BYTES:
                break
        if count == 0:
            data += chr(cps[pos]).encode("utf-8")
            count = 1
        return bytes(data), count

    def toggled(lang: int) -> int | None:
        if lang_b is None:
            return None
        if lang == lang_a:
            return lang_b
        if lang == lang_b:
            return lang_a
        return None

    def candidates(pos: int, lang: int, context: tuple[int, int | None]):
        """Yield (tokens, bits, switches, top4, next_state) in reference order."""
        cp = cps[pos]
        punct = _PUNCTUATION_ID.get(cp)
        if punct is not None:
            yield (
                [(_T_PUNCT, punct)],
                8,
                0,
                0,
                (pos + 1, lang, _context_after_punctuation(cp, context)),
            )
        other = toggled(lang)
        targets: list[tuple[int, tuple, list, int, int]] = [(lang, context, [], 0, 0)]
        if other is not None:
            targets.append((other, _START, [(_T_TOGGLE_LANGUAGE, 0)], _BITS_TOGGLE_LANGUAGE, 1))
        for model in models.models:
            if model.global_id in (lang, other):
                continue
            targets.append(
                (
                    model.global_id,
                    _START,
                    [(_T_SWITCH_OTHER, model.global_id)],
                    _BITS_SWITCH_OTHER,
                    1,
                )
            )
        found = punct is not None
        for target, symbol_context, prefix, prefix_bits, switches in targets:
            option = _symbol_option(models.by_id[target], cp, symbol_context, pos in shift_at)
            if option is None:
                continue
            found = True
            tokens, bits, symbol, hit = option
            if pos in toggle_before:
                tokens = [(_T_TOGGLE_CASE, 0), *tokens]
                bits += _BITS_TOGGLE_CASE
            yield (
                [*prefix, *tokens],
                bits + prefix_bits,
                switches,
                1 if hit else 0,
                (pos + 1, target, (_CTX_SYMBOL, symbol)),
            )
        if not found:
            data, count = utf8_run(pos)
            yield (
                [(_T_UTF8_RUN, data)],
                _BITS_UTF8_RUN_OVERHEAD + 8 * len(data),
                0,
                0,
                (pos + count, lang, _START),
            )

    # Forward pass: every state reachable from the start, grouped by position.
    start: tuple[int, int, tuple[int, int | None]] = (0, lang_a, _START)
    by_position: list[list[tuple]] = [[] for _ in range(n + 1)]
    edges: dict[tuple, list] = {}
    seen: set[tuple[int, int, tuple[int, int | None]]] = {start}
    by_position[0].append(start)
    for pos in range(n):
        for state in by_position[pos]:
            outgoing = list(candidates(*state))
            edges[state] = outgoing
            for *_rest, nxt in outgoing:
                if nxt not in seen:
                    seen.add(nxt)
                    by_position[nxt[0]].append(nxt)

    # Backward pass: best cost from each state to the end.
    best: dict[tuple, tuple[_Cost, list | None, tuple | None]] = {}
    for state in by_position[n]:
        best[state] = ((0, 0, 0, 0), None, None)
    for pos in range(n - 1, -1, -1):
        for state in by_position[pos]:
            chosen: tuple[_Cost, list | None, tuple | None] | None = None
            for tokens, bits, switches, top4, nxt in edges[state]:
                tail = best[nxt][0]
                cost = (tail[0] + bits, tail[1] + len(tokens), tail[2] + switches, tail[3] - top4)
                if chosen is None or cost < chosen[0]:
                    chosen = (cost, tokens, nxt)
            best[state] = chosen  # type: ignore[assignment]

    tokens_out: list[tuple] = []
    state: tuple | None = start
    while state is not None and state[0] < n:
        _cost, tokens, nxt = best[state]
        tokens_out.extend(tokens)  # type: ignore[arg-type]
        state = nxt
    return best[start][0] if n else (0, 0, 0, 0), tokens_out


def _write_token(writer: _BitWriter, token: tuple) -> None:
    kind, value = token
    if kind == _T_TOP4:
        writer.write((0, 2, 6, 7)[value], _TOP4_BITS[value])
    elif kind == _T_PRIMARY:
        writer.write(0b10, 2)
        writer.write(value, 5)
    elif kind == _T_PUNCT:
        writer.write(0b110, 3)
        writer.write(value, 5)
    elif kind == _T_EXTENSION:
        writer.write(0b1110, 4)
        writer.write(value, 5)
    elif kind == _T_SHIFT:
        writer.write(0b11110, 5)
    elif kind == _T_TOGGLE_LANGUAGE:
        writer.write(0b111110, 6)
    elif kind == _T_SWITCH_OTHER:
        writer.write(_EXT_PREFIX, _EXT_PREFIX_BITS)
        writer.write(_SUB_SWITCH_OTHER_LANGUAGE, _EXT_SUBOPCODE_BITS)
        writer.write(value, 8)
    elif kind == _T_TOGGLE_CASE:
        writer.write(_EXT_PREFIX, _EXT_PREFIX_BITS)
        writer.write(_SUB_TOGGLE_CASE_MODE, _EXT_SUBOPCODE_BITS)
    elif kind == _T_UTF8_RUN:
        writer.write(_EXT_PREFIX, _EXT_PREFIX_BITS)
        writer.write(_SUB_UTF8_RUN, _EXT_SUBOPCODE_BITS)
        writer.write(len(value) - 1, _UTF8_RUN_LENGTH_BITS)
        for byte in value:
            writer.write(byte, 8)


def _raw_utf8_stream(normalized: str, generation: int) -> tuple[bytes, int]:
    writer = _BitWriter()
    _write_header_field(writer, CODEC_VERSION)
    _write_header_field(writer, generation)
    writer.write(_EXTENDED_HEADER_WIRE_ID, _LANGUAGE_BITS)
    writer.write(_RAW_UTF8_FORMAT, _LANGUAGE_BITS)
    writer.write(0, _RAW_UTF8_PADDING_BITS)
    for byte in normalized.encode("utf-8"):
        writer.write(byte, 8)
    return writer.to_bytes(), writer.bit_length


_EN = 0
_RU = 1


def _default_pair(cps: list[int], models: _ModelSet) -> tuple[int, int | None]:
    """The language pair to declare, chosen from the letters the text uses.

    The reference app does not search pairs either: it declares the UI
    language and EN (EN and RU for an English UI), because a full search costs
    a planning pass per pair. A server has no UI language, so A is the table
    that knows the most of the text's letters -- ties go to the lower id, EN
    then RU -- and B follows the same rule as the app's ``forLocale``. Every
    other language stays reachable mid-message through SWITCH_OTHER_LANGUAGE,
    so this choice costs a few bits at most, never correctness.
    """
    letters = [cp for cp in cps if chr(cp).isalpha()]
    best_id, best_count = _EN, -1
    for model in models.models:
        count = sum(1 for cp in letters if model.normalize(cp) is not None)
        if count > best_count:
            best_id, best_count = model.global_id, count
    other = _RU if best_id == _EN else _EN
    return best_id, other if other in models.by_id else None


def encode_stream(
    text: str,
    *,
    languages: tuple[int, int | None] | None = None,
    search_all_pairs: bool = False,
) -> tuple[bytes, int]:
    """Encode ``text`` to an MCOtxt v1 stream; returns ``(data, bit_length)``.

    With ``languages`` (A, B) the pair is used as given. Otherwise the pair
    comes from :func:`_default_pair`, or, with ``search_all_pairs``, every
    available A is tried with every other language or none as B and the
    cheapest kept, with the reference's tie-break (fewest bits, then fewest
    language switches, then fewest declared languages, then lowest ids) --
    the reference's own behaviour without a default pair, and ~50x slower.
    The result is then priced against the codec's own RAW_UTF8 mode, which
    wins only when strictly smaller, so the stream is never much bigger than
    the text.
    """
    models = _models()
    normalized = normalize_text(text)
    cps = [ord(ch) for ch in normalized]

    if languages is not None:
        pairs = [languages]
    elif search_all_pairs:
        ids = [m.global_id for m in models.models]
        pairs = [(a, b) for a in ids for b in [*ids, None] if b != a]
    else:
        pairs = [_default_pair(cps, models)]

    best_key = None
    best_pair: tuple[int, int | None] = pairs[0]
    best_tokens: list[tuple] = []
    for lang_a, lang_b in pairs:
        cost, tokens = _plan(cps, lang_a, lang_b, models)
        key = (
            _NORMAL_HEADER_BITS + cost[0],
            cost[2],
            1 if lang_b is None else 2,
            lang_a,
            _GLOBAL_LANGUAGE_NONE if lang_b is None else lang_b,
        )
        if best_key is None or key < best_key:
            best_key, best_pair, best_tokens = key, (lang_a, lang_b), tokens

    writer = _BitWriter()
    _write_header_field(writer, CODEC_VERSION)
    _write_header_field(writer, models.generation)
    writer.write(best_pair[0], _LANGUAGE_BITS)
    writer.write(_LANGUAGE_NONE_WIRE_ID if best_pair[1] is None else best_pair[1], _LANGUAGE_BITS)
    for token in best_tokens:
        _write_token(writer, token)
    data, bit_length = writer.to_bytes(), writer.bit_length

    raw_data, raw_bits = _raw_utf8_stream(normalized, models.generation)
    if (len(raw_data), raw_bits) < (len(data), bit_length):
        return raw_data, raw_bits
    # The planner is a port; a stream that would not read back is never sent.
    if decode_stream(data, bit_length) != normalized:
        logger.warning("MCOtxt planner produced a stream that does not round-trip; sending raw")
        return raw_data, raw_bits
    return data, bit_length


# --- frame --------------------------------------------------------------------


def _varuint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


class _ByteReader:
    __slots__ = ("data", "offset")

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def byte(self) -> int:
        if self.offset >= len(self.data):
            raise MCOtxtError("truncated MCOtxt container")
        value = self.data[self.offset]
        self.offset += 1
        return value

    def take(self, length: int) -> bytes:
        if length < 0 or self.offset + length > len(self.data):
            raise MCOtxtError("truncated MCOtxt container")
        chunk = self.data[self.offset : self.offset + length]
        self.offset += length
        return chunk

    def uint32(self) -> int:
        return int.from_bytes(self.take(4), "little")

    def varuint(self) -> int:
        result = 0
        for shift in range(0, 35, 7):
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
        raise MCOtxtError("MCOtxt varuint is too long")


def encode_frame(text: str) -> bytes:
    """``varuint(bitLength)`` followed by the stream bytes."""
    data, bit_length = encode_stream(text)
    return _varuint(bit_length) + data


# --- application container ----------------------------------------------------

_FLAG_REPLY = 0x01
_FLAG_SENDER_NAME = 0x02
_FLAG_TIMESTAMP_INHERITED = 0x04
_KNOWN_FLAGS = _FLAG_REPLY | _FLAG_SENDER_NAME | _FLAG_TIMESTAMP_INHERITED
_STRING_MCOTXT = 0x00
_STRING_UTF8 = 0x01


@dataclass(frozen=True)
class DecodedMCOtxtMessage:
    """A decoded MCOtxt app container."""

    text: str
    timestamp: int | None
    """The container's own timestamp; None when it inherits the packet's."""
    sender_name: str | None
    """Only set when the container carries one (room posts, GROUP_DATA)."""
    reply_author_name: str | None
    reply_timestamp: int | None
    text_payload_bytes: int
    """Stream bytes of the text string, frame header excluded: the ratio basis."""


def _write_name(out: bytearray, name: str) -> None:
    """A metadata string in whichever mode is shorter, MCOtxt on a tie."""
    normalized = normalize_text(name)
    raw = normalized.encode("utf-8")
    utf8_form = bytes([_STRING_UTF8]) + _varuint(len(raw)) + raw
    try:
        mcotxt_form = bytes([_STRING_MCOTXT]) + encode_frame(normalized)
    except MCOtxtError:
        out += utf8_form
        return
    out += mcotxt_form if len(mcotxt_form) <= len(utf8_form) else utf8_form


def encode_container(
    text: str,
    *,
    timestamp: int | None = None,
    sender_name: str | None = None,
    reply_author_name: str | None = None,
    reply_timestamp: int | None = None,
) -> bytes:
    """Build the app container. ``timestamp=None`` means "inherit the packet's"."""
    if (reply_author_name is None) != (reply_timestamp is None):
        raise ValueError("reply_author_name and reply_timestamp go together")
    flags = (
        (_FLAG_REPLY if reply_author_name is not None else 0)
        | (_FLAG_SENDER_NAME if sender_name is not None else 0)
        | (_FLAG_TIMESTAMP_INHERITED if timestamp is None else 0)
    )
    out = bytearray([flags])
    if timestamp is not None:
        out += (timestamp & 0xFFFFFFFF).to_bytes(4, "little")
    if sender_name is not None:
        _write_name(out, sender_name)
    if reply_author_name is not None and reply_timestamp is not None:
        _write_name(out, reply_author_name)
        out += (reply_timestamp & 0xFFFFFFFF).to_bytes(4, "little")
    # The text always rides as an MCOtxt frame; the codec's own RAW_UTF8 mode
    # already covers text that does not compress.
    out += bytes([_STRING_MCOTXT]) + encode_frame(text)
    return bytes(out)


def _read_string(reader: _ByteReader) -> tuple[str, int]:
    """Read one string field; returns (text, payload bytes)."""
    mode = reader.byte()
    if mode == _STRING_MCOTXT:
        bit_length = reader.varuint()
        payload = reader.take((bit_length + 7) >> 3)
        return decode_stream(payload, bit_length), len(payload)
    if mode == _STRING_UTF8:
        payload = reader.take(reader.varuint())
        return _strict_utf8(payload), len(payload)
    raise MCOtxtError(f"unsupported MCOtxt string mode {mode}")


def decode_container(body: bytes) -> DecodedMCOtxtMessage:
    """Decode an app container, rejecting unknown flags and trailing bytes."""
    reader = _ByteReader(body)
    flags = reader.byte()
    if flags & ~_KNOWN_FLAGS:
        raise MCOtxtError("unsupported MCOtxt container flags")
    timestamp = None if flags & _FLAG_TIMESTAMP_INHERITED else reader.uint32()
    sender_name = _read_string(reader)[0] if flags & _FLAG_SENDER_NAME else None
    reply_author = reply_ts = None
    if flags & _FLAG_REPLY:
        reply_author = _read_string(reader)[0]
        reply_ts = reader.uint32()
    text, payload_bytes = _read_string(reader)
    if reader.offset != len(body):
        raise MCOtxtError("trailing bytes after the MCOtxt text")
    return DecodedMCOtxtMessage(
        text=text,
        timestamp=timestamp,
        sender_name=sender_name,
        reply_author_name=reply_author,
        reply_timestamp=reply_ts,
        text_payload_bytes=payload_bytes,
    )


# --- text transport -----------------------------------------------------------


def is_text_payload(text: str) -> bool:
    """Whether ``text`` claims to be an ``mct:`` payload (prefix + something)."""
    stripped = text.lstrip()
    return stripped.startswith(PREFIX) and len(stripped) > len(PREFIX)


def encode_text(text: str) -> str:
    """Wrap ``text`` in the ``mct:`` transport, the way MCO Advanced sends chat.

    The container inherits the packet timestamp (flag ``0x04``) and carries no
    sender name: on a channel the name stays in the outer ``Name: text`` layer,
    and a direct message needs none. Empty text and text that already is an
    ``mct:`` payload come back unchanged, as in the reference.
    """
    if not text or is_text_payload(text):
        return text
    body = encode_container(text)
    return PREFIX + encode_base91(bytes([SUBTYPE_VERSION]) + body)


def _text_body(text: str) -> bytes:
    payload = _b91_decode(text.lstrip()[len(PREFIX) :])
    if not payload or payload[0] >> 4 != SUBTYPE_ID:
        raise MCOtxtError("not an MCOtxt app payload")
    if payload[0] & 0x0F != WIRE_REVISION:
        raise MCOtxtError(f"unsupported MCOtxt container revision {payload[0] & 0x0F}")
    return payload[1:]


def try_decode_text(text: str) -> DecodedMCOtxtMessage | None:
    """Decode an ``mct:`` body, or None if it is not one this build reads.

    Never raises. A body that only looks like MCOtxt (bad basE91, another
    revision, a malformed container or stream) returns None and the caller
    keeps the raw text, so the message stays visible.
    """
    if not is_text_payload(text):
        return None
    try:
        return decode_container(_text_body(text))
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.debug("Could not decode an mct: payload: %s", exc)
        return None


def text_payload_bytes(text: str) -> int | None:
    """Stream bytes of the text string inside an ``mct:`` payload (ratio basis)."""
    decoded = try_decode_text(text)
    return decoded.text_payload_bytes if decoded is not None else None
