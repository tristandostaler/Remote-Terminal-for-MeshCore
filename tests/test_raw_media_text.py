"""The rmt1: tunnel that carries media fragments when raw data is unavailable.

The payload bytes have to survive the round trip *exactly* -- they are handed to
the same dispatch a real RAW_DATA push feeds, so a single wrong byte becomes a
corrupt image fragment rather than an error. And two peers answering fetch
requests at the same moment must not have their chunks merged, which is what the
sender-scoped transfer key is for.
"""

import pytest

from app.image_protocol import ImageFetchRequest, ImagePacket
from app.services import raw_media_text
from app.services.raw_media_text import (
    DEFAULT_MESSAGE_BUDGET,
    HEADER_CHARS,
    MAX_CHUNKS,
    TRANSFER_TTL_SECONDS,
    RawMediaTextChunk,
    RawMediaTextFormatError,
    chunk_capacity,
    encode_chunks,
    is_tunnel_chunk,
    note_chunk,
    parse_chunk,
    reassemble,
    reset_pending_transfers,
    transfer_key,
)
from app.voice_protocol import VoicePacket

ALICE = "aa" * 32
BOB = "bb" * 32


@pytest.fixture(autouse=True)
def _clean_transfers():
    reset_pending_transfers()
    yield
    reset_pending_transfers()


def _chunks_to_map(chunks: list[str]) -> dict[int, str]:
    parsed = [parse_chunk(chunk) for chunk in chunks]
    assert all(chunk is not None for chunk in parsed)
    return {chunk.index: chunk.payload for chunk in parsed if chunk is not None}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(ImagePacket("45abcdef", 3, b"\x00" * 152).encode(), id="full-image-fragment"),
        pytest.param(ImagePacket("45abcdef", 0, b"\xff").encode(), id="one-byte-image-fragment"),
        pytest.param(ImageFetchRequest("45abcdef", "aabbccddeeff").encode(), id="fetch-request"),
        pytest.param(
            ImageFetchRequest("45abcdef", "aabbccddeeff", tuple(range(140))).encode(),
            id="fetch-request-with-every-index-missing",
        ),
        pytest.param(VoicePacket("45abcdef", 7, b"\x5a" * 64).encode(), id="voice-fragment"),
        pytest.param(bytes(range(256)), id="every-byte-value"),
    ],
)
def test_a_payload_survives_the_tunnel_byte_for_byte(payload):
    chunks = encode_chunks(payload, transfer_id=42)
    assert reassemble(_chunks_to_map(chunks), len(chunks)) == payload


def test_a_full_image_fragment_costs_two_messages():
    """The documented price of the fallback, pinned so it cannot drift quietly.

    158 bytes of fragment become 195 basE91 characters, and 195 does not fit in
    one 156-byte message. Everything in the module docstring about airtime --
    "a 20-fragment picture -> 40 messages" -- rests on this number.
    """
    payload = ImagePacket("45abcdef", 3, b"x" * 152).encode()
    assert len(payload) == 158

    chunks = encode_chunks(payload, transfer_id=1)

    assert len(chunks) == 2
    assert [len(chunk) for chunk in chunks] == [DEFAULT_MESSAGE_BUDGET, 57]


def test_a_fresh_fetch_request_costs_one_message():
    """Asking for a whole picture is the cheap direction: 13 bytes, one message."""
    payload = ImageFetchRequest("45abcdef", "aabbccddeeff").encode()

    assert len(encode_chunks(payload, transfer_id=1)) == 1


@pytest.mark.parametrize("size", [1, 2, 100, 147, 148, 158, 500, 1000])
def test_no_chunk_ever_exceeds_the_message_budget(size):
    """A chunk over budget is silently truncated by the radio, corrupting the
    payload with no error anywhere. The header has to be paid for out of the
    budget, not added on top of it."""
    chunks = encode_chunks(b"z" * size)

    assert all(len(chunk) <= DEFAULT_MESSAGE_BUDGET for chunk in chunks)
    assert chunk_capacity() == DEFAULT_MESSAGE_BUDGET - HEADER_CHARS


def test_header_is_fixed_width_so_a_base91_colon_cannot_confuse_it():
    """basE91 contains ':'. Splitting on the delimiter would be ambiguous, so the
    header is read by position and the payload is whatever follows character 9."""
    chunks = encode_chunks(b"payload bytes here", transfer_id=1295)

    assert chunks[0].startswith("rmt1:")
    assert len(chunks[0]) > HEADER_CHARS
    parsed = parse_chunk(chunks[0])
    assert parsed is not None
    assert parsed.transfer_id == 1295
    assert parsed.payload == chunks[0][HEADER_CHARS:]


def test_a_payload_too_big_to_address_is_refused_rather_than_truncated():
    """36 chunks addresses ~4.3 KB. Past that the count wraps its single base36
    digit, so chunk 36 would announce itself as chunk 0 and quietly overwrite it."""
    assert len(encode_chunks(b"z" * 4000)) <= MAX_CHUNKS

    with pytest.raises(RawMediaTextFormatError, match="more than the 36"):
        encode_chunks(b"z" * 6000)


def test_an_empty_payload_is_refused():
    with pytest.raises(RawMediaTextFormatError, match="empty payload"):
        encode_chunks(b"")


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("hello there", id="ordinary-prose"),
        pytest.param("rmt1:", id="prefix-only"),
        pytest.param("rmt1:0011", id="header-but-no-payload"),
        pytest.param("rmt1:0021payload", id="index-past-total"),
        pytest.param("rmt1:0000payload", id="zero-total"),
        pytest.param("rmt1:ZZ11payload", id="uppercase-base36"),
        pytest.param("IE4:jbxb73:1:2:g:o:12c", id="an-image-envelope"),
        pytest.param("aei10123payload", id="an-aeic-chunk"),
    ],
)
def test_text_that_is_not_a_chunk_parses_as_none(text):
    """None, never an exception: this runs on every inbound direct message."""
    assert parse_chunk(text) is None


