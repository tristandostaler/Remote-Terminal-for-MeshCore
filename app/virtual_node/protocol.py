"""Companion wire-protocol codecs for the virtual node.

Everything in here is a pure function over bytes: framing, the response
frames the virtual node synthesizes from RemoteTerm's own state, and the
parsers for the handful of host commands it interprets locally. The byte
layouts mirror ``meshcore/reader.py`` (radio -> host) and
``meshcore/commands/*.py`` (host -> radio) from the vendored library, which
are themselves transcriptions of the firmware's ``MyMesh.cpp``.

Frame codes come from ``meshcore.packets.CommandType`` / ``PacketType`` where
the library defines them; the few it does not are spelled out here.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from meshcore.packets import CommandType, PacketType

from app.models import Channel, Contact

# --- Stream framing --------------------------------------------------------

FRAME_HOST_TO_RADIO = 0x3C  # '<' : command frames sent by an app
FRAME_RADIO_TO_HOST = 0x3E  # '>' : response/push frames sent by the radio
# The library's readers drop anything over 300; the largest real frame (a
# contact update) is 144 bytes.
MAX_FRAME_PAYLOAD = 300

# Frame codes the library does not enumerate.
RESP_CODE_CHANNEL_DATA_RECV = 27
PUSH_CODE_MSG_WAITING = PacketType.MESSAGES_WAITING.value
PUSH_CODE_ADVERT = PacketType.ADVERTISEMENT.value
PUSH_CODE_NEW_ADVERT = PacketType.PUSH_CODE_NEW_ADVERT.value

# Command codes the library's ``CommandType`` leaves out.
CMD_GET_CUSTOM_VARS = CommandType.GET_CUSTOM_VARS.value
CMD_SEND_CHANNEL_DATA = 62
"""``CMD_SEND_CHANNEL_DATA`` -- a channel message that is a binary blob.

Not in ``CommandType``, and not a niche command: MCO Advanced puts every image
on the air with it, and every compressed channel message too while its
``channelsSendAsBinary`` setting is on -- which is the default. Same value as
:data:`app.imaging.aeic.channel_data.CMD_SEND_CHANNEL_DATA`, which owns the
blob framing; this side only needs the command envelope.
"""


class ErrorCode(IntEnum):
    """``ERR_CODE_*`` values from the firmware, as sent in ``RESP_CODE_ERR``."""

    UNSUPPORTED_CMD = 1
    NOT_FOUND = 2
    TABLE_FULL = 3
    BAD_STATE = 4
    FILE_IO_ERROR = 5
    ILLEGAL_ARG = 6


def frame_to_host(payload: bytes) -> bytes:
    """Wrap a response/push payload the way the radio does on TCP/serial."""
    return bytes([FRAME_RADIO_TO_HOST]) + len(payload).to_bytes(2, "little") + payload


class HostFrameParser:
    """Reassemble ``<``-framed command payloads from a TCP byte stream.

    Mirror of ``TCPConnection.handle_rx`` for the opposite direction: it
    tolerates fragmentation, coalesced frames, and junk between frames by
    hunting for the start marker.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer.extend(data)
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(bytes([FRAME_HOST_TO_RADIO]))
            if start < 0:
                self._buffer.clear()
                break
            if start > 0:
                del self._buffer[:start]
            if len(self._buffer) < 3:
                break
            size = int.from_bytes(self._buffer[1:3], "little")
            if size == 0 or size > MAX_FRAME_PAYLOAD:
                # Not a real header: drop the marker and keep hunting.
                del self._buffer[:1]
                continue
            if len(self._buffer) < 3 + size:
                break
            frames.append(bytes(self._buffer[3 : 3 + size]))
            del self._buffer[: 3 + size]
        return frames


# --- Simple response frames -------------------------------------------------


def encode_ok(value: int | None = None) -> bytes:
    if value is None:
        return bytes([PacketType.OK.value])
    return bytes([PacketType.OK.value]) + int(value).to_bytes(4, "little")


def encode_error(code: ErrorCode | int) -> bytes:
    return bytes([PacketType.ERROR.value, int(code) & 0xFF])


def encode_disabled() -> bytes:
    return bytes([PacketType.DISABLED.value])


