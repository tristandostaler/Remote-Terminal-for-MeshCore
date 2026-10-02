"""Tests for the MCOtxt v1 codec (port of MeshCore Open Advanced's MCOtxt).

``fixtures/mcotxt_reference_vectors.json`` was produced by running HDDen/
meshcore-open's own Dart implementation over a hand-picked corpus and a
randomised one (mixed scripts, case flips, combining marks, emoji runs, stray
control characters). Matching it byte for byte pins wire compatibility in both
directions: our streams are the reference's streams, and we read what it sends.
"""

import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.compression import (
    CODEC_MCOTXT,
    TRANSPORT_MCOTXT,
    decode_and_describe,
    encode_outbound,
    is_framed_payload,
    mcotxt,
    try_decode_incoming,
)
from app.compression.mcotxt import MCOtxtError
from app.imaging.aeic.channel_data import (
    DATA_TYPE_MCO_APP,
    MCO_APP_SUBTYPE_MCOTXT,
)
from app.imaging.aeic.channel_data_ingest import describe_data_type
from app.imaging.aeic.channel_data_text import carries_text, decode_channel_data_text
from app.models import McmpEnabledRequest, McmpEstimateRequest
from app.repository import ChannelRepository, MessageRepository, RawPacketRepository
from app.routers.messages import estimate_mcmp
from app.routers.settings import set_mcmp_enabled

_VECTORS = json.loads(
    (Path(__file__).parent / "fixtures" / "mcotxt_reference_vectors.json").read_text(
        encoding="utf-8"
    )
)
_ALL = _VECTORS["corpus"] + _VECTORS["fuzz"]

# The frozen generation-0 wire hashes from upstream's model_manifest.json.
_UPSTREAM_HASHES = {
    "en": "55988b3bb2a000adf6e768a8541df5a25fec1628b4ec661a10b577e3af8b3770",
    "ru": "d123c4978a635bf1b26fe37261b7e96a0206b3ba15b2a80542e560f00d1eb193",
    "fr": "910c5821a202a9b35313a9b939355f09d1c4293fe2ed246fed0123639d379ccb",
}

LONG_TEXT = "Battery at 40%, switching to power save and checking channel five for traffic."


def _ids(case: dict) -> str:
    return repr(case["text"][:24])


class TestModels:
    def test_all_seven_languages_load(self):
        models = mcotxt._models()
        assert [m.language for m in models.models] == ["en", "ru", "fr", "de", "it", "uk", "be"]
        assert models.generation == 0

    def test_tables_carry_the_upstream_frozen_hashes(self):
        raw = json.loads(Path(mcotxt._MODEL_PATH).read_text(encoding="utf-8"))
        by_language = {m["language"]: m for m in raw["models"]}
        for language, expected in _UPSTREAM_HASHES.items():
            assert by_language[language]["wireHash"] == expected
            assert mcotxt._wire_hash(by_language[language]) == expected

    def test_an_edited_table_refuses_to_load(self):
        raw = json.loads(Path(mcotxt._MODEL_PATH).read_text(encoding="utf-8"))
        raw["models"][0]["top4"][0] = raw["models"][0]["top4"][1]
        with pytest.raises(MCOtxtError, match="wire hash"):
            mcotxt._ModelSet(raw)


class TestReferenceVectors:
    @pytest.mark.parametrize("case", _ALL, ids=_ids)
    def test_default_pair_stream_matches_the_reference(self, case):
        data, bits = mcotxt.encode_stream(case["text"])
        assert (data.hex(), bits) == (case["stream"], case["bits"])

    @pytest.mark.parametrize("case", _VECTORS["corpus"], ids=_ids)
    def test_full_pair_search_matches_the_reference(self, case):
        data, bits = mcotxt.encode_stream(case["text"], search_all_pairs=True)
        assert (data.hex(), bits) == (case["searchStream"], case["searchBits"])

    @pytest.mark.parametrize("case", _ALL, ids=_ids)
    def test_reference_streams_decode(self, case):
        assert mcotxt.decode_stream(bytes.fromhex(case["stream"]), case["bits"]) == case["decoded"]

    @pytest.mark.parametrize("case", _ALL, ids=_ids)
    def test_text_transport_matches_the_reference(self, case):
        assert mcotxt.encode_text(case["text"]) == case["mct"]
        if case["text"]:
            decoded = mcotxt.try_decode_text(case["mct"])
            assert decoded is not None and decoded.text == case["decoded"]

    @pytest.mark.parametrize("case", _VECTORS["corpus"], ids=_ids)
    def test_room_container_matches_the_reference(self, case):
        body = mcotxt.encode_container(
            case["text"],
            timestamp=1_700_000_000,
            sender_name="Alice",
            reply_author_name="Bob",
            reply_timestamp=1_699_999_999,
        )
        assert body.hex() == case["roomBody"]
        decoded = mcotxt.decode_container(bytes.fromhex(case["roomBody"]))
        assert (
            decoded.text,
            decoded.timestamp,
            decoded.sender_name,
            decoded.reply_author_name,
            decoded.reply_timestamp,
        ) == (case["decoded"], 1_700_000_000, "Alice", "Bob", 1_699_999_999)


