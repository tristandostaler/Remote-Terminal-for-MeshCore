"""Choosing a transport for media fragments.

Firmware without ``CMD_SEND_RAW_DATA`` cannot move an image or voice fragment at
all: not the fetch request out, not the fragments back. Opening a received
picture on such a node answered 501 and there was nothing the person tapping it
could do, so the same payload bytes are carried as ``rmt1:`` text instead.

Two rules decide which transport runs, and these tests pin both. What we
*initiate* follows ``contacts.raw_media_text_transport`` -- a switch, so it does
not wait for a raw send to fail first. What we *reply* mirrors the transport its
request arrived on, which is what keeps a MeshCore SAR client answered in raw
with the switch on.
"""

from types import SimpleNamespace

import pytest
from meshcore import EventType

from app.image_protocol import ImageFetchRequest, ImagePacket
from app.services import raw_media
from app.services.raw_media import (
    MediaTransport,
    RawDataUnsupportedError,
    note_inbound_text_chunk,
    send_raw_to_contact,
)
from app.services.raw_media_text import parse_chunk, reassemble, reset_pending_transfers

UNSUPPORTED = {"error_code": 1, "code_string": "ERR_CODE_UNSUPPORTED_CMD"}
PEER_KEY = "ab" * 32


@pytest.fixture(autouse=True)
def _no_chunk_delay(monkeypatch):
    """The real 1.2 s inter-chunk gap is airtime pacing, not behaviour."""
    monkeypatch.setattr(raw_media, "RAW_MEDIA_TEXT_CHUNK_DELAY_SECONDS", 0)
    reset_pending_transfers()
    yield
    reset_pending_transfers()


class _Radio:
    """A radio that records what it was asked to do.

    ``raw_result`` is the canned answer to ``send_raw_data``; ``text_result`` the
    answer to ``send_msg``. ``operations`` records every radio operation name, so
    a test can tell a skipped raw attempt from a failed one.
    """

    def __init__(self, *, raw_result=None, text_result=None, firmware_version=None):
        self.firmware_version = firmware_version
        self.raw_data_unsupported = False
        self._raw_result = raw_result
        self._text_result = text_result or SimpleNamespace(type=EventType.MSG_SENT, payload={})
        self.operations: list[str] = []
        self.raw_frames: list[bytes] = []
        self.text_messages: list[str] = []
        self.added_contacts: list[dict] = []

    def radio_operation(self, name, *, blocking=True):
        self.operations.append(name)
        radio = self

        async def send_raw_data(frame):
            radio.raw_frames.append(frame)
            return radio._raw_result

        async def send_msg(*, dst, msg, timestamp):
            radio.text_messages.append(msg)
            return radio._text_result

        async def add_contact(contact_data):
            radio.added_contacts.append(contact_data)
            return SimpleNamespace(type=EventType.OK, payload={})

        class _Ctx:
            async def __aenter__(self):
                return SimpleNamespace(
                    commands=SimpleNamespace(
                        send_raw_data=send_raw_data,
                        send_msg=send_msg,
                        add_contact=add_contact,
                    ),
                    get_contact_by_key_prefix=lambda _prefix: {"public_key": PEER_KEY},
                )

            async def __aexit__(self, *_exc):
                return False

        return _Ctx()


def _contact(*, text=True):
    return SimpleNamespace(
        public_key=PEER_KEY,
        effective_route_tuple=lambda: ("", 0, 0),
        to_radio_dict=lambda: {"public_key": PEER_KEY},
        raw_media_text_transport=text,
    )


def _payload():
    return ImagePacket("45abcdef", 3, b"x" * 152).encode()


def _reassembled(radio: _Radio) -> bytes | None:
    parsed = [parse_chunk(text) for text in radio.text_messages]
    assert all(chunk is not None for chunk in parsed)
    chunks = {chunk.index: chunk.payload for chunk in parsed if chunk is not None}
    return reassemble(chunks, len(chunks))


async def test_a_request_we_start_goes_over_text_without_trying_raw_first():
    """The switch, and the thing that makes it a switch rather than a fallback.

    A raw attempt first would be a wasted round trip on the firmware this exists
    for, and it is the *only* way to find out on that firmware -- so with the
    switch on there is no attempt to waste.
    """
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))
    payload = _payload()

    await send_raw_to_contact(radio, _contact(), payload)

    assert not radio.raw_frames, "tried raw data even though the switch chose text"
    assert _reassembled(radio) == payload


