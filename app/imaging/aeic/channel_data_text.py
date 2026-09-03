"""MCMP text carried over GRP_DATA -- the non-image half of the same envelope.

MCO Advanced does not only put pictures in ``PAYLOAD_TYPE_GRP_DATA`` blobs. Its
``channelsSendAsBinary`` setting is **on by default**, and with it every
MCMP-compressed channel message leaves the phone as a GRP_DATA blob rather than
as ``mcmp2:``/``mcmp3:`` text::

    0xFFF1  legacy: senderNameLen(varuint) senderName  <v2 compressed bytes>
    0x0120  app:    senderNameLen(varuint) senderName subtypeVersion(u8) <body>
            subtype 2 (MCMP), version 0 -> the same v3 container the ``mcmp3:``
            text transport carries, minus the basE91 wrapper

Both bodies are already understood by :mod:`app.compression.mcmp` -- v2 by
:meth:`MeshCompressor.decompress_bytes`, v3 by :func:`decode_v3_body`, which is
exactly what the text transports decode after unwrapping basE91. All that was
missing was the envelope, so a peer's ordinary compressed chat arrived here as
"MCMP text over GRP_DATA (not supported)" and was dropped.

This module sits beside the AEIC channel-data code because the envelope, the
data types and the frame are shared with it; the compression itself is
:mod:`app.compression.mcmp`'s, imported the same way
:mod:`app.imaging.aeic.text_transport` imports basE91 from it. It is
stdlib-only, like every module below ``service.py``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.compression.mcmp import DecodedV3Message, decode_v3_body, get_compressor
from app.imaging.aeic.channel_data import (
    DATA_TYPE_MCMP,
    DATA_TYPE_MCO_APP,
    MCO_APP_SUBTYPE_MCMP,
    parse_envelope,
)

logger = logging.getLogger(__name__)

MCMP_V3_WIRE_VERSION = 0
"""``mcmpV3WireVersion`` in ``channel_app_data_helper.dart``.

The subtype byte packs ``subtype(4) | version(4)``, and MCMP's *wire* version is
0 even though the container is "v3" -- the 3 is the codec generation, not the
nibble. Any other version is a format this build has not seen, and is refused
rather than guessed at.
"""


_ALLOWED_CONTROL_CHARACTERS = frozenset("\n\r\t")

DECODE_HARD_LIMIT = 4096
"""The compressor stops here, so a result of exactly this length was truncated."""


def _is_plausible_message(text: str) -> bool:
    """Whether a decoded body reads as a message rather than as noise.

    The arithmetic decoder is not self-checking: fed bytes that are not one of
    its bitstreams it returns *something* -- four thousand NUL characters, for a
    blob of zeros -- rather than failing. That is fine for a codec whose input is
    always its own output, and not fine here, where the data type is the only
    evidence that this blob is MCMP at all: another application's payload under
    the same legacy type, or a truncated one, would otherwise become a message
    row of control characters. Prose survives this test; noise does not.
    """
    if not text or len(text) >= DECODE_HARD_LIMIT:
        return False
    return not any(ch < " " and ch not in _ALLOWED_CONTROL_CHARACTERS for ch in text)


@dataclass(frozen=True)
class DecodedChannelDataText:
    """A GRP_DATA blob that turned out to be a text message."""

    sender_name: str
    text: str
    version: str
    """``"v2"`` or ``"v3"``, matching :class:`app.compression.DecodedIncoming`."""

    payload_bytes: int
    """Size of the whole GRP_DATA payload, for the compression ratio."""

    v3: DecodedV3Message | None = None


def carries_text(data_type: int, payload: bytes = b"") -> bool:
    """Whether this data type is one this module can turn into a message.

    Cheap and structural: it says what the blob claims to be, not whether the
    body decodes. :func:`decode_channel_data_text` is the one that knows that.
    """
    if data_type == DATA_TYPE_MCMP:
        return True
    if data_type != DATA_TYPE_MCO_APP:
        return False
    envelope = parse_envelope(payload, with_subtype=True)
    return envelope is not None and envelope.subtype == MCO_APP_SUBTYPE_MCMP


def decode_channel_data_text(data_type: int, payload: bytes) -> DecodedChannelDataText | None:
    """Decode one GRP_DATA blob as MCMP text, or None if it is not that.

    Never raises. A body that does not decode -- a truncated blob, a model this
    build could not load, a version nibble from a newer app -- returns None, and
    the caller treats the frame as it did before: named in the log, kept if it
    is media, dropped if it is not.
    """
    if data_type == DATA_TYPE_MCMP:
        envelope = parse_envelope(payload, with_subtype=False)
        if envelope is None:
            return None
        try:
            text = get_compressor().decompress_bytes(envelope.body)
        except Exception as exc:  # noqa: BLE001 - see docstring
            logger.info("Could not decode an MCMP v2 GRP_DATA body: %s", exc)
            return None
        if not _is_plausible_message(text):
            logger.debug("An MCMP v2 GRP_DATA body did not decode to a message; ignoring it")
            return None
        return DecodedChannelDataText(
            sender_name=envelope.sender_name,
            text=text,
            version="v2",
            payload_bytes=len(payload),
        )

    if data_type != DATA_TYPE_MCO_APP:
        return None
    envelope = parse_envelope(payload, with_subtype=True)
    if envelope is None or envelope.subtype != MCO_APP_SUBTYPE_MCMP:
        return None
    if envelope.version != MCMP_V3_WIRE_VERSION:
        logger.info(
            "Ignoring an MCMP GRP_DATA body with wire version %s; this build reads %d",
            envelope.version,
            MCMP_V3_WIRE_VERSION,
        )
        return None
    try:
        decoded = decode_v3_body(get_compressor(), envelope.body)
    except Exception as exc:  # noqa: BLE001 - see docstring
        logger.info("Could not decode an MCMP v3 GRP_DATA body: %s", exc)
        return None
    if not _is_plausible_message(decoded.text):
        logger.debug("An MCMP v3 GRP_DATA body did not decode to a message; ignoring it")
        return None
    return DecodedChannelDataText(
        # The v3 container carries its own sender name for room transports; on a
        # channel the envelope's is the one the app filled in.
        sender_name=decoded.sender_name or envelope.sender_name,
        text=decoded.text,
        version="v3",
        payload_bytes=len(payload),
        v3=decoded,
    )
