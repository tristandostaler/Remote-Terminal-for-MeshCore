"""MeshCore Open Advanced compatible emoji reactions ("remoji").

A reaction rides the mesh as an ordinary text message ``r:HHHH:II`` where
``HHHH`` is a 4-hex-char hash identifying the target message and ``II`` is a
2-hex-char index into a fixed emoji table. Wire format, hash inputs, and the
emoji table are ported from meshcore-open (branch ``rename-mco-advanced``):

- ``lib/helpers/reaction_helper.dart`` -- format, hash inputs, emoji indexing
- ``lib/widgets/emoji_picker.dart``    -- the emoji table (order is the wire
  contract; ``frontend/src/utils/meshcoreOpenPayloads.ts`` mirrors it)
- ``lib/connector/meshcore_connector.dart`` -- sender split, reply cleaning,
  and the direction rules for matching a reaction to its target

The 4-char hash is the low 16 bits of Dart's ``String.hashCode`` over
``"{senderTimestampSecs}{senderName}{first5}"`` (channels and room servers) or
``"{senderTimestampSecs}{first5}"`` (1:1 DMs, sender implicit), where
``first5`` is the first 5 UTF-16 code units of the message body. Dart VM
``String.hashCode`` is the Jenkins one-at-a-time hash from the Dart SDK
(``runtime/vm/hash.h`` CombineHashes/FinalizeHash over UTF-16 code units,
finalized to ``Object::kHashBits`` = 30 bits, with 0 mapped to 1);
``tests/fixtures/reaction_hash_vectors.json`` holds vectors generated from a C
transcription of that reference code.

Reaction messages are stored like any other message (so flood-echo dedup keeps
working) but flagged ``is_reaction`` and hidden from every conversation
surface; their effect lives in the target message's ``reactions`` JSON column.
"""

import json
import logging
import re
import struct
from dataclasses import dataclass

from app.models import CONTACT_TYPE_ROOM, Message

logger = logging.getLogger(__name__)

# How many recent messages of a conversation are scanned for the hash target.
# MCO Advanced scans its in-memory window (500 messages); matching that keeps
# the "reaction to a long-scrolled-away message is lost" behavior aligned.
REACTION_MATCH_SCAN_LIMIT = 500

# Combined reaction emoji table in wire-index order: quick + smileys +
# gestures + hearts + objects, exactly as meshcore-open concatenates them.
# Duplicates are intentional (quick emojis reappear in their category);
# encoding uses the first occurrence, decoding accepts either index.
# fmt: off
QUICK_REACTION_EMOJIS: tuple[str, ...] = ("👍", "❤️", "😂", "🎉", "👏", "🔥")

_SMILEYS = (
    "😀", "😃", "😄", "😁", "😅", "😂", "🤣", "😊", "😇", "🙂",
    "🙃", "😉", "😌", "😍", "🥰", "😘", "😗", "😙", "😚", "😋",
    "😛", "😝", "😜", "🤪", "🤨", "🧐", "🤓", "😎", "🥸", "🤩",
    "🥳", "😏", "😒", "😞", "😔", "😟", "😕", "🙁", "😣", "😖",
    "😫", "😩", "🥺", "😢", "😭", "😤", "😠", "😡", "🤬", "🤯",
    "😳", "🥵", "🥶", "😱", "😨", "😰", "😥", "😓", "🤗", "🤔",
    "🤭", "🤫", "🤥", "😶",
)
_GESTURES = (
    "👍", "👎", "👊", "✊", "🤛", "🤜", "🤞", "✌️", "🤟", "🤘",
    "👌", "🤌", "🤏", "👈", "👉", "👆", "👇", "☝️", "👋", "🤚",
    "🖐️", "✋", "🖖", "👏", "🙌", "👐", "🤲", "🤝", "🙏", "✍️",
    "💅", "🤳", "💪",
)
_HEARTS = (
    "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍", "🤎", "💔",
    "❤️‍🔥", "❤️‍🩹", "💕", "💞", "💓", "💗", "💖", "💘", "💝", "💟",
    "💌", "💢", "💥", "💫", "💦", "💨", "🕳️", "💬", "👁️‍🗨️", "🗨️",
    "🗯️", "💭",
)
_OBJECTS = (
    "🎉", "🎊", "🎈", "🎁", "🎀", "🪅", "🪆", "🏆", "🥇", "🥈",
    "🥉", "⚽", "⚾", "🥎", "🏀", "🏐", "🏈", "🏉", "🎾", "🥏",
    "🎳", "🏏", "🏑", "🏒", "🥍", "🏓", "🏸", "🥊", "🥋", "🥅",
    "⛳", "🔥", "⭐", "🌟", "✨", "⚡", "💡", "🔦", "🏮", "🪔",
    "📱", "💻", "⌚", "📷", "📺", "📻", "🎵", "🎶", "🚀",
)
# fmt: on

