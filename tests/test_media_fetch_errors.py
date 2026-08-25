"""Opening received media must explain itself, not answer 500 or a false 409.

Every reason the fragment request can fail is something the person tapping the
picture can act on. They all used to escape ``fetch_image`` uncaught, so the
toast read "Internal Server Error" and the real cause only existed in a
container log.

The session id is also not private to one message: a re-sent or pasted envelope,
and media sent to yourself, produce a second message row for the same content.
"""

from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from meshcore import EventType

from app.image_protocol import ImageEnvelope, ImageFormat
from app.routers.images import fetch_image
from app.routers.voice import fetch_voice
from app.services.voice import RawDataUnsupportedError, send_raw_to_contact

ENVELOPE = ImageEnvelope("45abcdef", ImageFormat.JPEG, 2, 32, 24, 300).encode()


async def _async_noop(*_args, **_kwargs):
    return None


def _incoming_image_message():
    return SimpleNamespace(
        id=7,
        type="PRIV",
        text=ENVELOPE,
        sender_name=None,
        sender_key="ab" * 32,
        conversation_key="ab" * 32,
        outgoing=False,
    )


def _incomplete_session():
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
    }


def _patch_image_fetch(monkeypatch, request_failure):
    message = _incoming_image_message()

    async def get_message(_message_id):
        return message

    async def get_session(_session_id):
        return _incomplete_session()

    monkeypatch.setattr("app.routers.images.ImageRepository.enforce_cache_limit", _async_noop)
    monkeypatch.setattr("app.routers.images.ImageRepository.create_session", _async_noop)
    monkeypatch.setattr("app.routers.images.ImageRepository.get", get_session)
    monkeypatch.setattr("app.routers.images.MessageRepository.get_by_id", get_message)
    monkeypatch.setattr("app.routers.images.radio_manager.require_connected", lambda: None)
    monkeypatch.setattr("app.routers.images.request_image_session", request_failure)
    return message