class TestCodec:
    def test_normalisation(self):
        assert mcotxt.normalize_text("a\r\nb\rc") == "a\nb\nc"
        assert mcotxt.normalize_text("café") == "café"
        assert mcotxt.normalize_text("ёж") == "ёж"

    def test_short_text_rides_as_raw_utf8(self):
        data, bits = mcotxt.encode_stream("~^`")
        assert bits == 16 + 3 * 8
        assert mcotxt.decode_stream(data, bits) == "~^`"

    def test_language_pick_follows_the_letters(self):
        models = mcotxt._models()

        def pair(text: str):
            return mcotxt._default_pair([ord(c) for c in text], models)

        assert pair("hello there") == (0, 1)
        assert pair("привет как дела") == (1, 0)
        assert pair("Grüß Gott, schöne Straße") == (3, 0)
        assert pair("") == (0, 1)

    def test_text_compresses_well_below_utf8(self):
        russian = "Привет всем, кто-нибудь слышит меня с северной стороны города сегодня вечером?"
        data, _bits = mcotxt.encode_stream(russian)
        assert len(data) < len(russian.encode("utf-8")) * 0.45

    def test_empty_and_already_wrapped_text_pass_through(self):
        assert mcotxt.encode_text("") == ""
        wrapped = mcotxt.encode_text(LONG_TEXT)
        assert mcotxt.encode_text(wrapped) == wrapped


class TestStrictDecoder:
    @staticmethod
    def _stream(*fields: tuple[int, int]) -> tuple[bytes, int]:
        writer = mcotxt._BitWriter()
        for value, count in fields:
            writer.write(value, count)
        return writer.to_bytes(), writer.bit_length

    def test_wrong_version_is_rejected(self):
        with pytest.raises(MCOtxtError, match="version"):
            mcotxt.decode_stream(*self._stream((2, 3), (0, 3), (0, 3), (7, 3)))

    def test_unknown_generation_is_rejected(self):
        with pytest.raises(MCOtxtError, match="generation"):
            mcotxt.decode_stream(*self._stream((1, 3), (5, 3), (0, 3), (7, 3)))

    def test_raw_utf8_decodes_under_any_generation(self):
        data, bits = self._stream((1, 3), (5, 3), (7, 3), (1, 3), (0, 4), (0x68, 8), (0x69, 8))
        assert mcotxt.decode_stream(data, bits) == "hi"

    def test_truncated_token_is_rejected(self):
        data, bits = self._stream((1, 3), (0, 3), (0, 3), (7, 3), (0b10, 2), (1, 3))
        with pytest.raises(MCOtxtError, match="unexpected end"):
            mcotxt.decode_stream(data, bits)

    def test_shift_at_the_end_is_rejected(self):
        data, bits = self._stream((1, 3), (0, 3), (0, 3), (7, 3), (0b11110, 5))
        with pytest.raises(MCOtxtError, match="SHIFT"):
            mcotxt.decode_stream(data, bits)

    def test_toggle_without_language_b_is_rejected(self):
        data, bits = self._stream((1, 3), (0, 3), (0, 3), (7, 3), (0b111110, 6))
        with pytest.raises(MCOtxtError, match="language B"):
            mcotxt.decode_stream(data, bits)

    def test_invalid_utf8_run_is_rejected(self):
        data, bits = self._stream(
            (1, 3), (0, 3), (0, 3), (7, 3), (63, 6), (2, 3), (1, 5), (0xC3, 8), (0x28, 8)
        )
        with pytest.raises(MCOtxtError, match="UTF-8"):
            mcotxt.decode_stream(data, bits)

    def test_container_trailing_bytes_and_unknown_flags_are_rejected(self):
        body = mcotxt.encode_container("hello there")
        with pytest.raises(MCOtxtError, match="trailing"):
            mcotxt.decode_container(body + b"\x00")
        with pytest.raises(MCOtxtError, match="flags"):
            mcotxt.decode_container(bytes([body[0] | 0x80]) + body[1:])

    def test_other_revision_or_noise_is_not_decoded(self):
        payload = bytes([(mcotxt.SUBTYPE_ID << 4) | 0x02]) + mcotxt.encode_container(LONG_TEXT)
        assert mcotxt.try_decode_text(mcotxt.PREFIX + mcotxt.encode_base91(payload)) is None
        assert mcotxt.try_decode_text("mct:hello world") is None
        assert mcotxt.try_decode_text("mct:") is None