def test_the_prefix_test_agrees_with_the_parser_on_real_chunks():
    for chunk in encode_chunks(b"a payload", transfer_id=3):
        assert is_tunnel_chunk(chunk)
        assert parse_chunk(chunk) is not None


def test_an_incomplete_transfer_reassembles_to_nothing():
    chunks = encode_chunks(ImagePacket("45abcdef", 3, b"x" * 152).encode(), transfer_id=1)
    partial = _chunks_to_map(chunks)
    del partial[1]

    assert reassemble(partial, len(chunks)) is None


def test_chunks_arrive_out_of_order_and_still_reassemble():
    payload = ImagePacket("45abcdef", 3, b"x" * 152).encode()
    chunks = encode_chunks(payload, transfer_id=9)

    assert note_chunk(_parsed(chunks[1]), sender_key=ALICE, now=100.0) is None
    assert note_chunk(_parsed(chunks[0]), sender_key=ALICE, now=101.0) == payload


def _parsed(text: str) -> RawMediaTextChunk:
    chunk = parse_chunk(text)
    assert chunk is not None
    return chunk


def test_two_senders_reusing_one_transfer_id_do_not_merge():
    """The id is two base36 characters, so it is unique per sender, not globally.

    Two contacts answering fetch requests at the same moment will collide sooner
    or later. Merging their chunks would produce a payload made of both, which
    decodes to garbage rather than failing.
    """
    mine = ImagePacket("45abcdef", 0, b"a" * 152).encode()
    theirs = ImagePacket("45abcdef", 1, b"b" * 152).encode()
    from_alice = encode_chunks(mine, transfer_id=7)
    from_bob = encode_chunks(theirs, transfer_id=7)

    assert note_chunk(_parsed(from_alice[0]), sender_key=ALICE, now=100.0) is None
    assert note_chunk(_parsed(from_bob[0]), sender_key=BOB, now=100.0) is None
    assert note_chunk(_parsed(from_bob[1]), sender_key=BOB, now=101.0) == theirs
    assert note_chunk(_parsed(from_alice[1]), sender_key=ALICE, now=102.0) == mine


def test_a_transfer_id_reused_with_a_different_length_starts_over():
    """A disagreeing total means a new transfer reusing the id, not a continuation."""
    first = encode_chunks(b"z" * 200, transfer_id=4)
    second = encode_chunks(b"q" * 20, transfer_id=4)
    assert len(first) == 2 and len(second) == 1

    assert note_chunk(_parsed(first[0]), sender_key=ALICE, now=100.0) is None
    assert note_chunk(_parsed(second[0]), sender_key=ALICE, now=101.0) == b"q" * 20


def test_a_half_delivered_transfer_is_dropped_once_it_expires():
    chunks = encode_chunks(b"z" * 200, transfer_id=8)

    assert note_chunk(_parsed(chunks[0]), sender_key=ALICE, now=100.0) is None
    # The other half turns up after the window; it must not complete a transfer
    # whose first half is long gone, nor resurrect it.
    late = 100.0 + TRANSFER_TTL_SECONDS + 1
    assert note_chunk(_parsed(chunks[1]), sender_key=ALICE, now=late) is None


def test_a_completed_transfer_does_not_replay_on_a_retransmitted_chunk():
    """The entry is dropped on completion, so a duplicate cannot dispatch twice.

    A replayed fetch request would make us re-send every fragment; a replayed
    fragment would be a second ACK. Both are avoidable by not keeping the entry.
    """
    payload = b"z" * 200
    chunks = encode_chunks(payload, transfer_id=11)

    assert note_chunk(_parsed(chunks[0]), sender_key=ALICE, now=100.0) is None
    assert note_chunk(_parsed(chunks[1]), sender_key=ALICE, now=101.0) == payload
    assert note_chunk(_parsed(chunks[1]), sender_key=ALICE, now=102.0) is None


def test_a_single_chunk_transfer_completes_immediately():
    payload = ImageFetchRequest("45abcdef", "aabbccddeeff").encode()
    chunks = encode_chunks(payload, transfer_id=2)
    assert len(chunks) == 1

    assert note_chunk(_parsed(chunks[0]), sender_key=ALICE, now=100.0) == payload


def test_pending_transfers_stay_bounded_under_a_flood_of_openers():
    """A peer opening transfers it never finishes must not grow the map forever."""
    for transfer_id in range(raw_media_text.MAX_PENDING_TRANSFERS * 3):
        chunk = encode_chunks(b"z" * 200, transfer_id=transfer_id % 1296)[0]
        note_chunk(_parsed(chunk), sender_key=f"{transfer_id:064x}", now=100.0)

    assert len(raw_media_text._pending) <= raw_media_text.MAX_PENDING_TRANSFERS


def test_garbage_in_the_payload_position_reassembles_to_nothing_not_an_exception():
    """A chunk can be corrupted into invalid basE91 by a truncating relay. That
    must look like a lost fragment, not crash the ingest path."""
    assert reassemble({0: "not valid base91 \x00\x01"}, 1) is None


def test_transfer_keys_are_scoped_by_sender_prefix():
    assert transfer_key(ALICE, 7) != transfer_key(BOB, 7)
    assert transfer_key(ALICE.upper(), 7) == transfer_key(ALICE, 7)


def test_the_format_can_address_a_payload_far_larger_than_either_protocol_sends():
    """Headroom check: 36 chunks must comfortably exceed the biggest real payload."""
    biggest_real_payload = 158
    assert MAX_CHUNKS * chunk_capacity() > biggest_real_payload * 20