async def test_image_fetch_reports_an_unroutable_sender_as_a_conflict(monkeypatch):
    async def no_route(_radio_manager, _session):
        raise ValueError("raw media transfer requires a direct or learned route")

    message = _patch_image_fetch(monkeypatch, no_route)

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image(message.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "raw media transfer requires a direct or learned route"


async def test_image_fetch_reports_firmware_without_raw_data_as_not_implemented(monkeypatch):
    async def unsupported(_radio_manager, _session):
        raise RawDataUnsupportedError("This node's firmware (v1.2.3) cannot send raw data")

    message = _patch_image_fetch(monkeypatch, unsupported)

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image(message.id)

    assert exc_info.value.status_code == 501
    assert "cannot send raw data" in exc_info.value.detail


async def test_image_fetch_reports_a_radio_failure_as_a_bad_gateway(monkeypatch):
    async def radio_failed(_radio_manager, _session):
        raise RuntimeError("raw data send failed: no radio response")

    message = _patch_image_fetch(monkeypatch, radio_failed)

    with pytest.raises(HTTPException) as exc_info:
        await fetch_image(message.id)

    assert exc_info.value.status_code == 502
    # Never "voice": the reader is looking at a picture.
    assert "voice" not in exc_info.value.detail


async def test_voice_fetch_separates_a_firmware_limit_from_a_radio_failure(monkeypatch):
    message = SimpleNamespace(
        id=43,
        type="PRIV",
        text="VE3:jbxb73:3:c:a",
        sender_name=None,
        sender_key="ab" * 32,
        conversation_key="ab" * 32,
        outgoing=False,
    )

    async def get_message(_message_id):
        return message

    async def get_session(_session_id):
        return {
            "session_id": "45abcdef",
            "message_id": message.id,
            "state": "available",
            "mode": 3,
            "duration_ms": 10_000,
            "packet_count": 12,
            "fragments": [],
        }

    async def unsupported(_radio_manager, _session):
        raise RawDataUnsupportedError("This node's firmware cannot send raw data")

    monkeypatch.setattr("app.routers.voice.VoiceRepository.enforce_cache_limit", _async_noop)
    monkeypatch.setattr("app.routers.voice.VoiceRepository.upsert_session", _async_noop)
    monkeypatch.setattr("app.routers.voice.VoiceRepository.get", get_session)
    monkeypatch.setattr("app.routers.voice.MessageRepository.get_by_id", get_message)
    monkeypatch.setattr("app.routers.voice.radio_manager.require_connected", lambda: None)
    monkeypatch.setattr("app.routers.voice.request_voice_session", unsupported)

    with pytest.raises(HTTPException) as exc_info:
        await fetch_voice(message.id)

    assert exc_info.value.status_code == 501


class _Radio:
    """A radio whose one raw-data send returns a canned event."""

    def __init__(self, event, firmware_version=None):
        self.firmware_version = firmware_version
        self._event = event

    def radio_operation(self, _name, *, blocking=True):
        event = self._event

        class _Ctx:
            async def __aenter__(self):
                commands = SimpleNamespace(send_raw_data=_send)
                return SimpleNamespace(commands=commands)

            async def __aexit__(self, *_exc):
                return False

        async def _send(_frame):
            return event

        return _Ctx()


_DIRECT_CONTACT = SimpleNamespace(effective_route_tuple=lambda: ("", 0, 0))


@pytest.mark.parametrize(
    "payload",
    [
        {"error_code": 1, "code_string": "ERR_CODE_UNSUPPORTED_CMD"},
        {"error_code": 1},
        {"code_string": "ERR_CODE_UNSUPPORTED_CMD"},
    ],
)
async def test_raw_send_names_the_firmware_limit_and_its_version(payload):
    radio = _Radio(SimpleNamespace(type=EventType.ERROR, payload=payload), "v1.9.0-abc")

    with pytest.raises(RawDataUnsupportedError) as exc_info:
        await send_raw_to_contact(radio, _DIRECT_CONTACT, b"payload")

    assert "v1.9.0-abc" in str(exc_info.value)
    assert "CMD_SEND_RAW_DATA" in str(exc_info.value)


async def test_raw_send_reports_any_other_rejection_as_a_plain_runtime_error():
    payload = {"error_code": 4, "code_string": "ERR_CODE_BAD_STATE"}
    radio = _Radio(SimpleNamespace(type=EventType.ERROR, payload=payload))

    with pytest.raises(RuntimeError) as exc_info:
        await send_raw_to_contact(radio, _DIRECT_CONTACT, b"payload")

    assert not isinstance(exc_info.value, RawDataUnsupportedError)
    # The image transport shares this sender, so the wording cannot say "voice".
    assert "raw data send failed" in str(exc_info.value)
    assert "voice" not in str(exc_info.value)


async def test_raw_send_reports_a_silent_radio_without_claiming_a_firmware_limit():
    radio = _Radio(None)

    with pytest.raises(RuntimeError) as exc_info:
        await send_raw_to_contact(radio, _DIRECT_CONTACT, b"payload")

    assert not isinstance(exc_info.value, RawDataUnsupportedError)
    assert "no radio response" in str(exc_info.value)


def _voice_message():
    return SimpleNamespace(
        id=43,
        type="PRIV",
        text="VE3:jbxb73:3:c:a",
        sender_name=None,
        sender_key="ab" * 32,
        conversation_key="ab" * 32,
        outgoing=True,
    )


def _voice_session(**overrides):
    session = {
        "session_id": "45abcdef",
        # Bound to a *different* message: the first one that carried this envelope.
        "message_id": 99,
        "state": "complete",
        "mode": 3,
        "duration_ms": 10_000,
        "packet_count": 12,
        "fragments": [(index, b"data") for index in range(12)],
    }
    session.update(overrides)
    return session


def _patch_voice_fetch(monkeypatch, session):
    message = _voice_message()

    async def get_message(_message_id):
        return message

    async def get_session(_session_id):
        return session

    monkeypatch.setattr("app.routers.voice.VoiceRepository.enforce_cache_limit", _async_noop)
    monkeypatch.setattr("app.routers.voice.VoiceRepository.upsert_session", _async_noop)
    monkeypatch.setattr("app.routers.voice.VoiceRepository.get", get_session)
    monkeypatch.setattr("app.routers.voice.MessageRepository.get_by_id", get_message)
    return message


async def test_voice_replays_a_recording_a_second_message_also_carries(monkeypatch):
    message = _patch_voice_fetch(monkeypatch, _voice_session())

    result = await fetch_voice(message.id)

    assert result["state"] == "complete"


async def test_voice_still_refuses_a_session_id_describing_another_recording(monkeypatch):
    message = _patch_voice_fetch(monkeypatch, _voice_session(packet_count=11))

    with pytest.raises(HTTPException) as exc_info:
        await fetch_voice(message.id)

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "voice session ID describes a different recording"


def _complete_session():
    return {
        "session_id": "45abcdef",
        # Bound to the OUTGOING message: this node sent the picture.
        "message_id": 1,
        "state": "complete",
        "format": 1,
        "width": 32,
        "height": 24,
        "size_bytes": 300,
        "fragment_count": 2,
        "fragments": [(0, b"a" * 152), (1, b"b" * 148)],
    }


async def test_opening_a_self_sent_image_needs_no_transfer_at_all(monkeypatch):
    """Send a picture to yourself and it arrives back as a second message row.

    Every fragment is already stored from the send, so opening the received copy
    must render straight from the cache -- not ask the mesh for what this node is
    holding, and not refuse because another message got there first.
    """

    async def get_message(_message_id):
        return _incoming_image_message()

    async def get_session(_session_id):
        return _complete_session()

    async def must_not_transmit(_radio_manager, _session):
        raise AssertionError("asked the mesh for fragments this node already holds")

    monkeypatch.setattr("app.routers.images.ImageRepository.enforce_cache_limit", _async_noop)
    monkeypatch.setattr("app.routers.images.ImageRepository.create_session", _async_noop)
    monkeypatch.setattr("app.routers.images.ImageRepository.get", get_session)
    monkeypatch.setattr("app.routers.images.MessageRepository.get_by_id", get_message)
    monkeypatch.setattr("app.routers.images.request_image_session", must_not_transmit)

    result = await fetch_image(7)

    assert result["state"] == "complete"
    assert result["received_count"] == 2
    assert result["missing_indices"] == []


async def test_sending_one_picture_repeatedly_keeps_every_copy_openable(test_db):
    """The flow that reported this: copy a message, send it again, open both."""
    import app.repository.image as image_module
    from app.repository.image import ImageRepository

    original = image_module.db
    image_module.db = test_db
    try:
        async with test_db.tx() as conn:
            for message_id in (1, 2, 3):
                await conn.execute(
                    "INSERT INTO messages (id, type, conversation_key, text, received_at, "
                    "outgoing) VALUES (?, 'PRIV', ?, ?, 1700000000, 1)",
                    (message_id, "aa" * 32, ENVELOPE),
                )
        shared = {
            "session_id": "45abcdef",
            "direction": "outgoing",
            "conversation_type": "PRIV",
            "conversation_key": "aa" * 32,
            "peer_public_key": "aa" * 32,
            "format_id": 1,
            "width": 32,
            "height": 24,
            "size_bytes": 300,
            "fragment_count": 2,
            "state": "complete",
            "ttl_seconds": 3600,
        }
        for message_id in (1, 2, 3):
            await ImageRepository.create_session(message_id=message_id, **shared)
        await ImageRepository.add_fragment("45abcdef", 0, b"a" * 152)
        await ImageRepository.add_fragment("45abcdef", 1, b"b" * 148)

        session = await ImageRepository.get("45abcdef")
        assert session is not None
        assert session["state"] == "complete"
        # One stored copy of the picture, not three.
        assert len(session["fragments"]) == 2
    finally:
        image_module.db = original