def encode_no_more_messages() -> bytes:
    return bytes([PacketType.NO_MORE_MSGS.value])


def encode_current_time(epoch: int) -> bytes:
    return bytes([PacketType.CURRENT_TIME.value]) + int(epoch).to_bytes(4, "little")


def encode_battery(level_mv: int, used_kb: int | None = None, total_kb: int | None = None) -> bytes:
    frame = bytes([PacketType.BATTERY.value]) + max(0, min(0xFFFF, int(level_mv))).to_bytes(
        2, "little"
    )
    if used_kb is not None and total_kb is not None:
        frame += int(used_kb).to_bytes(4, "little") + int(total_kb).to_bytes(4, "little")
    return frame


def encode_msg_sent(*, is_flood: bool, expected_ack: bytes, suggested_timeout_ms: int) -> bytes:
    ack = (expected_ack or b"\x00\x00\x00\x00")[:4].ljust(4, b"\x00")
    return (
        bytes([PacketType.MSG_SENT.value, 1 if is_flood else 0])
        + ack
        + max(0, int(suggested_timeout_ms)).to_bytes(4, "little")
    )


def encode_push_advert(public_key_hex: str) -> bytes:
    return bytes([PUSH_CODE_ADVERT]) + bytes.fromhex(public_key_hex)[:32].ljust(32, b"\x00")


def encode_push_msg_waiting() -> bytes:
    return bytes([PUSH_CODE_MSG_WAITING])


# --- Self / device info -----------------------------------------------------


def encode_self_info(info: dict) -> bytes:
    """Rebuild ``RESP_CODE_SELF_INFO`` from the dict ``MessageReader`` parsed.

    The reader consumes the whole frame (the name is ``read()`` to the end),
    so this is a lossless inverse for every field the library knows about.
    """
    telemetry_mode = (
        (int(info.get("telemetry_mode_base", 0)) & 0b11)
        | ((int(info.get("telemetry_mode_loc", 0)) & 0b11) << 2)
        | ((int(info.get("telemetry_mode_env", 0)) & 0b11) << 4)
    )
    return (
        bytes(
            [
                PacketType.SELF_INFO.value,
                int(info.get("adv_type", 1)) & 0xFF,
                int(info.get("tx_power", 0)) & 0xFF,
                int(info.get("max_tx_power", 0)) & 0xFF,
            ]
        )
        + bytes.fromhex(info.get("public_key", "")).ljust(32, b"\x00")[:32]
        + _coord_bytes(info.get("adv_lat"))
        + _coord_bytes(info.get("adv_lon"))
        + bytes(
            [
                int(info.get("multi_acks", 0)) & 0xFF,
                int(info.get("adv_loc_policy", 0)) & 0xFF,
                telemetry_mode,
                1 if info.get("manual_add_contacts") else 0,
            ]
        )
        + int(round(float(info.get("radio_freq", 0.0)) * 1000)).to_bytes(4, "little")
        + int(round(float(info.get("radio_bw", 0.0)) * 1000)).to_bytes(4, "little")
        + bytes([int(info.get("radio_sf", 0)) & 0xFF, int(info.get("radio_cr", 0)) & 0xFF])
        + str(info.get("name", "")).encode("utf-8")
    )


DEVICE_INFO_MAX_CHANNELS_OFFSET = 3


def rewrite_device_info_max_channels(frame: bytes, max_channels: int) -> bytes:
    """Return a ``RESP_CODE_DEVICE_INFO`` frame advertising a different slot count.

    Apps enumerate channels up to this number. The virtual node presents the
    server's whole channel list, which can exceed the radio's physical slots.
    """
    if len(frame) <= DEVICE_INFO_MAX_CHANNELS_OFFSET or frame[1] < 3:
        return frame
    out = bytearray(frame)
    out[DEVICE_INFO_MAX_CHANNELS_OFFSET] = max(0, min(255, int(max_channels)))
    return bytes(out)


# --- Contacts ---------------------------------------------------------------


def _coord_bytes(value) -> bytes:
    try:
        scaled = int(round(float(value) * 1e6)) if value is not None else 0
    except (TypeError, ValueError):
        scaled = 0
    scaled = max(-(2**31), min(2**31 - 1, scaled))
    return scaled.to_bytes(4, "little", signed=True)


