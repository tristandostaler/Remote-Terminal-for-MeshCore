"""Text compression for MeshCore messages: MCMP and MCOtxt.

See :mod:`app.compression.mcmp` for MCMP and :mod:`app.compression.mcotxt` for
MCOtxt, MCO Advanced's table-driven codec. The common entry points:

    from app.compression import get_compressor, try_decode_incoming

``try_decode_incoming(text)`` decodes an inbound ``mcmp2:``/``mcmp3:``/``mct:``
body to plain text (or returns ``None`` for anything else). ``get_compressor()`` returns
the process-wide compressor for encoding outbound text.

:mod:`app.compression.metadata` turns a (plaintext, wire payload) pair into the
codec + ratio the conversation view shows -- see :func:`describe_compression`.
"""

from .mcmp import (
    TRANSPORT_MCOTXT,
    DecodedIncoming,
    DecodedV3Message,
    MeshCompressor,
    MeshCompressorError,
    decode_base91,
    decode_incoming_body,
    encode_base91,
    encode_outbound,
    encode_v3_text,
    get_compressor,
    is_framed_payload,
    is_v3_text_payload,
    try_decode_incoming,
    try_decode_v3_text,
    v3_compressed_text_bytes,
)
from .metadata import (
    CODEC_MCMP_V2,
    CODEC_MCMP_V3,
    CODEC_MCOTXT,
    CompressionInfo,
    decode_and_describe,
    describe_compression,
)

__all__ = [
    "CODEC_MCMP_V2",
    "CODEC_MCMP_V3",
    "CODEC_MCOTXT",
    "CompressionInfo",
    "DecodedIncoming",
    "DecodedV3Message",
    "MeshCompressor",
    "MeshCompressorError",
    "TRANSPORT_MCOTXT",
    "decode_and_describe",
    "decode_base91",
    "decode_incoming_body",
    "describe_compression",
    "encode_base91",
    "encode_outbound",
    "encode_v3_text",
    "get_compressor",
    "is_framed_payload",
    "is_v3_text_payload",
    "try_decode_incoming",
    "try_decode_v3_text",
    "v3_compressed_text_bytes",
]
