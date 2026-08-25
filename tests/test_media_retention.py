"""Media lives as long as the message that shows it.

Both caches used to be swept on a 24 h TTL and a newest-128 cap, so an image or
voice message older than that stayed readable in the conversation while the
picture or audio behind it was gone: a 404 on ``/content`` for your own
messages, and a fetch request the sender silently ignored for everyone else's.
"""

import app.repository.image as image_module
import app.repository.voice as voice_module
from app.repository.image import ImageRepository
from app.repository.voice import VoiceRepository

IMAGE_SESSION = {
    "direction": "outgoing",
    "conversation_type": "PRIV",
    "conversation_key": "aa" * 32,
    "peer_public_key": "aa" * 32,
    "format_id": 1,
    "width": 32,
    "height": 24,
    "size_bytes": 200,
    "fragment_count": 2,
    "state": "complete",
}
VOICE_SESSION = {
    "direction": "outgoing",
    "conversation_type": "PRIV",
    "conversation_key": "aa" * 32,
    "peer_public_key": "aa" * 32,
    "mode": 3,
    "duration_ms": 10_000,
    "packet_count": 2,
    "state": "complete",
}


async def _messages(db, *ids):
    async with db.tx() as conn:
        for message_id in ids:
            await conn.execute(
                "INSERT INTO messages (id, type, conversation_key, text, received_at, outgoing) "
                "VALUES (?, 'PRIV', ?, ?, 1700000000, 1)",
                (message_id, "aa" * 32, f"envelope-{message_id}"),
            )


async def _delete_message(db, message_id):
    async with db.tx() as conn:
        await conn.execute("DELETE FROM messages WHERE id=?", (message_id,))


async def test_an_expired_image_a_message_still_shows_is_kept(test_db):
    original = image_module.db
    image_module.db = test_db
    try:
        await _messages(test_db, 1)
        await ImageRepository.create_session(
            session_id="00112233", message_id=1, ttl_seconds=-1, **IMAGE_SESSION
        )
        await ImageRepository.add_fragment("00112233", 0, b"a" * 152)

        await ImageRepository.enforce_cache_limit()

        session = await ImageRepository.get("00112233")
        assert session is not None
        assert len(session["fragments"]) == 1
    finally:
        image_module.db = original


async def test_deleting_the_message_releases_the_image_again(test_db):
    original = image_module.db
    image_module.db = test_db
    try:
        await _messages(test_db, 1)
        await ImageRepository.create_session(
            session_id="00112233", message_id=1, ttl_seconds=-1, **IMAGE_SESSION
        )
        await ImageRepository.add_fragment("00112233", 0, b"a" * 152)

        await _delete_message(test_db, 1)
        await ImageRepository.enforce_cache_limit()

        assert await ImageRepository.get("00112233") is None
        async with test_db.readonly() as conn:
            async with conn.execute("SELECT COUNT(*) FROM image_fragments") as cursor:
                row = await cursor.fetchone()
        assert row is not None and row[0] == 0
    finally:
        image_module.db = original


async def test_one_surviving_copy_is_enough_to_keep_the_picture(test_db):
    """The reason references live in their own table rather than on the session.

    Three messages carry one envelope; the session records only the first. If
    retention followed that single id, deleting the original would drop the
    picture while the two copies still showed it in the conversation.
    """
    original = image_module.db
    image_module.db = test_db
    try:
        await _messages(test_db, 1, 2, 3)
        for message_id in (1, 2, 3):
            await ImageRepository.create_session(
                session_id="00112233", message_id=message_id, ttl_seconds=-1, **IMAGE_SESSION
            )

        await _delete_message(test_db, 1)
        await ImageRepository.enforce_cache_limit()
        assert await ImageRepository.get("00112233") is not None

        await _delete_message(test_db, 2)
        await ImageRepository.enforce_cache_limit()
        assert await ImageRepository.get("00112233") is not None

        await _delete_message(test_db, 3)
        await ImageRepository.enforce_cache_limit()
        assert await ImageRepository.get("00112233") is None
    finally:
        image_module.db = original


async def test_media_no_message_references_is_still_swept_by_age_and_count(test_db):
    """The old cache behaviour has to survive for sessions nothing shows."""
    original = image_module.db
    image_module.db = test_db
    try:
        await ImageRepository.create_session(
            session_id="expired0", message_id=None, ttl_seconds=-1, **IMAGE_SESSION
        )
        for index in range(3):
            await ImageRepository.create_session(
                session_id=f"live0{index:03d}", message_id=None, ttl_seconds=3600, **IMAGE_SESSION
            )

        surplus = await ImageRepository.enforce_cache_limit(max_sessions=2)

        assert await ImageRepository.get("expired0") is None
        assert surplus == 1
    finally:
        image_module.db = original


async def test_the_cap_counts_only_sessions_nothing_references(test_db):
    """A pinned session must not use up the unreferenced cache's budget.

    Capping pinned sessions would put a hard ceiling on how far back the
    conversation can be read, which is exactly what this is meant to remove.
    """
    original = image_module.db
    image_module.db = test_db
    try:
        await _messages(test_db, 1)
        await ImageRepository.create_session(
            session_id="pinned00", message_id=1, ttl_seconds=3600, **IMAGE_SESSION
        )
        for index in range(2):
            await ImageRepository.create_session(
                session_id=f"loose0{index:02d}", message_id=None, ttl_seconds=3600, **IMAGE_SESSION
            )

        assert await ImageRepository.enforce_cache_limit(max_sessions=2) == 0
        assert await ImageRepository.get("pinned00") is not None
    finally:
        image_module.db = original


async def test_an_expired_recording_a_message_still_plays_is_kept(test_db):
    original = voice_module.db
    voice_module.db = test_db
    try:
        await _messages(test_db, 1)
        await VoiceRepository.upsert_session(
            session_id="00112233", message_id=1, ttl_seconds=-1, **VOICE_SESSION
        )
        await VoiceRepository.add_fragment("00112233", 0, b"a" * 20)

        await VoiceRepository.enforce_cache_limit()

        session = await VoiceRepository.get("00112233")
        assert session is not None
        assert len(session["fragments"]) == 1
    finally:
        voice_module.db = original


async def test_deleting_the_message_releases_the_recording_again(test_db):
    original = voice_module.db
    voice_module.db = test_db
    try:
        await _messages(test_db, 1)
        await VoiceRepository.upsert_session(
            session_id="00112233", message_id=1, ttl_seconds=-1, **VOICE_SESSION
        )

        await _delete_message(test_db, 1)
        await VoiceRepository.enforce_cache_limit()

        assert await VoiceRepository.get("00112233") is None
    finally:
        voice_module.db = original


async def test_a_reference_outliving_its_session_is_cleaned_up(test_db):
    """Nothing else removes these: the cascade only fires from the messages side."""
    original = image_module.db
    image_module.db = test_db
    try:
        await _messages(test_db, 1)
        async with test_db.tx() as conn:
            await conn.execute(
                "INSERT INTO media_session_messages (kind, session_id, message_id) "
                "VALUES ('image', 'ghost000', 1)"
            )

        await ImageRepository.enforce_cache_limit()

        async with test_db.readonly() as conn:
            async with conn.execute("SELECT COUNT(*) FROM media_session_messages") as cursor:
                row = await cursor.fetchone()
        assert row is not None and row[0] == 0
    finally:
        image_module.db = original
