from hashlib import sha256

PUBLIC_CHANNEL_KEY = "8B3387E9C5CDEA6AC9E5EDBAA115CD72"
PUBLIC_CHANNEL_NAME = "Public"

# The channels a bot listens to out of the box. Bots answer commands, and a
# command bot let loose on Public (or on every channel a node happens to carry)
# is noise for everyone else on the mesh, so a fresh bot is scoped to the two
# conventional bot channels plus DMs and the operator widens it from there.
BOT_CHANNEL_NAMES = ("#bot", "#bots")


def is_public_channel_key(key: str) -> bool:
    return key.upper() == PUBLIC_CHANNEL_KEY


def is_public_channel_name(name: str) -> bool:
    return name.casefold() == PUBLIC_CHANNEL_NAME.casefold()


def hashtag_channel_key(name: str) -> str:
    """The channel key a hashtag name derives to, as uppercase hex.

    The name is hashed **verbatim, including the leading '#'** (matching
    meshcore_py / meshcore-cli / meshcore.js), which is what makes hashtag
    channels the same key on every node — and what lets the default bot scope
    below name channels this node has not joined yet.
    """
    return sha256(name.encode("utf-8")).digest()[:16].hex().upper()


# Derived, never hardcoded: the same helper channel creation uses, so the
# default scope can never drift from the key a joined "#bot" actually gets.
BOT_CHANNEL_KEYS = tuple(hashtag_channel_key(name) for name in BOT_CHANNEL_NAMES)


def is_bot_channel_key(key: str) -> bool:
    return key.upper() in BOT_CHANNEL_KEYS