class TestTransportIntegration:
    def test_encode_outbound_compresses_when_smaller(self):
        wire = encode_outbound(LONG_TEXT, version=TRANSPORT_MCOTXT)
        assert wire.startswith("mct:")
        assert len(wire.encode("utf-8")) < len(LONG_TEXT.encode("utf-8"))
        assert encode_outbound(LONG_TEXT, version=TRANSPORT_MCOTXT) == wire  # deterministic

    def test_encode_outbound_keeps_short_text_plain(self):
        assert encode_outbound("ok", version=TRANSPORT_MCOTXT) == "ok"

    @pytest.mark.parametrize(
        "text",
        [LONG_TEXT.replace("power", "power\r\n"), LONG_TEXT.replace("five", "cafe\u0301")],
    )
    def test_text_the_codec_would_normalise_goes_out_plain(self, text):
        """What a peer (and our own echo) reads must equal what we stored."""
        assert mcotxt.normalize_text(text) != text
        assert encode_outbound(text, version=TRANSPORT_MCOTXT) == text

    def test_framed_payloads_are_never_wrapped(self):
        wire = encode_outbound(LONG_TEXT, version=TRANSPORT_MCOTXT)
        assert is_framed_payload(wire)
        assert encode_outbound(wire, version=TRANSPORT_MCOTXT) == wire
        assert encode_outbound(wire, version=3) == wire

    def test_incoming_body_is_decoded_and_described(self):
        wire = encode_outbound(LONG_TEXT, version=TRANSPORT_MCOTXT)
        decoded = try_decode_incoming(wire)
        assert decoded is not None
        assert (decoded.text, decoded.version) == (LONG_TEXT, "mcotxt")

        text, info = decode_and_describe(wire)
        assert text == LONG_TEXT
        assert info is not None and info.codec == CODEC_MCOTXT
        assert info.plain_bytes == len(LONG_TEXT.encode("utf-8"))
        assert info.wire_bytes == len(wire.encode("utf-8"))
        # The ratio covers the text stream only, as in MCO Advanced.
        assert info.payload_bytes < info.wire_bytes

    def test_undecodable_body_passes_through(self):
        assert decode_and_describe("mct:not really") == ("mct:not really", None)


class TestChannelData:
    """MCO Advanced's binary channel transport: the container behind 0x0120."""

    @staticmethod
    def _envelope(body: bytes, *, revision: int = mcotxt.WIRE_REVISION) -> bytes:
        # Empty outer name: the name rides inside the container.
        return b"\x00" + bytes([(MCO_APP_SUBTYPE_MCOTXT << 4) | revision]) + body

    def test_a_grp_data_message_decodes_with_sender_and_clock(self):
        body = mcotxt.encode_container("on my way", timestamp=1_700_000_123, sender_name="Phone")
        payload = self._envelope(body)
        assert carries_text(DATA_TYPE_MCO_APP, payload) is True
        decoded = decode_channel_data_text(DATA_TYPE_MCO_APP, payload)
        assert decoded is not None
        assert (decoded.sender_name, decoded.text, decoded.version, decoded.timestamp) == (
            "Phone",
            "on my way",
            "mcotxt",
            1_700_000_123,
        )

    def test_another_revision_is_refused(self):
        body = mcotxt.encode_container("hello", timestamp=1, sender_name="Phone")
        assert decode_channel_data_text(DATA_TYPE_MCO_APP, self._envelope(body, revision=2)) is None

    def test_noise_is_not_turned_into_a_message(self):
        assert decode_channel_data_text(DATA_TYPE_MCO_APP, self._envelope(bytes(30))) is None

    def test_description_names_the_codec(self):
        assert "MCOtxt" in describe_data_type(DATA_TYPE_MCO_APP, self._envelope(b"\x04"))