def _fixed(data: bytes, size: int) -> bytes:
    return data[:size].ljust(size, b"\x00")


def _route_byte(path_len: int | None, hash_mode: int | None) -> int:
    if path_len is None or path_len < 0:
        return 0xFF
    mode = hash_mode if hash_mode is not None and hash_mode >= 0 else 0
    return (min(int(path_len), 0x3F)) | ((mode & 0x03) << 6)


def contact_lastmod(contact: Contact) -> int:
    """The freshest timestamp RemoteTerm holds for a contact.

    The firmware's ``lastmod`` is a write clock over its contact table; ours
    is derived, so ``GET_CONTACTS since`` sees a contact again whenever any
    fact about it changed.
    """
    candidates = [
        contact.last_advert,
        contact.last_seen,
        contact.direct_path_updated_at,
        contact.last_contacted,
        contact.first_seen,
    ]
    return max((int(v) for v in candidates if v is not None), default=0)


def is_full_public_key(public_key: str | None) -> bool:
    if not public_key or len(public_key) != 64:
        return False
    try:
        bytes.fromhex(public_key)
    except ValueError:
        return False
    return True


def encode_contact(contact: Contact, *, lastmod: int | None = None) -> bytes:
    """Encode a stored contact as a ``RESP_CODE_CONTACT`` frame.

    Inverse of the reader's ``PacketType.CONTACT`` branch. The route sent is
    the contact's *effective* route (override > learned direct > flood), so an
    app sees the same path RemoteTerm would use for a DM. The favorite flag
    mirrors the app-side favorite into the firmware's bit 0.
    """
    path, path_len, hash_mode = contact.effective_route_tuple()
    path_bytes = bytes.fromhex(path) if path else b""
    flags = int(contact.flags) & 0xFF
    if contact.favorite:
        flags |= 0x01
    return (
        bytes([PacketType.CONTACT.value])
        + _fixed(bytes.fromhex(contact.public_key), 32)
        + bytes([int(contact.type) & 0xFF, flags, _route_byte(path_len, hash_mode)])
        + _fixed(path_bytes, 64)
        + _fixed((contact.name or "").encode("utf-8"), 32)
        + int(contact.last_advert or 0).to_bytes(4, "little")
        + _coord_bytes(contact.lat)
        + _coord_bytes(contact.lon)
        + int(lastmod if lastmod is not None else contact_lastmod(contact)).to_bytes(4, "little")
    )


def encode_contact_start(count: int) -> bytes:
    return bytes([PacketType.CONTACT_START.value]) + int(count).to_bytes(4, "little")


def encode_contact_end(lastmod: int) -> bytes:
    return bytes([PacketType.CONTACT_END.value]) + int(lastmod).to_bytes(4, "little")


@dataclass(slots=True)
class ContactUpdate:
    """Decoded ``CMD_ADD_UPDATE_CONTACT`` (the app's add/edit of a contact)."""

    public_key: str
    type: int
    flags: int
    out_path: str
    out_path_len: int
    out_path_hash_mode: int
    adv_name: str
    last_advert: int
    adv_lat: float
    adv_lon: float

    def to_radio_dict(self) -> dict:
        """Shape ``ContactUpsert.from_radio_dict`` expects."""
        return {
            "public_key": self.public_key,
            "type": self.type,
            "flags": self.flags,
            "out_path": self.out_path,
            "out_path_len": self.out_path_len,
            "out_path_hash_mode": self.out_path_hash_mode,
            "adv_name": self.adv_name,
            "last_advert": self.last_advert,
            "adv_lat": self.adv_lat,
            "adv_lon": self.adv_lon,
        }


