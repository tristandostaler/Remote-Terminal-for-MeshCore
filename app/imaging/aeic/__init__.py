"""AEIC-SE: the neural image codec, ported from meshcore-open's MCO Advanced fork.

A 512x512 colour photo becomes a 117-209 byte rANS bitstream that the receiver
turns back into a picture by running an ONNX synthesis network. basE91-framed as
``aei1:`` text it is one or two ordinary MeshCore messages -- against the IE4
path's envelope plus 15-40 raw fragments for a 256x256 *greyscale* image.

It is generative, not pixel-faithful: ~17-20 dB PSNR. The receiver gets a
recognisably-the-same scene, not the same pixels. That is the trade the codec
exists to make.

Layer map, bottom up:

``tables``          the CDF table file parser                      (stdlib only)
``rans``            the rANS coder, byte-identical to the C++ ref   (stdlib only)
``entropy``         masks, squeeze, build_indexes, the 4-stage loop (numpy)
``onnx_backend``    ORT sessions and tensor marshalling            (numpy + ORT)
``bundle``          the 958 MiB model registry and its download
``png``             a minimal PNG writer for decoded output         (stdlib only)
``text_transport``  the ``aei1:`` basE91 message framing            (stdlib only)
``service``         process-wide orchestration; what routers call

The bottom two layers are stdlib-only on purpose, so the wire format stays
testable on an install that never opted into the ``aeic`` extra.

**Read ``AGENTS_aeic.md`` before changing anything below ``service``.** This
codec fails silently: the rANS coder is synchronous with the entropy model, so a
single wrong symbol position or one ULP of float drift yields a sharp, plausible,
WRONG image with no error raised anywhere.
"""

from app.imaging.aeic.service import AeicService, AeicUnavailable, aeic_service
from app.imaging.aeic.text_transport import (
    AeicChunk,
    AeicStreamMetadata,
    is_aeic_chunk,
    parse_chunk,
    reassemble,
)

__all__ = [
    "AeicChunk",
    "AeicService",
    "AeicStreamMetadata",
    "AeicUnavailable",
    "aeic_service",
    "is_aeic_chunk",
    "parse_chunk",
    "reassemble",
]