async def test_a_request_we_start_goes_over_raw_when_the_switch_is_off():
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))

    await send_raw_to_contact(radio, _contact(text=False), _payload())

    assert len(radio.raw_frames) == 1
    assert not radio.text_messages


async def test_a_raw_request_is_answered_in_raw_even_with_the_switch_on():
    """What keeps MeshCore SAR clients working. They speak raw data and nothing
    else, so answering their fetch request over text would be unreadable -- the
    switch governs what we start, never what we reply."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))

    await send_raw_to_contact(radio, _contact(), _payload(), transport=MediaTransport.RAW)

    assert len(radio.raw_frames) == 1
    assert not radio.text_messages, "answered a raw request over text"


async def test_a_text_request_is_answered_over_text():
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))
    payload = _payload()

    await send_raw_to_contact(radio, _contact(), payload, transport=MediaTransport.TEXT)

    assert not radio.raw_frames
    assert _reassembled(radio) == payload


async def test_the_switch_off_keeps_raw_even_for_a_text_request():
    """Off means "spend no extra airtime on this contact", which has to hold for a
    reply too or the setting can be overridden by whoever asks."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))

    await send_raw_to_contact(
        radio, _contact(text=False), _payload(), transport=MediaTransport.TEXT
    )

    assert len(radio.raw_frames) == 1
    assert not radio.text_messages


async def test_a_reply_that_must_go_raw_still_falls_back_when_raw_cannot_work():
    """Mirroring asks for raw; this firmware cannot send it. Text is then the only
    remaining way to answer at all, so the switch being on still rescues it."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED))
    payload = _payload()

    await send_raw_to_contact(radio, _contact(), payload, transport=MediaTransport.RAW)

    assert radio.raw_data_unsupported is True
    assert _reassembled(radio) == payload


async def test_the_firmware_limit_is_learned_once_not_once_per_fragment():
    """A picture is 15-40 fragments. Re-discovering the limit on each one costs a
    doomed radio round trip every time, which is the difference between a slow
    transfer and an unusable one. Only reachable while mirroring raw requests --
    a request we start never attempts raw with the switch on."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED))
    contact = _contact()

    await send_raw_to_contact(radio, contact, _payload(), transport=MediaTransport.RAW)
    assert radio.raw_data_unsupported is True
    attempts_after_first = len(radio.raw_frames)

    for _ in range(5):
        await send_raw_to_contact(radio, contact, _payload(), transport=MediaTransport.RAW)

    assert len(radio.raw_frames) == attempts_after_first == 1


async def test_turning_the_switch_off_restores_the_plain_firmware_error():
    radio = _Radio(
        raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED),
        firmware_version="v1.9.0-abc",
    )

    with pytest.raises(RawDataUnsupportedError) as exc_info:
        await send_raw_to_contact(radio, _contact(text=False), _payload())

    message = str(exc_info.value)
    assert "v1.9.0-abc" in message
    assert "CMD_SEND_RAW_DATA" in message
    # Both ways out have to be in the message: the person seeing it may not be
    # able to reflash a node, and the switch is not discoverable on its own.
    assert "text transport" in message
    assert not radio.text_messages


async def test_a_contact_missing_the_column_entirely_still_gets_the_text_transport():
    """An older contact row, or a caller holding a contact-shaped object, must not
    turn a working transport into an AttributeError in the middle of a send."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED))
    contact = SimpleNamespace(
        public_key=PEER_KEY,
        effective_route_tuple=lambda: ("", 0, 0),
        to_radio_dict=lambda: {"public_key": PEER_KEY},
    )

    await send_raw_to_contact(radio, contact, _payload())

    assert _reassembled(radio) == _payload()


async def test_an_ordinary_radio_failure_is_not_treated_as_a_missing_command():
    """Only ERR_CODE_UNSUPPORTED_CMD means "this firmware cannot". Everything else
    is transient, and spending 2.5x the airtime on a text transfer because the
    radio was momentarily busy would be the wrong trade."""
    radio = _Radio(
        raw_result=SimpleNamespace(type=EventType.ERROR, payload={"error_code": 4}),
    )

    with pytest.raises(RuntimeError) as exc_info:
        await send_raw_to_contact(radio, _contact(), _payload(), transport=MediaTransport.RAW)

    assert "raw data send failed" in str(exc_info.value)
    assert not radio.text_messages
    assert radio.raw_data_unsupported is False


async def test_a_failed_text_chunk_is_reported_rather_than_silently_dropped():
    radio = _Radio(
        raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED),
        text_result=SimpleNamespace(type=EventType.ERROR, payload={"error_code": 7}),
    )

    with pytest.raises(RuntimeError, match="raw media text send failed"):
        await send_raw_to_contact(radio, _contact(), _payload())


async def test_the_radio_lock_is_taken_per_chunk_not_held_for_the_whole_transfer():
    """A 40-message transfer takes minutes. Holding the operation lock across it
    would stall every other radio user for its whole duration."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED))

    await send_raw_to_contact(radio, _contact(), _payload())

    sends = [name for name in radio.operations if name == "raw_media_text_send"]
    assert len(sends) == len(radio.text_messages) == 2
    assert radio.operations.count("raw_media_text_prepare") == 1