def parse_contact_update(payload: bytes) -> ContactUpdate | None:
    """Decode the body of ``CMD_ADD_UPDATE_CONTACT`` (code byte excluded)."""
    if len(payload) < 32 + 1 + 1 + 1 + 64 + 32 + 4 + 4 + 4:
        return None
    public_key = payload[0:32].hex()
    contact_type = payload[32]
    flags = payload[33]
    route = payload[34]
    if route == 0xFF:
        out_path_len, hash_mode = -1, -1
        out_path = ""
    else:
        out_path_len = route & 0x3F
        hash_mode = route >> 6
        out_path = payload[35 : 35 + out_path_len * (hash_mode + 1)].hex()
    name = payload[99:131].split(b"\x00", 1)[0].decode("utf-8", "ignore")
    last_advert = int.from_bytes(payload[131:135], "little")
    lat = int.from_bytes(payload[135:139], "little", signed=True) / 1e6
    lon = int.from_bytes(payload[139:143], "little", signed=True) / 1e6
    return ContactUpdate(
        public_key=public_key,
        type=contact_type,
        flags=flags,
        out_path=out_path,
        out_path_len=out_path_len,
        out_path_hash_mode=hash_mode,
        adv_name=name,
        last_advert=last_advert,
        adv_lat=lat,
        adv_lon=lon,
    )


def parse_get_contacts_since(payload: bytes) -> int:
    """``CMD_GET_CONTACTS`` carries an optional 4-byte ``since`` lastmod."""
    if len(payload) >= 4:
        return int.from_bytes(payload[:4], "little")
    return 0


def parse_public_key_arg(payload: bytes) -> str | None:
    """Commands addressed to a contact carry its 32-byte key right after the code."""
    if len(payload) < 32:
        return None
    return payload[:32].hex()


# --- Channels ---------------------------------------------------------------

CHANNEL_NAME_SIZE = 32
CHANNEL_SECRET_SIZE = 16
EMPTY_CHANNEL_SECRET = bytes(CHANNEL_SECRET_SIZE)


def encode_channel_info(index: int, channel: Channel | None) -> bytes:
    """``RESP_CODE_CHANNEL_INFO`` for a virtual slot; an empty slot reads as blank."""
    if channel is None:
        name = b""
        secret = EMPTY_CHANNEL_SECRET
    else:
        name = channel.name.encode("utf-8")
        secret = bytes.fromhex(channel.key)
    return (
        bytes([PacketType.CHANNEL_INFO.value, int(index) & 0xFF])
        + _fixed(name, CHANNEL_NAME_SIZE)
        + _fixed(secret, CHANNEL_SECRET_SIZE)
    )


@dataclass(slots=True)
class ChannelUpdate:
    """Decoded ``CMD_SET_CHANNEL``."""

    index: int
    name: str
    secret: bytes

    @property
    def is_clear(self) -> bool:
        return not self.name and self.secret == EMPTY_CHANNEL_SECRET

    @property
    def key(self) -> str:
        return self.secret.hex().upper()


def parse_channel_update(payload: bytes) -> ChannelUpdate | None:
    if len(payload) < 1 + CHANNEL_NAME_SIZE + CHANNEL_SECRET_SIZE:
        return None
    index = payload[0]
    name = payload[1 : 1 + CHANNEL_NAME_SIZE].split(b"\x00", 1)[0].decode("utf-8", "ignore")
    secret = bytes(payload[1 + CHANNEL_NAME_SIZE : 1 + CHANNEL_NAME_SIZE + CHANNEL_SECRET_SIZE])
    return ChannelUpdate(index=index, name=name, secret=secret)


# --- Messages ---------------------------------------------------------------


@dataclass(slots=True)
class OutgoingTextMessage:
    """Decoded ``CMD_SEND_TXT_MSG``."""

    txt_type: int
    attempt: int
    timestamp: int
    pubkey_prefix: str  # 12 hex chars (6 bytes)
    text: str


def parse_send_txt_msg(payload: bytes) -> OutgoingTextMessage | None:
    """Decode the body of ``CMD_SEND_TXT_MSG`` (code byte excluded).

    The text is NUL-terminated on this command (and only this one): apps append
    the byte because the firmware reads the field as a C string and drops it
    again. Nothing downstream here does -- the text is stored as a message and
    re-encoded for the radio -- so it is stripped at the parse, where the wire
    format is already the subject.
    """
    if len(payload) < 1 + 1 + 4 + 6:
        return None
    return OutgoingTextMessage(
        txt_type=payload[0],
        attempt=payload[1],
        timestamp=int.from_bytes(payload[2:6], "little"),
        pubkey_prefix=payload[6:12].hex(),
        text=payload[12:].split(b"\x00", 1)[0].decode("utf-8", "ignore"),
    )


