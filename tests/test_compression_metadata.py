"""Per-message compression facts: codec, byte counts and the displayed ratio."""

import pytest

from app.compression import (
    CODEC_MCMP_V2,
    CODEC_MCMP_V3,
    CompressionInfo,
    decode_and_describe,
    describe_compression,
    encode_outbound,
)
from app.compression.mcmp import v3_compressed_text_bytes

# Long and prose-like enough that v2's "only if smaller" gate actually fires.
COMPRESSIBLE = "Hey, are you at the repeater site? I can hear you but the path looks long."


class TestDescribeCompression:
    def test_returns_none_for_plain_text(self):
        assert describe_compression(plain_text="hi", wire_text="hi") is None

    def test_returns_none_for_a_non_mcmp_payload(self):
        """An AEIC chunk or IE4 envelope is framed, not compressed."""
        assert describe_compression(plain_text="hi", wire_text="aei1:AAAA") is None

    def test_returns_none_for_an_empty_wire_payload(self):
        assert describe_compression(plain_text="hi", wire_text="") is None

    def test_describes_a_v2_payload(self):
        wire = encode_outbound(COMPRESSIBLE, version=2, timestamp=0)
        assert wire != COMPRESSIBLE

        info = describe_compression(plain_text=COMPRESSIBLE, wire_text=wire)

        assert info is not None
        assert info.codec == CODEC_MCMP_V2
        assert info.plain_bytes == len(COMPRESSIBLE.encode("utf-8"))
        assert info.wire_bytes == len(wire.encode("utf-8"))
        # v2 has no container, so the ratio is measured over the whole payload.
        assert info.payload_bytes == info.wire_bytes
        assert 0 < info.savings_percent < 100

    def test_describes_a_v3_payload_over_the_text_segment_only(self):
        """The v3 container's header must not be counted against the ratio.

        meshcore-open reports the ratio over the compressed text alone; matching
        it keeps the two clients quoting the same percentage for one message.
        """
        wire = encode_outbound(COMPRESSIBLE, version=3, timestamp=1_700_000_000)

        info = describe_compression(plain_text=COMPRESSIBLE, wire_text=wire)

        assert info is not None
        assert info.codec == CODEC_MCMP_V3
        assert info.payload_bytes == v3_compressed_text_bytes(wire)
        # The header is real airtime even though the ratio excludes it.
        assert info.payload_bytes < info.wire_bytes

    def test_v3_wire_bytes_stay_the_true_on_air_size(self):
        """The container can make v3 larger on air than v2 for the same text.

        Storing both counts is what lets the UI quote the meshcore-open ratio
        without misrepresenting how much airtime the message actually used.
        """
        v2 = describe_compression(
            plain_text=COMPRESSIBLE,
            wire_text=encode_outbound(COMPRESSIBLE, version=2, timestamp=0),
        )
        v3 = describe_compression(
            plain_text=COMPRESSIBLE,
            wire_text=encode_outbound(COMPRESSIBLE, version=3, timestamp=1_700_000_000),
        )

        assert v2 is not None and v3 is not None
        assert v3.wire_bytes > v2.wire_bytes
        assert v3.payload_bytes < v2.payload_bytes


class TestSavingsPercent:
    @pytest.mark.parametrize(
        ("plain", "payload", "expected"),
        [
            (100, 47, 53),
            (100, 100, 0),
            (100, 0, 100),
            # Growth is reported as no saving rather than a negative percentage.
            (100, 180, 0),
            # A zero-length plaintext has no ratio to speak of.
            (0, 10, 0),
        ],
    )
    def test_clamps_to_zero_through_one_hundred(self, plain, payload, expected):
        info = CompressionInfo(
            codec=CODEC_MCMP_V2, plain_bytes=plain, wire_bytes=payload, payload_bytes=payload
        )
        assert info.savings_percent == expected


class TestDecodeAndDescribe:
    def test_passes_plain_text_through_untouched(self):
        text, info = decode_and_describe("just a message")
        assert text == "just a message"
        assert info is None

    def test_passes_a_malformed_mcmp_body_through_untouched(self):
        """A body that only looks like MCMP is stored as-is, with no facts claimed."""
        text, info = decode_and_describe("mcmp2:not-actually-base91-!!!")
        assert text == "mcmp2:not-actually-base91-!!!"
        assert info is None

    @pytest.mark.parametrize("version", [2, 3])
    def test_round_trips_symmetrically_with_the_send_path(self, version):
        """A sent and a received copy of one message must report identical facts."""
        wire = encode_outbound(COMPRESSIBLE, version=version, timestamp=1_700_000_000)
        sent = describe_compression(plain_text=COMPRESSIBLE, wire_text=wire)

        text, received = decode_and_describe(wire)

        assert text == COMPRESSIBLE
        assert received == sent