async def test_an_inbound_transfer_reaches_the_same_dispatch_a_raw_push_would():
    """The whole point: the bytes are indistinguishable once reassembled, so the
    image and voice handlers need no idea which transport carried them."""
    dispatched: list[tuple[bytes, MediaTransport]] = []

    async def record(payload, _radio_manager, *, transport=MediaTransport.RAW):
        dispatched.append((payload, transport))

    original = raw_media.dispatch_raw_media_payload
    raw_media.dispatch_raw_media_payload = record
    try:
        request = ImageFetchRequest("45abcdef", "aabbccddeeff", tuple(range(140))).encode()
        radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))
        await send_raw_to_contact(radio, _contact(), request)
        assert len(radio.text_messages) == 2

        for index, text in enumerate(radio.text_messages):
            handled = await note_inbound_text_chunk(
                text=text, sender_key=PEER_KEY, radio_manager=radio
            )
            assert handled is True
            # Nothing dispatched until the last chunk lands.
            assert dispatched == ([] if index == 0 else [(request, MediaTransport.TEXT)])
    finally:
        raw_media.dispatch_raw_media_payload = original

    # Dispatched as TEXT, so whatever this payload turns out to be is answered the
    # same way. That is the whole mirroring rule, at the one point it is decided.
    assert dispatched == [(request, MediaTransport.TEXT)]


async def test_an_inbound_chunk_is_dispatched_as_text_so_the_reply_mirrors_it():
    """The replacement for remembering tunnel peers by key and TTL. The arriving
    chunk already states its transport, so nothing has to be recalled -- and a
    reply cannot disagree with the request it is answering."""
    seen: list[MediaTransport] = []

    async def record(_payload, _radio_manager, *, transport=MediaTransport.RAW):
        seen.append(transport)

    original = raw_media.dispatch_raw_media_payload
    raw_media.dispatch_raw_media_payload = record
    try:
        chunk = (await _one_tunnel_chunk())[0]
        await note_inbound_text_chunk(text=chunk, sender_key=PEER_KEY, radio_manager=None)
    finally:
        raw_media.dispatch_raw_media_payload = original

    assert seen == [MediaTransport.TEXT]


async def _ignore_payload(_payload, _radio_manager):
    return None


async def _one_tunnel_chunk() -> list[str]:
    """One fetch request, framed for the tunnel: 13 bytes, so exactly one chunk."""
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.OK, payload={}))
    await send_raw_to_contact(
        radio, _contact(), ImageFetchRequest("45abcdef", "aabbccddeeff").encode()
    )
    assert len(radio.text_messages) == 1
    return radio.text_messages


async def test_ordinary_text_is_left_for_the_message_layer():
    assert await note_inbound_text_chunk(text="hello there", sender_key=PEER_KEY) is False


async def test_a_chunk_from_an_unidentified_sender_is_swallowed_not_stored():
    """Returning False would put machine framing in the chat. There is no sender
    to scope reassembly to, so it cannot be acted on either."""
    chunk = (await _one_tunnel_chunk())[0]

    assert await note_inbound_text_chunk(text=chunk, sender_key=None) is True