REACTION_EMOJIS: tuple[str, ...] = QUICK_REACTION_EMOJIS + _SMILEYS + _GESTURES + _HEARTS + _OBJECTS

_REACTION_RE = re.compile(r"^r:([0-9a-f]{4}):([0-9a-f]{2})$")

# meshcore-open reply wire form: "@[sender] body", optionally with an exact
# quote line ">fragment\n" at the start of the body. MCO Advanced strips both
# before storing the message text, so its reaction hashes are computed over the
# cleaned body -- see clean_channel_body_for_hash().
_REPLY_MENTION_RE = re.compile(r"^@\[[^\]]+\]\s+(.+)$", re.DOTALL)


@dataclass(frozen=True)
class ReactionInfo:
    """A parsed ``r:HHHH:II`` reaction payload."""

    target_hash: str
    emoji: str


def _utf16_code_units(text: str) -> tuple[int, ...]:
    encoded = text.encode("utf-16-le", "surrogatepass")
    return struct.unpack(f"<{len(encoded) // 2}H", encoded)


def dart_string_hash(text: str) -> int:
    """Dart VM ``String.hashCode`` (30-bit Jenkins hash over UTF-16 units)."""
    h = 0
    for unit in _utf16_code_units(text):
        h = (h + unit) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + (h << 15)) & 0xFFFFFFFF
    h &= (1 << 30) - 1
    return h if h != 0 else 1


def compute_reaction_hash(timestamp_secs: int, sender_name: str | None, text: str) -> str:
    """4-hex-char reaction hash for a message, exactly as MCO Advanced computes it.

    ``first5`` is taken in UTF-16 code units (Dart ``substring(0, 5)``), so a
    leading astral-plane emoji is cut mid-surrogate-pair just like Dart does.
    """
    first5 = _utf16_code_units(text)[:5]
    prefix = f"{timestamp_secs}{sender_name}" if sender_name is not None else str(timestamp_secs)
    units = _utf16_code_units(prefix) + first5
    h = 0
    for unit in units:
        h = (h + unit) & 0xFFFFFFFF
        h = (h + (h << 10)) & 0xFFFFFFFF
        h ^= h >> 6
    h = (h + (h << 3)) & 0xFFFFFFFF
    h ^= h >> 11
    h = (h + (h << 15)) & 0xFFFFFFFF
    h &= (1 << 30) - 1
    if h == 0:
        h = 1
    return format(h & 0xFFFF, "04x")


def parse_reaction(text: str) -> ReactionInfo | None:
    """Parse ``r:HHHH:II``, or None when the text is not a valid reaction."""
    match = _REACTION_RE.match(text)
    if not match:
        return None
    index = int(match.group(2), 16)
    if index >= len(REACTION_EMOJIS):
        return None
    return ReactionInfo(target_hash=match.group(1), emoji=REACTION_EMOJIS[index])


def is_reaction_text(text: str) -> bool:
    """Whether ``text`` is a bare reaction payload (no sender prefix)."""
    return parse_reaction(text) is not None


def emoji_to_index_hex(emoji: str) -> str | None:
    """2-hex-char wire index for an emoji (first occurrence), or None."""
    try:
        return format(REACTION_EMOJIS.index(emoji), "02x")
    except ValueError:
        return None


def encode_reaction(target_hash: str, emoji_index_hex: str) -> str:
    return f"r:{target_hash}:{emoji_index_hex}"


def split_channel_sender_text(text: str) -> tuple[str, str]:
    """Split a channel wire text ``"Name: body"`` into (sender, body).

    Port of MCO Advanced ``_splitSenderText``: the sender is everything before
    the first colon when that colon sits within the first 50 UTF-16 code units,
    is not the last character, and the candidate name contains no ``[``/``]``.
    Anything else yields ``("Unknown", text)`` -- including a bare reaction
    body, whose own colon makes it split as sender ``"r"``, so a nameless node
    cannot emit channel reactions (MCO Advanced has the same blind spot).
    """
    idx = text.find(":")
    if 0 < idx < len(text) - 1:
        # The 50-char budget is measured in UTF-16 code units (Dart indexOf).
        utf16_prefix_len = sum(2 if ord(ch) > 0xFFFF else 1 for ch in text[:idx])
        if utf16_prefix_len < 50:
            sender = text[:idx]
            if "[" in sender or "]" in sender:
                return ("Unknown", text)
            offset = idx + 2 if idx + 1 < len(text) and text[idx + 1] == " " else idx + 1
            return (sender, text[offset:])
    return ("Unknown", text)


def clean_channel_body_for_hash(body: str) -> str:
    """Reduce a channel message body to the text MCO Advanced stores (and hashes).

    MCO Advanced strips the reply mention (``@[Name] ``) and, when the body
    then starts with an exact-quote line (``>fragment\\n``), that line too,
    before storing a reply's text -- reaction hashes are computed over the
    cleaned body on its side, so ours must be as well.
    """
    match = _REPLY_MENTION_RE.match(body)
    if not match:
        return body
    actual = match.group(1)
    if actual.startswith(">"):
        newline = actual.find("\n")
        # resolveReply requires a non-empty fragment: newline index > 1.
        if newline > 1:
            return actual[newline + 1 :]
    return actual


