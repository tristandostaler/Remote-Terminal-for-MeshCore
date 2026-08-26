import aiosqlite


async def migrate(conn: aiosqlite.Connection) -> None:
    """Keep inbound media this build has no decoder for, so a later one can read it.

    An image arriving on a channel in a codec we do not have -- MCOimg today,
    whatever MCO Advanced ships next -- used to be identified, refused and
    dropped. Correct, and invisible: nothing in the conversation said a picture
    had been sent, and the bytes were gone, so adding the decoder later would not
    bring back a single image already received.

    The payloads are stored verbatim, in arrival order, because that is the only
    honest thing to do with a format we cannot parse: we do not know its chunking,
    so we cannot reassemble it, and any "helpful" normalisation would be a guess
    baked into storage. A future decoder gets exactly the blobs the radio handed
    us, in the order they arrived.

    **Nothing expires on a timer.** Media is pinned to the marker message that
    represents it in the conversation, the way migration 075 pins image and voice
    sessions, so ``ON DELETE CASCADE`` makes deleting that message the way to
    reclaim the space -- and the only way. That is a deliberate choice: the value
    of these bytes is precisely that they are still there when support arrives,
    which may be a long time, so a TTL would quietly defeat the feature. The cost
    is that a channel carrying foreign images grows this table until those
    messages are deleted.

    ``last_blob_at`` is what groups blobs into one picture. An unknown format
    gives us no image id and no chunk count, so consecutive blobs of the same type
    on the same channel are treated as one arrival while they keep coming; a gap
    means a new one. It is a heuristic, and it is confined to this column.
    """
    await conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS unsupported_media (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            conversation_key TEXT NOT NULL,
            data_type INTEGER NOT NULL,
            codec_label TEXT NOT NULL,
            received_at INTEGER NOT NULL,
            last_blob_at INTEGER NOT NULL,
            FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_unsupported_media_grouping
            ON unsupported_media(conversation_key, data_type, last_blob_at DESC);

        CREATE TABLE IF NOT EXISTS unsupported_media_blobs (
            media_id INTEGER NOT NULL,
            idx INTEGER NOT NULL,
            payload BLOB NOT NULL,
            PRIMARY KEY (media_id, idx),
            FOREIGN KEY (media_id) REFERENCES unsupported_media(id) ON DELETE CASCADE
        );
        """
    )
    await conn.commit()
