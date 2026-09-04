"""What a push notification says when the message body is not words."""

import json

from app.push.manager import _build_payload


def _body(text: str, **overrides) -> str:
    data = {"type": "CHAN", "conversation_key": "AB" * 16, "channel_name": "#pics", "text": text}
    data.update(overrides)
    return json.loads(_build_payload(data))["body"]


class TestNotificationBody:
    def test_ordinary_text_is_the_body(self):
        assert _body("hello there") == "hello there"

    def test_a_picture_reads_as_a_picture(self):
        """The marker is a server-to-UI convention; a phone was showing it verbatim."""
        assert _body("aeib:grp:1c1e08f41fd4dd96") == "📷 Photo"

    def test_undecodable_media_reads_as_media(self):
        assert _body("mediax:17") == "📎 Media"

    def test_a_direct_message_is_treated_the_same(self):
        body = _body("aeib:out:m42", type="PRIV", sender_name="Alice")
        assert body == "📷 Photo"

    def test_a_missing_body_does_not_raise(self):
        assert _body(None) == ""  # type: ignore[arg-type]