async def test_a_voice_fragment_that_arrived_as_text_is_not_acked(monkeypatch):
    """The ACK has no consumer on this side, and a fragment that arrived over text
    came from RemoteTerm, which has none either -- it would be one extra message
    per fragment, doubling the cost of a recording carried over text."""
    from app.services import voice as voice_service
    from app.voice_protocol import VoicePacket

    sent: list[bytes] = []

    async def record_send(_radio_manager, _contact, payload, **_kwargs):
        sent.append(payload)

    async def session(_session_id):
        return {
            "session_id": "45abcdef",
            "packet_count": 4,
            "peer_public_key": PEER_KEY,
            "fragments": [],
        }

    monkeypatch.setattr(voice_service, "send_raw_to_contact", record_send)
    monkeypatch.setattr(voice_service.VoiceRepository, "get", session)
    monkeypatch.setattr(voice_service.VoiceRepository, "add_fragment", _accept_fragment)
    monkeypatch.setattr(voice_service, "broadcast_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(voice_service.ContactRepository, "get_by_key", _some_contact)

    fragment = VoicePacket("45abcdef", 0, b"\x5a" * 8).encode()

    assert (
        await voice_service.handle_raw_voice_payload(fragment, None, transport=MediaTransport.TEXT)
        is True
    )
    assert sent == [], "acked a fragment that arrived over text"

    # A fragment that arrived raw still gets its ACK: a SAR client retries without one.
    assert (
        await voice_service.handle_raw_voice_payload(fragment, None, transport=MediaTransport.RAW)
        is True
    )
    assert len(sent) == 1


async def _accept_fragment(*_args, **_kwargs):
    return True


async def _some_contact(_key):
    return _contact()


def _received_image_message():
    from app.image_protocol import ImageEnvelope, ImageFormat

    return SimpleNamespace(
        id=7,
        type="PRIV",
        text=ImageEnvelope("45abcdef", ImageFormat.JPEG, 2, 32, 24, 300).encode(),
        sender_name=None,
        sender_key=PEER_KEY,
        conversation_key=PEER_KEY,
        outgoing=False,
    )


def _empty_image_session():
    return {
        "session_id": "45abcdef",
        "message_id": 7,
        "state": "available",
        "format": 1,
        "width": 32,
        "height": 24,
        "size_bytes": 300,
        "fragment_count": 2,
        "fragments": [],
        "peer_public_key": PEER_KEY,
    }


def _patch_image_router(monkeypatch, radio, contact):
    """Wire the real router and service to a fake radio and repositories.

    Only the storage and radio edges are faked: the router, ``request_image_session``
    and the transport all run for real, which is the point -- the bug was in how
    those three fitted together.
    """
    from app.routers import images as images_router
    from app.services import image as image_service

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(images_router.ImageRepository, "enforce_cache_limit", _noop)
    monkeypatch.setattr(images_router.ImageRepository, "create_session", _noop)
    monkeypatch.setattr(
        images_router.ImageRepository, "get", lambda _sid: _resolve(_empty_image_session())
    )
    monkeypatch.setattr(
        images_router.MessageRepository,
        "get_by_id",
        lambda _mid: _resolve(_received_image_message()),
    )
    radio.require_connected = lambda: None
    monkeypatch.setattr(images_router, "radio_manager", radio)
    monkeypatch.setattr(
        image_service.ContactRepository, "get_by_key_or_prefix", lambda _key: _resolve(contact)
    )
    monkeypatch.setattr(image_service, "get_public_key", lambda: bytes(range(32)))
    return images_router


async def test_opening_a_received_picture_now_works_on_a_node_without_raw_data(monkeypatch):
    """The whole reported failure, end to end: router -> service -> transport.

    Tapping a received picture on this firmware answered 501 "This node's firmware
    cannot send raw data packets" and there was nothing the person tapping it
    could do. The fetch request now leaves as text instead.
    """
    radio = _Radio(raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED))
    images_router = _patch_image_router(monkeypatch, radio, _contact())

    payload = await images_router.fetch_image(7)

    assert payload["missing_indices"] == [0, 1]
    assert len(radio.text_messages) == 1, "the fetch request did not go out over text"
    assert _reassembled(radio) == ImageFetchRequest("45abcdef", bytes(range(6)).hex()).encode()


async def test_the_same_picture_still_reports_501_when_the_switch_is_off(monkeypatch):
    """The other half of the switch, at the level the person tapping it sees.

    Off means the old behaviour on purpose: a clear error rather than minutes of
    airtime spent without being asked for.
    """
    from fastapi import HTTPException

    radio = _Radio(
        raw_result=SimpleNamespace(type=EventType.ERROR, payload=UNSUPPORTED),
        firmware_version="v1.9.0-abc",
    )
    images_router = _patch_image_router(monkeypatch, radio, _contact(text=False))

    with pytest.raises(HTTPException) as exc_info:
        await images_router.fetch_image(7)

    assert exc_info.value.status_code == 501
    assert "CMD_SEND_RAW_DATA" in str(exc_info.value.detail)
    assert not radio.text_messages


async def _resolve(value):
    return value