@dataclass(slots=True)
class OutgoingChannelMessage:
    """Decoded ``CMD_SEND_CHANNEL_TXT_MSG``."""

    txt_type: int
    channel_index: int
    timestamp: int
    text: str


def parse_send_channel_txt_msg(payload: bytes) -> OutgoingChannelMessage | None:
    if len(payload) < 1 + 1 + 4:
        return None
    return OutgoingChannelMessage(
        txt_type=payload[0],
        channel_index=payload[1],
        timestamp=int.from_bytes(payload[2:6], "little"),
        text=payload[6:].decode("utf-8", "ignore"),
    )


@dataclass(slots=True)
class OutgoingChannelData:
    """Decoded ``CMD_SEND_CHANNEL_DATA`` -- an app's binary channel payload.

    ``[channel_idx][path_len][path?][data_type u16][blob]``. ``path`` is
    normally absent (``path_len`` 0xFF asks the firmware to flood), and
    RemoteTerm re-sends the blob under its own routing anyway, so it is kept
    only so the trace can say the app asked for something else.
    """

    channel_index: int
    path_len_byte: int
    path: bytes
    data_type: int
    blob: bytes


def parse_send_channel_data(payload: bytes) -> OutgoingChannelData | None:
    """Decode the body of ``CMD_SEND_CHANNEL_DATA`` (code byte excluded)."""
    if len(payload) < 2:
        return None
    channel_index = payload[0]
    path_len_byte = payload[1]
    path_bytes = 0 if path_len_byte == 0xFF else path_len_byte
    type_at = 2 + path_bytes
    if len(payload) < type_at + 2:
        return None
    return OutgoingChannelData(
        channel_index=channel_index,
        path_len_byte=path_len_byte,
        path=bytes(payload[2:type_at]),
        data_type=payload[type_at] | (payload[type_at + 1] << 8),
        blob=bytes(payload[type_at + 2 :]),
    )


CHANNEL_DATA_RECV_INDEX_OFFSET = 4
CHANNEL_DATA_RECV_HEADER_BYTES = 9


def encode_channel_data_recv(
    channel_index: int, data_type: int, blob: bytes, *, snr: float | None = None
) -> bytes:
    """Build a ``RESP_CODE_CHANNEL_DATA_RECV`` (27) frame for a client.

    ``[27][snr][2 reserved][channel_idx][path_len][type u16][len][blob]`` -- the
    inverse of ``channel_data.parse_channel_data_frame``. Used for the blobs
    RemoteTerm itself puts on the air, which no radio hands back: without it an
    image sent from the web UI is invisible to every app on the node.
    """
    return (
        bytes([RESP_CODE_CHANNEL_DATA_RECV])
        + _snr_byte(snr)
        + b"\x00\x00"
        + bytes(
            [
                int(channel_index) & 0xFF,
                0xFF,
                data_type & 0xFF,
                (data_type >> 8) & 0xFF,
                len(blob) & 0xFF,
            ]
        )
        + blob
    )


def rewrite_channel_data_index(frame: bytes, channel_index: int) -> bytes:
    """Re-address an inbound frame 27 to a client's virtual channel slot.

    The frame names the RADIO's slot, and apps on the node address channels by
    the virtual slot table, so relaying it unchanged points the image at
    whatever the app has at that index -- the same class of mistake as sending a
    channel message to the wrong slot, and just as invisible.
    """
    if len(frame) <= CHANNEL_DATA_RECV_INDEX_OFFSET:
        return frame
    out = bytearray(frame)
    out[CHANNEL_DATA_RECV_INDEX_OFFSET] = int(channel_index) & 0xFF
    return bytes(out)