class TestApi:
    @pytest.mark.asyncio
    async def test_estimate_sizes_the_mct_body(self):
        result = await estimate_mcmp(McmpEstimateRequest(text=LONG_TEXT, version=TRANSPORT_MCOTXT))
        assert result.compressed is True
        assert result.wire_bytes == len(encode_outbound(LONG_TEXT, version=4).encode("utf-8"))

    def test_unknown_transport_is_rejected(self):
        with pytest.raises(ValidationError):
            McmpEstimateRequest(text=LONG_TEXT, version=5)
        with pytest.raises(ValidationError):
            McmpEnabledRequest(type="channel", id="x", enabled=True, version=5)

    @pytest.mark.asyncio
    async def test_channel_can_select_mcotxt(self, test_db):
        key = "c3" * 16
        await ChannelRepository.upsert(key=key, name="#general")
        resp = await set_mcmp_enabled(
            McmpEnabledRequest(type="channel", id=key, enabled=True, version=TRANSPORT_MCOTXT)
        )
        assert resp.version == TRANSPORT_MCOTXT
        channel = await ChannelRepository.get_by_key(key)
        assert channel is not None and channel.mcmp_version == TRANSPORT_MCOTXT

    @pytest.mark.asyncio
    async def test_missing_channel_still_404s(self, test_db):
        with pytest.raises(HTTPException) as exc:
            await set_mcmp_enabled(
                McmpEnabledRequest(type="channel", id="d4" * 16, enabled=True, version=4)
            )
        assert exc.value.status_code == 404


class TestIngest:
    @pytest.mark.asyncio
    async def test_our_own_channel_echo_folds_onto_the_sent_row(self, test_db):
        """The repeat of a sent MCOtxt message decodes to the stored text, so it
        dedups onto the outgoing row and counts as a repeat."""
        from app.packet_processor import create_message_from_decrypted

        key = "ABC123DEF456ABC123DEF456ABC12345"
        sent_id = await MessageRepository.create(
            msg_type="CHAN",
            text=f"Me: {LONG_TEXT}",
            conversation_key=key,
            sender_timestamp=1_700_000_100,
            received_at=1_700_000_100,
            outgoing=True,
        )
        packet_id, _ = await RawPacketRepository.create(b"chan_mcotxt_echo", 1_700_000_101)
        await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key=key,
            sender="Me",
            message_text=encode_outbound(LONG_TEXT, version=TRANSPORT_MCOTXT),
            timestamp=1_700_000_100,
        )
        rows = await MessageRepository.get_all(msg_type="CHAN", conversation_key=key, limit=10)
        assert [row.id for row in rows] == [sent_id]
        assert rows[0].acked == 1

    @pytest.mark.asyncio
    async def test_channel_body_is_decoded_and_recorded(self, test_db):
        from app.packet_processor import create_message_from_decrypted

        wire = encode_outbound(LONG_TEXT, version=TRANSPORT_MCOTXT)
        packet_id, _ = await RawPacketRepository.create(b"chan_mcotxt", 1_700_000_000)
        msg_id = await create_message_from_decrypted(
            packet_id=packet_id,
            channel_key="ABC123DEF456ABC123DEF456ABC12345",
            sender="Alice",
            message_text=wire,
            timestamp=1_700_000_000,
        )
        assert msg_id is not None
        stored = await MessageRepository.get_by_id(msg_id)
        assert stored is not None
        assert stored.text == f"Alice: {LONG_TEXT}"
        assert stored.compression == CODEC_MCOTXT
        assert stored.wire_bytes == len(wire.encode("utf-8"))
