"""Per-message compression facts, for the conversation view's meta line.

One symmetric entry point, :func:`describe_compression`, is used by both directions: the
send path passes the plaintext it was given and the payload it put on air, and
the ingest path passes the decoded plaintext and the payload it received. Either
way the answer is the same shape, so a sent and a received message report their
codec and ratio identically.

The ratio deliberately matches meshcore-open's (``lib/models/message_compression
.dart``): measured over the compressed-text segment, which for v3 excludes the
container header. Without that, container overhead would mask the real saving and
the two clients would disagree about the same message.
"""

from dataclasses import dataclass

from .mcmp import is_v3_text_payload, try_decode_incoming, v3_compressed_text_bytes

CODEC_MCMP_V2 = "mcmp2"
CODEC_MCMP_V3 = "mcmp3"

_PREFIX_V2 = "mcmp2:"


@dataclass(frozen=True)
class CompressionInfo:
    """What a message's compression cost and bought."""

    codec: str
    plain_bytes: int
    """UTF-8 length of the plaintext body (no channel ``sender: `` prefix)."""
    wire_bytes: int
    """UTF-8 length of the payload actually transmitted, container and all."""
    payload_bytes: int
    """Compressed-text segment the ratio is measured against."""

    @property
    def savings_percent(self) -> int:
        """Percent saved versus the plaintext, clamped to 0-100."""
        if self.plain_bytes <= 0:
            return 0
        saved = (self.plain_bytes - self.payload_bytes) * 100 / self.plain_bytes
        return max(0, min(100, round(saved)))


def describe_compression(*, plain_text: str, wire_text: str) -> CompressionInfo | None:
    """Describe the compression that turned ``plain_text`` into ``wire_text``.

    Returns ``None`` when nothing was compressed -- the two are identical (v2's
    "only if smaller" gate declined, or the conversation has MCMP off), or the
    payload is not an MCMP body at all. Callers store ``None`` as "rode as plain
    text", which the meta line renders by simply omitting the codec badge.
    """
    if not wire_text or wire_text == plain_text:
        return None

    stripped = wire_text.lstrip()
    plain_bytes = len(plain_text.encode("utf-8"))
    wire_bytes = len(wire_text.encode("utf-8"))

    if is_v3_text_payload(wire_text):
        segment_bytes = v3_compressed_text_bytes(wire_text)
        if segment_bytes is None:
            return None
        return CompressionInfo(
            codec=CODEC_MCMP_V3,
            plain_bytes=plain_bytes,
            wire_bytes=wire_bytes,
            payload_bytes=segment_bytes,
        )

    if stripped.startswith(_PREFIX_V2) and len(stripped) > len(_PREFIX_V2):
        # v2 has no container: the whole payload, prefix included, is what the
        # ratio is measured against -- same as meshcore-open.
        return CompressionInfo(
            codec=CODEC_MCMP_V2,
            plain_bytes=plain_bytes,
            wire_bytes=wire_bytes,
            payload_bytes=wire_bytes,
        )

    return None


def decode_and_describe(text: str) -> tuple[str, CompressionInfo | None]:
    """Decode an inbound body and report the compression it arrived under.

    The inbound counterpart to :func:`describe_compression`: one call yields both
    the plaintext to store and the facts to store alongside it, so ingest routes
    cannot record one without the other. Non-MCMP bodies come back unchanged with
    ``None``, exactly as :func:`app.compression.decode_incoming_body` behaves.
    """
    decoded = try_decode_incoming(text)
    if decoded is None:
        return text, None
    return decoded.text, describe_compression(plain_text=decoded.text, wire_text=text)