def extract_reaction_from_stored_text(msg_type: str, text: str) -> ReactionInfo | None:
    """Reaction carried by a stored message row's text, or None.

    Channel rows store the wire form ``"Name: r:HHHH:II"``; direct rows store
    the bare body. Parsing through the sender split keeps parity with MCO
    Advanced, which never sees a channel reaction that lacks a sender prefix.
    """
    if msg_type == "CHAN":
        _, body = split_channel_sender_text(text)
    else:
        body = text
    return parse_reaction(body)


def _our_radio_name() -> str | None:
    """Radio display name from the connected radio's cached self info."""
    from app.services.radio_runtime import radio_runtime

    try:
        mc = radio_runtime.meshcore
    except Exception:
        return None
    if mc and mc.self_info:
        return mc.self_info.get("name") or None
    return None


def hash_inputs_for_message(
    message: Message, *, is_room: bool, our_name: str | None
) -> tuple[int, str | None, str] | None:
    """(timestamp_secs, sender_name, text) that MCO Advanced would hash for this row.

    Returns None when the row cannot be hashed (no sender timestamp).
    """
    if message.sender_timestamp is None:
        return None
    if message.type == "CHAN":
        sender, body = split_channel_sender_text(message.text)
        return (message.sender_timestamp, sender, clean_channel_body_for_hash(body))
    if is_room:
        sender = our_name if message.outgoing else (message.sender_name or None)
        return (message.sender_timestamp, sender, message.text)
    return (message.sender_timestamp, None, message.text)


def reaction_hash_for_message(
    message: Message, *, is_room: bool, our_name: str | None
) -> str | None:
    inputs = hash_inputs_for_message(message, is_room=is_room, our_name=our_name)
    if inputs is None:
        return None
    return compute_reaction_hash(*inputs)


async def _conversation_is_room(msg_type: str, conversation_key: str) -> bool:
    if msg_type != "PRIV":
        return False
    from app.repository import ContactRepository

    contact = await ContactRepository.get_by_key(conversation_key.lower())
    return contact is not None and contact.type == CONTACT_TYPE_ROOM


async def apply_reaction(
    *,
    msg_type: str,
    conversation_key: str,
    reaction: ReactionInfo,
    reactor_is_self: bool,
    broadcast_fn,
    fallback_target: Message | None = None,
) -> Message | None:
    """Match a reaction to its target message, bump the count, and broadcast.

    Scans the conversation newest-first (like MCO Advanced) applying its
    direction rules: in 1:1 DMs an incoming reaction can only land on our
    outgoing messages and vice versa; channels and room servers match any
    message. Scan-first even for our own sends so a hash collision lands on the
    same (newest) message every client picks. ``fallback_target`` -- the row the
    user actually long-pressed -- catches the one case the scan cannot: a
    target older than the scan window. Returns the updated target, or None when
    nothing matched (the reaction is then lost, exactly as MCO Advanced loses
    it).
    """
    from app.repository import MessageRepository

    is_room = await _conversation_is_room(msg_type, conversation_key)
    our_name = _our_radio_name()

    candidates = await MessageRepository.get_recent_for_reaction_matching(
        msg_type=msg_type,
        conversation_key=conversation_key,
        limit=REACTION_MATCH_SCAN_LIMIT,
    )
    target: Message | None = None
    for candidate in candidates:
        if msg_type == "PRIV" and not is_room:
            # 1:1: you react to what you received, never to your own bubble.
            if reactor_is_self and candidate.outgoing:
                continue
            if not reactor_is_self and not candidate.outgoing:
                continue
        if reaction_hash_for_message(candidate, is_room=is_room, our_name=our_name) == (
            reaction.target_hash
        ):
            target = candidate
            break

    if target is None:
        target = fallback_target
    if target is None:
        logger.info(
            "Reaction %s (hash %s) matched no recent message in %s",
            reaction.emoji,
            reaction.target_hash,
            conversation_key[:12],
        )
        return None

    reactions = await MessageRepository.increment_reaction(target.id, reaction.emoji)
    logger.info(
        "Applied reaction %s to message %d in %s",
        reaction.emoji,
        target.id,
        conversation_key[:12],
    )
    broadcast_fn(
        "message_reaction",
        {
            "message_id": target.id,
            "conversation_key": conversation_key,
            "type": msg_type,
            "reactions": reactions,
        },
    )
    return target.model_copy(update={"reactions": reactions})


def parse_reactions_json(raw: str | None) -> dict[str, int] | None:
    """Decode a message row's ``reactions`` JSON column, tolerating garbage."""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not data:
        return None
    return {str(emoji): int(count) for emoji, count in data.items()}
