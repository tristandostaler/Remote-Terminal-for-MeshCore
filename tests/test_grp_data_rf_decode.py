"""Decoding GRP_DATA (binary channel data) straight off raw RF.

The golden vector below is a real packet: an AEIC photo sent from MCO Advanced
to the hashtag channel #bots, captured by the RF log of a radio whose firmware
never delivered it as frame 27. It is what proved the RF path both necessary and
safe -- the packet decrypts with the key derived from the channel *name*,
MAC-verified, and parses as exactly what the sending app reported ("123 bytes,
1 packet", resized to 512x512).

Plaintext layout, from the firmware source (``BaseChatMesh.cpp``,
``sendGroupData`` / ``onGroupDataRecv``): data_type (2, LE), data_len (1),
data, zero-padded to the AES block. No timestamp.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.channel_constants import hashtag_channel_key
from app.decoder import decrypt_group_data, try_decrypt_group_data_packet
from app.imaging.aeic.channel_data import (
    DATA_TYPE_AEIC_IMAGE,
    parse_chunk_blob,
)
from app.imaging.aeic.text_transport import AeicStreamMetadata

# The full 152-byte RF packet: header 0x19 (flood, GroupData), path len 0x81
# (1 hop, 3-byte hashes), path E8B3AD, then channel_hash + MAC(2) + ciphertext.
GOLDEN_PACKET = bytes.fromhex(
    "1981"
    "E8B3AD"
    "44AD25C0DF6FB0C7CA48A376E1331C3EBBFEC6FCEBA915D404D8486FB80D9FC6EA72092591"
    "687164F9B7464BA565118E039EFC00613A045E76EE929D20DA9FE61897B19C5FD8C0CD1085"
    "7FAF2F2D147C8D48D7AE4F80E82CB400E4F72"
    "94F337A40417D1587654C31F907D030027D3D"
    "65E4C5858590821EF4C5BE3DE2C5A44138338BDF58E8B5103131E7DE585AEBB78B77B2B9"
)
BOTS_KEY = bytes.fromhex(hashtag_channel_key("#bots"))


class TestTheGoldenPacket:
    def test_it_decrypts_with_the_key_derived_from_the_channel_name(self):
        decoded = try_decrypt_group_data_packet(GOLDEN_PACKET, BOTS_KEY)

        assert decoded is not None, "the real packet no longer decrypts"
        assert decoded.data_type == DATA_TYPE_AEIC_IMAGE
        # 4-byte chunk header + 1 metadata byte + the 123 image bytes the
        # sending app reported on its bubble.
        assert len(decoded.data) == 128

    def test_the_blob_is_the_photo_the_sender_described(self):
        decoded = try_decrypt_group_data_packet(GOLDEN_PACKET, BOTS_KEY)
        assert decoded is not None

        chunk = parse_chunk_blob(decoded.data)
        assert chunk is not None, "the decrypted blob is not a valid AEIC chunk"
        assert (chunk.index, chunk.total, chunk.is_parity) == (0, 1, False)
        meta = AeicStreamMetadata.decode(chunk.body[0])
        assert meta is not None and meta.square_size == 512, (
            "the metadata does not match the send dialog's 'Resized to 512 x 512'"
        )
        assert len(chunk.body) - 1 == 123, "not the '123 bytes' the sender reported"

    def test_a_wrong_key_yields_none_not_a_wrong_picture(self):
        """The property that makes RF decode of binary data safe at all."""
        wrong = bytes.fromhex(hashtag_channel_key("#bot"))
        assert try_decrypt_group_data_packet(GOLDEN_PACKET, wrong) is None

    def test_a_flipped_ciphertext_bit_fails_the_mac(self):
        tampered = bytearray(GOLDEN_PACKET)
        tampered[-1] ^= 0x01
        assert try_decrypt_group_data_packet(bytes(tampered), BOTS_KEY) is None

    def test_a_length_byte_pointing_past_the_ciphertext_is_refused(self):
        # Craft: valid MAC but absurd declared length cannot be built without the
        # key, so test the parser directly on a decrypted-shaped payload.
        import hashlib
        import hmac as hmac_mod

        from Crypto.Cipher import AES

        plaintext = bytes([0x1C, 0xAE, 200]) + bytes(13)  # claims 200 of 13
        ct = AES.new(BOTS_KEY, AES.MODE_ECB).encrypt(plaintext)
        mac = hmac_mod.new(BOTS_KEY + bytes(16), ct, hashlib.sha256).digest()[:2]
        payload = bytes([hashlib.sha256(BOTS_KEY).digest()[0]]) + mac + ct

        assert decrypt_group_data(payload, BOTS_KEY) is None


class TestFeedingTheIngest:
    @pytest.fixture
    def channels(self, monkeypatch):
        """A channel table holding #bots, keyed the way the repository keys it."""
        import app.packet_processor as pp

        class _Chan:
            key = hashtag_channel_key("#bots")
            name = "#bots"

        monkeypatch.setattr(pp.ChannelRepository, "get_all", AsyncMock(return_value=[_Chan()]))
        return _Chan

    async def test_the_golden_packet_reaches_handle_channel_data(self, channels, monkeypatch):
        import app.imaging.aeic.channel_data_ingest as cdi
        import app.packet_processor as pp

        seen: list[tuple] = []

        async def fake_handle(parsed, *, conversation_key, broadcast_fn=None):
            seen.append((parsed, conversation_key))
            return True

        monkeypatch.setattr(cdi, "handle_channel_data", fake_handle)

        result = await pp._process_group_data(GOLDEN_PACKET, snr=12.0)

        assert result == {
            "decrypted": True,
            "channel_name": "#bots",
            "channel_key": hashtag_channel_key("#bots"),
        }
        assert len(seen) == 1
        parsed, conversation_key = seen[0]
        assert conversation_key == hashtag_channel_key("#bots"), (
            "the ingest was handed a different key form than the repository's -- "
            "the frame-27 path and this one would mint two sessions for one picture"
        )
        assert parsed.data_type == DATA_TYPE_AEIC_IMAGE
        assert len(parsed.payload) == 128

    async def test_the_echo_of_our_own_send_is_not_ingested(self, channels, monkeypatch):
        """Repeaters re-flood our own packets and the RF log hears the copy.

        Without the filter, every picture we sent to a channel came straight back
        as a second, incoming copy of itself.
        """
        import app.imaging.aeic.channel_data_ingest as cdi
        import app.packet_processor as pp

        handled = AsyncMock()
        monkeypatch.setattr(cdi, "handle_channel_data", handled)
        # The golden packet's chunk carries sender prefix 0x3D1F; pretend it is us.
        monkeypatch.setattr(pp, "_self_sender_prefix", lambda: 0x3D1F)

        result = await pp._process_group_data(GOLDEN_PACKET, snr=12.0)

        handled.assert_not_awaited()
        # Still reported as decrypted, so the packet feed shows the channel.
        assert result is not None and result["decrypted"] is True

    async def test_an_unknown_channel_stays_silent(self, monkeypatch):
        import app.packet_processor as pp

        class _Other:
            key = hashtag_channel_key("#other")
            name = "#other"

        monkeypatch.setattr(pp.ChannelRepository, "get_all", AsyncMock(return_value=[_Other()]))

        assert await pp._process_group_data(GOLDEN_PACKET, snr=12.0) is None