def rewrite_channel_data(frame: bytes, *, channel_index: int, blob: bytes) -> bytes:
    """Re-address an inbound frame 27 AND replace the blob it carries.

    Used when the payload a client should see is not the one the radio delivered
    -- today only to relabel the sender prefix of a chunk this node sent, which
    an app would otherwise discard as its own echo. The SNR and hop bytes are
    kept from the original frame, because those facts are still true.
    """
    if len(frame) < CHANNEL_DATA_RECV_HEADER_BYTES:
        return frame
    head = bytearray(frame[:CHANNEL_DATA_RECV_HEADER_BYTES])
    head[CHANNEL_DATA_RECV_INDEX_OFFSET] = int(channel_index) & 0xFF
    head[CHANNEL_DATA_RECV_HEADER_BYTES - 1] = len(blob) & 0xFF
    return bytes(head) + bytes(blob)


def _snr_byte(snr: float | None) -> bytes:
    if snr is None:
        return b"\x00"
    scaled = max(-128, min(127, int(round(float(snr) * 4))))
    return scaled.to_bytes(1, "little", signed=True)


def _message_route_byte(message: dict) -> int:
    """Hop-count byte for a stored message: the newest path, direct when unknown."""
    paths = message.get("paths") or []
    if not paths:
        return 0xFF
    last = paths[-1] or {}
    path_len = last.get("path_len")
    path_hex = last.get("path") or ""
    if path_len is None:
        path_len = len(path_hex) // 2
    return min(int(path_len), 0x3F)


def _message_snr(message: dict) -> float | None:
    paths = message.get("paths") or []
    if not paths:
        return None
    return (paths[-1] or {}).get("snr")


def encode_contact_message(message: dict) -> bytes | None:
    """``RESP_CODE_CONTACT_MSG_RECV_V3`` for a stored incoming direct message.

    ``message`` is the ``Message`` payload RemoteTerm broadcasts. The reply
    to ``CMD_SYNC_NEXT_MESSAGE`` on protocol v3+ carries SNR and two reserved
    bytes ahead of the classic layout.
    """
    conversation_key = str(message.get("conversation_key") or "")
    if len(conversation_key) < 12:
        return None
    try:
        prefix = bytes.fromhex(conversation_key[:12])
    except ValueError:
        return None
    txt_type = int(message.get("txt_type") or 0) & 0xFF
    frame = (
        bytes([PacketType.CONTACT_MSG_RECV_V3.value])
        + _snr_byte(_message_snr(message))
        + b"\x00\x00"
        + prefix
        + bytes([_message_route_byte(message), txt_type])
        + int(message.get("sender_timestamp") or message.get("received_at") or 0).to_bytes(
            4, "little"
        )
    )
    if txt_type == 2:
        signature = str(message.get("signature") or "")
        try:
            sig_bytes = bytes.fromhex(signature) if signature else b""
        except ValueError:
            sig_bytes = b""
        frame += _fixed(sig_bytes, 4)
    return frame + str(message.get("text") or "").encode("utf-8")


def encode_channel_message(message: dict, channel_index: int) -> bytes:
    """``RESP_CODE_CHANNEL_MSG_RECV_V3`` for a stored channel message on a virtual slot."""
    return (
        bytes([PacketType.CHANNEL_MSG_RECV_V3.value])
        + _snr_byte(_message_snr(message))
        + b"\x00\x00"
        + bytes(
            [
                int(channel_index) & 0xFF,
                _message_route_byte(message),
                int(message.get("txt_type") or 0) & 0xFF,
            ]
        )
        + int(message.get("sender_timestamp") or message.get("received_at") or 0).to_bytes(
            4, "little"
        )
        + str(message.get("text") or "").encode("utf-8")
    )


def pulled_message_txt_type(frame: bytes) -> int | None:
    """``txt_type`` of a ``CONTACT_MSG_RECV`` / ``_V3`` frame the radio handed us."""
    code = frame[0] if frame else None
    if code == PacketType.CONTACT_MSG_RECV.value and len(frame) >= 9:
        return frame[8]
    if code == PacketType.CONTACT_MSG_RECV_V3.value and len(frame) >= 12:
        return frame[11]
    return None


def stats_frame_type(payload: bytes) -> int | None:
    """Sub-type byte of ``CMD_GET_STATS`` (0 core, 1 radio, 2 packets)."""
    return payload[0] if payload else None


def unpack_u32(data: bytes) -> int:
    return struct.unpack("<I", data[:4].ljust(4, b"\x00"))[0]
