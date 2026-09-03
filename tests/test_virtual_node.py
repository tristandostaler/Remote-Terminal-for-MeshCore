"""Virtual MeshCore companion node: wire codecs and the TCP proxy server."""

import asyncio
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from meshcore.events import Event, EventType
from meshcore.packets import CommandType, PacketType
from meshcore.reader import MessageReader

from app.channel_constants import PUBLIC_CHANNEL_KEY
from app.models import ContactUpsert
from app.repository import ChannelRepository, ContactRepository, MessageRepository
from app.services import dm_ack_tracker
from app.virtual_node import protocol
from app.virtual_node.protocol import ErrorCode
from app.virtual_node.server import VirtualNodeServer, install_frame_tap

CMD = CommandType
RESP = PacketType

KEY_A = "aa" * 32
KEY_B = "bb" * 32
CHANNEL_KEY = "8b3387e9c5cdea6ac9e5edbaa115cd72".upper()


class RecordingDispatcher:
    """Stand-in for the library's EventDispatcher that just collects events."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    async def dispatch(self, event: Event) -> None:
        self.events.append(event)


async def parse_with_library(frame: bytes) -> list[Event]:
    """Run a frame through meshcore's own reader and return what it dispatched."""
    dispatcher = RecordingDispatcher()
    reader = MessageReader(dispatcher)
    await reader.handle_rx(bytearray(frame))
    return dispatcher.events


# --------------------------------------------------------------------------- codecs


class TestHostFrameParser:
    def test_reassembles_fragments_and_coalesced_frames(self):
        parser = protocol.HostFrameParser()
        frame_a = b"<" + (2).to_bytes(2, "little") + b"\x16\x03"
        frame_b = b"<" + (1).to_bytes(2, "little") + b"\x04"
        stream = frame_a + frame_b
        assert parser.feed(stream[:2]) == []
        assert parser.feed(stream[2:4]) == []
        assert parser.feed(stream[4:5]) == [b"\x16\x03"]
        assert parser.feed(stream[5:]) == [b"\x04"]

    def test_skips_junk_before_the_start_marker(self):
        parser = protocol.HostFrameParser()
        junk = b"hello\r\n"
        frame = b"<" + (1).to_bytes(2, "little") + b"\x05"
        assert parser.feed(junk + frame) == [b"\x05"]

    def test_rejects_absurd_lengths_and_resyncs(self):
        parser = protocol.HostFrameParser()
        bogus = b"<" + (5000).to_bytes(2, "little")
        frame = b"<" + (1).to_bytes(2, "little") + b"\x05"
        assert parser.feed(bogus + frame) == [b"\x05"]


class TestIdentityFrames:
    @pytest.mark.asyncio
    async def test_self_info_round_trips_through_library_reader(self):
        info = {
            "adv_type": 1,
            "tx_power": 20,
            "max_tx_power": 22,
            "public_key": KEY_A,
            "adv_lat": 45.5017,
            "adv_lon": -73.5673,
            "multi_acks": 1,
            "adv_loc_policy": 1,
            "telemetry_mode_env": 2,
            "telemetry_mode_loc": 1,
            "telemetry_mode_base": 3,
            "manual_add_contacts": True,
            "radio_freq": 910.525,
            "radio_bw": 62.5,
            "radio_sf": 7,
            "radio_cr": 5,
            "name": "RemoteTerm",
        }
        events = await parse_with_library(protocol.encode_self_info(info))
        assert [e.type for e in events] == [EventType.SELF_INFO]
        parsed = events[0].payload
        assert parsed["public_key"] == KEY_A
        assert parsed["name"] == "RemoteTerm"
        assert parsed["radio_freq"] == pytest.approx(910.525)
        assert parsed["radio_bw"] == pytest.approx(62.5)
        assert parsed["adv_lat"] == pytest.approx(45.5017, abs=1e-6)
        assert parsed["adv_lon"] == pytest.approx(-73.5673, abs=1e-6)
        assert parsed["telemetry_mode_env"] == 2
        assert parsed["telemetry_mode_loc"] == 1
        assert parsed["telemetry_mode_base"] == 3
        assert parsed["manual_add_contacts"] is True
        assert parsed["multi_acks"] == 1

    def test_rewrite_device_info_max_channels(self):
        frame = bytes([RESP.DEVICE_INFO.value, 10, 200 // 2, 40]) + bytes(78)
        rewritten = protocol.rewrite_device_info_max_channels(frame, 120)
        assert rewritten[3] == 120
        assert rewritten[:3] == frame[:3]
        assert rewritten[4:] == frame[4:]
        # Pre-v3 frames carry no capacity fields; leave them alone.
        assert protocol.rewrite_device_info_max_channels(bytes([13, 2]), 120) == bytes([13, 2])


class TestContactFrames:
    @pytest.mark.asyncio
    async def test_contact_frame_round_trips_through_library_reader(self):
        from app.models import Contact

        contact = Contact(
            public_key=KEY_A,
            name="Alice",
            type=1,
            flags=0,
            direct_path="1a2b",
            direct_path_len=2,
            direct_path_hash_mode=0,
            last_advert=1_700_000_000,
            lat=45.5,
            lon=-73.5,
            last_seen=1_700_000_100,
            favorite=True,
        )
        events = await parse_with_library(protocol.encode_contact(contact))
        assert [e.type for e in events] == [EventType.NEXT_CONTACT]
        parsed = events[0].payload
        assert parsed["public_key"] == KEY_A
        assert parsed["adv_name"] == "Alice"
        assert parsed["type"] == 1
        assert parsed["flags"] & 0x01, "favorite mirrors into the firmware flag bit"
        assert parsed["out_path_len"] == 2
        assert parsed["out_path_hash_mode"] == 0
        assert parsed["out_path"] == "1a2b"
        assert parsed["last_advert"] == 1_700_000_000
        assert parsed["adv_lat"] == pytest.approx(45.5)
        assert parsed["adv_lon"] == pytest.approx(-73.5)
        assert parsed["lastmod"] == 1_700_000_100

    @pytest.mark.asyncio
    async def test_flood_contact_encodes_route_byte_255(self):
        from app.models import Contact

        contact = Contact(public_key=KEY_B, name="Bob", type=2)
        events = await parse_with_library(protocol.encode_contact(contact))
        assert events[0].payload["out_path_len"] == -1
        assert events[0].payload["out_path_hash_mode"] == -1

    def test_parse_contact_update_matches_library_encoding(self):
        # Mirror of ContactCommands.update_contact: 32 key, type, flags, route
        # byte, 64 path, 32 name, last_advert, lat, lon.
        route = 2 | (1 << 6)  # two hops at 2-byte hashes
        payload = (
            bytes.fromhex(KEY_A)
            + bytes([3, 1, route])
            + bytes.fromhex("aabbccdd").ljust(64, b"\x00")
            + b"Room".ljust(32, b"\x00")
            + (1_700_000_000).to_bytes(4, "little")
            + int(45.5 * 1e6).to_bytes(4, "little", signed=True)
            + int(-73.5 * 1e6).to_bytes(4, "little", signed=True)
        )
        update = protocol.parse_contact_update(payload)
        assert update is not None
        assert update.public_key == KEY_A
        assert update.type == 3
        assert update.flags == 1
        assert update.out_path_len == 2
        assert update.out_path_hash_mode == 1
        assert update.out_path == "aabbccdd"
        assert update.adv_name == "Room"
        assert update.adv_lat == pytest.approx(45.5)
        assert update.adv_lon == pytest.approx(-73.5)
        upsert = ContactUpsert.from_radio_dict(update.public_key, update.to_radio_dict())
        assert upsert.direct_path_len == 2
        assert upsert.direct_path_hash_mode == 1
        assert protocol.parse_contact_update(payload[:50]) is None


class TestMessageFrames:
    @pytest.mark.asyncio
    async def test_direct_message_frame_parses_with_library_reader(self):
        message = {
            "type": "PRIV",
            "conversation_key": KEY_A,
            "text": "hello",
            "sender_timestamp": 1_700_000_000,
            "received_at": 1_700_000_001,
            "txt_type": 0,
            "paths": [{"path": "1a", "path_len": 1, "snr": 7.5}],
        }
        frame = protocol.encode_contact_message(message)
        assert frame is not None
        events = await parse_with_library(frame)
        assert [e.type for e in events] == [EventType.CONTACT_MSG_RECV]
        parsed = events[0].payload
        assert parsed["pubkey_prefix"] == KEY_A[:12]
        assert parsed["text"] == "hello"
        assert parsed["sender_timestamp"] == 1_700_000_000
        assert parsed["path_len"] == 1
        assert parsed["SNR"] == pytest.approx(7.5)

    @pytest.mark.asyncio
    async def test_room_post_carries_signature(self):
        message = {
            "type": "PRIV",
            "conversation_key": KEY_A,
            "text": "post",
            "sender_timestamp": 1_700_000_000,
            "received_at": 1_700_000_001,
            "txt_type": 2,
            "signature": "01020304",
        }
        events = await parse_with_library(protocol.encode_contact_message(message))
        assert events[0].payload["signature"] == "01020304"
        assert events[0].payload["text"] == "post"

    @pytest.mark.asyncio
    async def test_channel_message_frame_uses_virtual_slot(self):
        message = {
            "type": "CHAN",
            "conversation_key": CHANNEL_KEY,
            "text": "Alice: hi all",
            "sender_timestamp": 1_700_000_000,
            "received_at": 1_700_000_001,
            "paths": None,
        }
        events = await parse_with_library(protocol.encode_channel_message(message, 7))
        assert [e.type for e in events] == [EventType.CHANNEL_MSG_RECV]
        assert events[0].payload["channel_idx"] == 7
        assert events[0].payload["text"] == "Alice: hi all"
        assert events[0].payload["path_len"] == 255, "no path info reads as direct"

    def test_the_apps_trailing_nul_is_not_part_of_the_text(self):
        """``CMD_SEND_TXT_MSG`` is NUL-terminated; the firmware drops the byte."""
        payload = b"\x00\x00" + (1_700_000_000).to_bytes(4, "little") + bytes.fromhex(KEY_A[:12])
        parsed = protocol.parse_send_txt_msg(payload + b"mcmp2:body\x00")
        assert parsed is not None
        assert parsed.text == "mcmp2:body"

    def test_channel_data_command_parses_with_and_without_a_path(self):
        flood = protocol.parse_send_channel_data(bytes([4, 0xFF, 0x1C, 0xAE]) + b"blob")
        assert flood is not None
        assert (flood.channel_index, flood.data_type, flood.blob) == (4, 0xAE1C, b"blob")
        routed = protocol.parse_send_channel_data(bytes([4, 2, 0xAB, 0xCD, 0x1C, 0xAE]) + b"blob")
        assert routed is not None
        assert routed.path == b"\xab\xcd"
        assert routed.blob == b"blob"
        assert protocol.parse_send_channel_data(b"\x04") is None

    @pytest.mark.asyncio
    async def test_channel_data_frames_round_trip_through_the_real_parser(self):
        from app.imaging.aeic.channel_data import parse_channel_data_frame

        frame = protocol.encode_channel_data_recv(5, 0xAE1C, b"\x01\x02\x03", snr=-2.5)
        parsed = parse_channel_data_frame(frame)
        assert parsed is not None
        assert (parsed.channel_index, parsed.data_type, parsed.payload) == (
            5,
            0xAE1C,
            b"\x01\x02\x03",
        )
        assert parsed.snr_db == pytest.approx(-2.5)
        moved = parse_channel_data_frame(protocol.rewrite_channel_data_index(frame, 9))
        assert moved is not None and moved.channel_index == 9
        assert moved.payload == parsed.payload

    def test_pulled_message_txt_type(self):
        v3 = (
            bytes([RESP.CONTACT_MSG_RECV_V3.value, 0, 0, 0]) + bytes(6) + bytes([255, 1]) + bytes(4)
        )
        legacy = bytes([RESP.CONTACT_MSG_RECV.value]) + bytes(6) + bytes([255, 2]) + bytes(4)
        assert protocol.pulled_message_txt_type(v3) == 1
        assert protocol.pulled_message_txt_type(legacy) == 2
        assert protocol.pulled_message_txt_type(bytes([RESP.OK.value])) is None


# --------------------------------------------------------------------------- server


class FakeMeshCore:
    """Just enough of MeshCore for the proxy: identity, staging, raw send."""

    def __init__(self, server_getter):
        self._server_getter = server_getter
        self.self_info = {
            "adv_type": 1,
            "tx_power": 20,
            "max_tx_power": 22,
            "public_key": "cc" * 32,
            "adv_lat": 0.0,
            "adv_lon": 0.0,
            "multi_acks": 0,
            "adv_loc_policy": 0,
            "telemetry_mode_env": 0,
            "telemetry_mode_loc": 0,
            "telemetry_mode_base": 0,
            "manual_add_contacts": False,
            "radio_freq": 910.525,
            "radio_bw": 62.5,
            "radio_sf": 7,
            "radio_cr": 5,
            "name": "Proxy",
        }
        self.sent: list[bytes] = []
        self.responses: dict[int, bytes] = {}
        self.commands = MagicMock()
        self.commands.add_contact = AsyncMock(return_value=Event(EventType.OK, {}))
        self.commands.reset_path = AsyncMock(return_value=Event(EventType.OK, {}))
        self.commands.remove_contact = AsyncMock(return_value=Event(EventType.OK, {}))
        self.commands.send_appstart = AsyncMock(return_value=Event(EventType.OK, {}))
        self.commands.set_channel = AsyncMock(return_value=Event(EventType.OK, {}))
        self.sent_direct: list[tuple] = []
        self.sent_channel: list[tuple] = []

        async def _send_msg(dst=None, msg=None, timestamp=None, **_kw):
            self.sent_direct.append((dst, msg, timestamp))
            return Event(
                EventType.MSG_SENT,
                {"type": 0, "expected_ack": b"\x01\x02\x03\x04", "suggested_timeout": 3000},
            )

        async def _send_chan_msg(chan=None, msg=None, timestamp=None, **_kw):
            self.sent_channel.append((chan, msg, timestamp))
            return Event(EventType.OK, {})

        self.commands.send_msg = AsyncMock(side_effect=_send_msg)
        self.commands.send_chan_msg = AsyncMock(side_effect=_send_chan_msg)
        self.connection_manager = MagicMock()
        self.connection_manager.send = AsyncMock(side_effect=self._send)
        self._contacts: dict[str, dict] = {}

    def get_contact_by_key_prefix(self, prefix: str):
        for key, contact in self._contacts.items():
            if key.startswith(prefix):
                return contact
        return None

    async def _send(self, data: bytes) -> None:
        self.sent.append(bytes(data))
        response = self.responses.get(data[0])
        if response is not None:
            asyncio.get_running_loop().call_soon(self._server_getter().on_radio_frame, response)


class FakeRadio:
    def __init__(self, server_getter):
        self.meshcore = FakeMeshCore(server_getter)
        self.is_connected = True
        self.max_channels = 8
        self.path_hash_mode = 0
        self.repeat_enabled = False
        self.operations: list[str] = []
        # The real RadioManager serializes every operation; the proxy relies on that.
        self._lock = asyncio.Lock()
        # Everything not stubbed above (channel slot cache, capability flags) comes
        # from a real RadioManager, so the send services exercise their real logic.
        from app.radio import RadioManager

        self._manager = RadioManager()
        self._manager.max_channels = 8

    def __getattr__(self, name):
        return getattr(self._manager, name)

    @asynccontextmanager
    async def radio_operation(self, name: str, **_kwargs):
        async with self._lock:
            self.operations.append(name)
            yield self.meshcore


async def read_frame(reader: asyncio.StreamReader) -> bytes:
    header = await asyncio.wait_for(reader.readexactly(3), timeout=2.0)
    assert header[0] == protocol.FRAME_RADIO_TO_HOST
    size = int.from_bytes(header[1:3], "little")
    return await asyncio.wait_for(reader.readexactly(size), timeout=2.0)


def host_frame(payload: bytes) -> bytes:
    return bytes([protocol.FRAME_HOST_TO_RADIO]) + len(payload).to_bytes(2, "little") + payload


class ProxyClient:
    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    async def command(self, payload: bytes) -> bytes:
        self.writer.write(host_frame(payload))
        await self.writer.drain()
        return await read_frame(self.reader)

    async def send_only(self, payload: bytes) -> None:
        self.writer.write(host_frame(payload))
        await self.writer.drain()

    async def read(self) -> bytes:
        return await read_frame(self.reader)

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except Exception:
            pass


@pytest.fixture
async def proxy(test_db):
    holder: dict[str, VirtualNodeServer] = {}
    radio = FakeRadio(lambda: holder["server"])
    server = VirtualNodeServer(radio=radio)
    holder["server"] = server
    await server.start(host="127.0.0.1", port=0)
    clients: list[ProxyClient] = []

    async def connect() -> ProxyClient:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        client = ProxyClient(reader, writer)
        clients.append(client)
        # Let the server register the session before the test starts pushing.
        for _ in range(50):
            if server.client_count >= len(clients):
                break
            await asyncio.sleep(0.01)
        return client

    with patch("app.websocket.ws_manager.broadcast", new=AsyncMock()):
        yield server, radio, connect

    for client in clients:
        await client.close()
    await server.stop()


class TestIdentityAndClock:
    @pytest.mark.asyncio
    async def test_app_start_serves_self_info_from_cached_radio_identity(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        frame = await client.command(b"\x01\x03      mccli")
        events = await parse_with_library(frame)
        assert events[0].type == EventType.SELF_INFO
        assert events[0].payload["name"] == "Proxy"
        assert radio.meshcore.sent == [], "identity never costs a radio round trip"

    @pytest.mark.asyncio
    async def test_raw_self_info_frame_from_radio_is_preferred(self, proxy):
        server, radio, connect = proxy
        raw = protocol.encode_self_info({**radio.meshcore.self_info, "name": "FromRadio"})
        server.on_radio_frame(raw)
        client = await connect()
        frame = await client.command(b"\x01")
        assert frame == raw

    @pytest.mark.asyncio
    async def test_device_query_is_forwarded_once_then_served_locally(self, proxy):
        server, radio, connect = proxy
        device_info = bytes([RESP.DEVICE_INFO.value, 10, 100, 8]) + bytes(78)
        radio.meshcore.responses[CMD.DEVICE_QEURY.value] = device_info
        client = await connect()
        first = await client.command(b"\x16\x03")
        second = await client.command(b"\x16\x03")
        assert first[:3] == device_info[:3]
        assert first[3] >= 40, "virtual node advertises at least the default channel table"
        assert second == first
        assert len(radio.meshcore.sent) == 1

    @pytest.mark.asyncio
    async def test_time_is_answered_from_the_server_clock(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        frame = await client.command(b"\x05")
        assert frame[0] == RESP.CURRENT_TIME.value
        assert abs(int.from_bytes(frame[1:5], "little") - int(time.time())) < 5
        assert await client.command(b"\x06" + (1).to_bytes(4, "little")) == protocol.encode_ok()
        assert radio.meshcore.sent == []


class TestContacts:
    @pytest.mark.asyncio
    async def test_get_contacts_streams_the_database(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(
            ContactUpsert(public_key=KEY_A, name="Alice", type=1, last_seen=1_700_000_000)
        )
        await ContactRepository.upsert(
            ContactUpsert(public_key=KEY_B, name="Bob", type=2, last_seen=1_700_000_050)
        )
        # A prefix-only contact has no full key to hand to an app.
        await ContactRepository.upsert(ContactUpsert(public_key="dd" * 6, name="Prefix", type=1))
        client = await connect()
        await client.send_only(b"\x04")
        start = await client.read()
        assert start[0] == RESP.CONTACT_START.value
        count = int.from_bytes(start[1:5], "little")
        assert count == 2
        keys = set()
        for _ in range(count):
            frame = await client.read()
            events = await parse_with_library(frame)
            keys.add(events[0].payload["public_key"])
        end = await client.read()
        assert end[0] == RESP.CONTACT_END.value
        assert int.from_bytes(end[1:5], "little") == 1_700_000_050
        assert keys == {KEY_A, KEY_B}
        assert radio.meshcore.sent == []

    @pytest.mark.asyncio
    async def test_get_contacts_since_filters_by_lastmod(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(
            ContactUpsert(public_key=KEY_A, name="Alice", type=1, last_seen=1_700_000_000)
        )
        await ContactRepository.upsert(
            ContactUpsert(public_key=KEY_B, name="Bob", type=2, last_seen=1_700_000_050)
        )
        client = await connect()
        await client.send_only(b"\x04" + (1_700_000_000).to_bytes(4, "little"))
        start = await client.read()
        assert int.from_bytes(start[1:5], "little") == 1
        only = await parse_with_library(await client.read())
        assert only[0].payload["public_key"] == KEY_B
        await client.read()

    @pytest.mark.asyncio
    async def test_get_contact_by_key(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Alice", type=1))
        client = await connect()
        found = await client.command(b"\x1e" + bytes.fromhex(KEY_A))
        assert found[0] == RESP.CONTACT.value
        missing = await client.command(b"\x1e" + bytes.fromhex(KEY_B))
        assert missing == protocol.encode_error(ErrorCode.NOT_FOUND)

    @pytest.mark.asyncio
    async def test_add_update_contact_writes_the_database(self, proxy):
        server, radio, connect = proxy
        payload = (
            bytes.fromhex(KEY_A)
            + bytes([1, 1, 0xFF])
            + bytes(64)
            + b"Alice".ljust(32, b"\x00")
            + (1_700_000_000).to_bytes(4, "little")
            + int(45.5 * 1e6).to_bytes(4, "little", signed=True)
            + int(-73.5 * 1e6).to_bytes(4, "little", signed=True)
        )
        client = await connect()
        assert await client.command(b"\x09" + payload) == protocol.encode_ok()
        stored = await ContactRepository.get_by_key(KEY_A)
        assert stored is not None
        assert stored.name == "Alice"
        assert stored.favorite is True
        assert stored.lat == pytest.approx(45.5)
        assert radio.meshcore.sent == [], "contact writes land in the store, not on the radio"

    @pytest.mark.asyncio
    async def test_remove_contact_deletes_locally_and_on_radio(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Alice", type=1))
        radio.meshcore._contacts[KEY_A] = {"public_key": KEY_A}
        client = await connect()
        assert await client.command(b"\x0f" + bytes.fromhex(KEY_A)) == protocol.encode_ok()
        assert await ContactRepository.get_by_key(KEY_A) is None
        radio.meshcore.commands.remove_contact.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reset_path_clears_the_learned_route(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(
            ContactUpsert(
                public_key=KEY_A,
                name="Alice",
                type=1,
                direct_path="1a",
                direct_path_len=1,
                direct_path_hash_mode=0,
            )
        )
        client = await connect()
        assert await client.command(b"\x0d" + bytes.fromhex(KEY_A)) == protocol.encode_ok()
        stored = await ContactRepository.get_by_key(KEY_A)
        assert stored.direct_path_len == -1
        radio.meshcore.commands.reset_path.assert_awaited_once()


class TestChannels:
    @pytest.mark.asyncio
    async def test_channels_are_served_from_virtual_slots(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert(CHANNEL_KEY, "#test", is_hashtag=True)
        client = await connect()
        # Slots are assigned over the whole store (default channels included).
        slot = await server._slot_for_channel(CHANNEL_KEY)
        assert slot is not None
        frame = await client.command(b"\x1f" + bytes([slot]))
        events = await parse_with_library(frame)
        assert events[0].type == EventType.CHANNEL_INFO
        assert events[0].payload["channel_idx"] == slot
        assert events[0].payload["channel_name"] == "#test"
        assert events[0].payload["channel_secret"].hex().upper() == CHANNEL_KEY
        empty = await client.command(b"\x1f\x27")
        empty_events = await parse_with_library(empty)
        assert empty_events[0].payload["channel_name"] == ""
        assert await client.command(b"\x1f\xff") == protocol.encode_error(ErrorCode.ILLEGAL_ARG)
        assert radio.meshcore.sent == []

    @pytest.mark.asyncio
    async def test_set_channel_creates_the_channel_in_the_store(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        secret = bytes.fromhex("11" * 16)
        payload = bytes([39]) + b"#new".ljust(32, b"\x00") + secret
        assert await client.command(b"\x20" + payload) == protocol.encode_ok()
        stored = await ChannelRepository.get_by_key(secret.hex())
        assert stored is not None
        assert stored.name == "#new"
        back = await parse_with_library(await client.command(b"\x1f\x27"))
        assert back[0].payload["channel_name"] == "#new"

    @pytest.mark.asyncio
    async def test_clearing_a_slot_keeps_the_channel_on_the_server(self, proxy):
        server, radio, connect = proxy
        other_key = ("dd" * 16).upper()
        await ChannelRepository.upsert(other_key, "#test", is_hashtag=True)
        client = await connect()
        slot = await server._slot_for_channel(other_key)
        clear = bytes([slot]) + bytes(32) + bytes(16)
        assert await client.command(b"\x20" + clear) == protocol.encode_ok()
        assert await ChannelRepository.get_by_key(other_key) is not None
        blank = await parse_with_library(await client.command(b"\x1f" + bytes([slot])))
        assert blank[0].payload["channel_name"] == ""

    @pytest.mark.asyncio
    async def test_clearing_slot_zero_cannot_unseat_the_public_channel(self, proxy):
        """Slot 0 is the public channel by definition; sends depend on it."""
        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        client = await connect()
        clear = bytes([0]) + bytes(32) + bytes(16)
        assert await client.command(b"\x20" + clear) == protocol.encode_ok()
        back = await parse_with_library(await client.command(b"\x1f\x00"))
        assert back[0].payload["channel_name"] == "Public"


class TestMessageQueue:
    @pytest.mark.asyncio
    async def test_incoming_messages_are_queued_per_client_with_a_waiting_push(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert(CHANNEL_KEY, "#test", is_hashtag=True)
        client = await connect()
        assert await client.command(b"\x0a") == protocol.encode_no_more_messages()

        server.on_app_event(
            "message",
            {
                "type": "CHAN",
                "conversation_key": CHANNEL_KEY,
                "text": "Alice: hello",
                "sender_timestamp": 1_700_000_000,
                "received_at": 1_700_000_001,
                "outgoing": False,
            },
        )
        assert await client.read() == protocol.encode_push_msg_waiting()
        frame = await client.command(b"\x0a")
        events = await parse_with_library(frame)
        assert events[0].type == EventType.CHANNEL_MSG_RECV
        assert events[0].payload["text"] == "Alice: hello"
        assert events[0].payload["channel_idx"] == await server._slot_for_channel(CHANNEL_KEY)
        assert await client.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_several_clients_each_get_their_own_copy(self, proxy):
        server, radio, connect = proxy
        first = await connect()
        second = await connect()
        third = await connect()
        assert server.client_count == 3

        # One client drains its inbox ahead of the others; that must not
        # consume anything another client has yet to fetch.
        server.on_app_event(
            "message",
            {
                "type": "PRIV",
                "conversation_key": KEY_A,
                "text": "for everyone",
                "sender_timestamp": 1_700_000_000,
                "received_at": 1_700_000_001,
                "outgoing": False,
            },
        )
        for client in (first, second, third):
            assert await client.read() == protocol.encode_push_msg_waiting()
        frame_first = await first.command(b"\x0a")
        assert await first.command(b"\x0a") == protocol.encode_no_more_messages()
        frame_second = await second.command(b"\x0a")
        frame_third = await third.command(b"\x0a")
        assert frame_first == frame_second == frame_third
        assert (await parse_with_library(frame_first))[0].payload["text"] == "for everyone"

        # Pushes fan out to every client too.
        ack = bytes([RESP.ACK.value, 9, 9, 9, 9])
        server.on_radio_frame(ack)
        for client in (first, second, third):
            assert await client.read() == ack

        # Forwarded commands from concurrent clients are serialized through the
        # radio lock and each gets its own answer; the cache then serves the rest.
        radio.meshcore.responses[CMD.GET_CUSTOM_VARS.value] = (
            bytes([RESP.CUSTOM_VARS.value]) + b"k:v"
        )
        answers = await asyncio.gather(
            first.command(b"\x28"), second.command(b"\x28"), third.command(b"\x28")
        )
        assert answers == [bytes([RESP.CUSTOM_VARS.value]) + b"k:v"] * 3
        assert len(radio.meshcore.sent) == 1, "three clients, one radio round trip"

        # A client leaving does not disturb the others.
        await second.close()
        for _ in range(50):
            if server.client_count == 2:
                break
            await asyncio.sleep(0.01)
        assert server.client_count == 2
        assert (await first.command(b"\x05"))[0] == RESP.CURRENT_TIME.value

    @pytest.mark.asyncio
    async def test_outgoing_and_reaction_messages_are_not_queued(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        server.on_app_event(
            "message",
            {"type": "PRIV", "conversation_key": KEY_A, "text": "x", "outgoing": True},
        )
        server.on_app_event(
            "message",
            {"type": "PRIV", "conversation_key": KEY_A, "text": "r:0000:00", "is_reaction": True},
        )
        await asyncio.sleep(0.05)
        assert await client.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_repeater_cli_replies_pulled_by_remoteterm_reach_the_app(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        cli_reply = (
            bytes([RESP.CONTACT_MSG_RECV_V3.value, 0, 0, 0])
            + bytes.fromhex(KEY_A[:12])
            + bytes([255, 1])
            + (1_700_000_000).to_bytes(4, "little")
            + b"uptime 4d"
        )
        server.on_radio_frame(cli_reply)
        assert await client.read() == protocol.encode_push_msg_waiting()
        assert await client.command(b"\x0a") == cli_reply

    @pytest.mark.asyncio
    async def test_contact_events_become_advert_pushes(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        server.on_app_event("contact", {"public_key": KEY_A})
        assert await client.read() == protocol.encode_push_advert(KEY_A)
        # Prefix-only contacts cannot be announced with a 32-byte key.
        server.on_app_event("contact", {"public_key": "dd" * 6})
        server.on_app_event("contact", {"public_key": KEY_B})
        assert await client.read() == protocol.encode_push_advert(KEY_B)


class TestForwarding:
    @pytest.mark.asyncio
    async def test_read_only_queries_are_forwarded_then_cached(self, proxy):
        server, radio, connect = proxy
        radio.meshcore.responses[CMD.GET_CUSTOM_VARS.value] = (
            bytes([RESP.CUSTOM_VARS.value]) + b"a:1"
        )
        client = await connect()
        first = await client.command(b"\x28")
        second = await client.command(b"\x28")
        assert first == second == bytes([RESP.CUSTOM_VARS.value]) + b"a:1"
        assert len(radio.meshcore.sent) == 1
        assert radio.operations == ["virtual_node:40"]

    @pytest.mark.asyncio
    async def test_config_writes_forward_and_invalidate_the_cache(self, proxy):
        from app.repository import AppSettingsRepository

        server, radio, connect = proxy
        await AppSettingsRepository.update(virtual_node_allow_admin_commands=True)
        radio.meshcore.responses[CMD.GET_CUSTOM_VARS.value] = (
            bytes([RESP.CUSTOM_VARS.value]) + b"a:1"
        )
        radio.meshcore.responses[CMD.SET_CUSTOM_VAR.value] = protocol.encode_ok()
        client = await connect()
        await client.command(b"\x28")
        assert await client.command(b"\x29a:2") == protocol.encode_ok()
        await client.command(b"\x28")
        assert [f[0] for f in radio.meshcore.sent] == [0x28, 0x29, 0x28]

    @pytest.mark.asyncio
    async def test_contact_addressed_commands_stage_the_contact_first(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Repeater", type=2))
        radio.meshcore.responses[CMD.SEND_LOGIN.value] = protocol.encode_msg_sent(
            is_flood=True, expected_ack=b"\x01\x02\x03\x04", suggested_timeout_ms=3000
        )
        client = await connect()
        frame = await client.command(b"\x1a" + bytes.fromhex(KEY_A) + b"password")
        assert frame[0] == RESP.MSG_SENT.value
        radio.meshcore.commands.add_contact.assert_awaited_once()
        staged = radio.meshcore.commands.add_contact.await_args.args[0]
        assert staged["public_key"] == KEY_A

    @pytest.mark.asyncio
    async def test_unanswered_forward_reports_bad_state(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        with patch("app.virtual_node.server.FORWARD_TIMEOUT_SECONDS", 0.05):
            frame = await client.command(b"\x07\x01")
        assert frame == protocol.encode_error(ErrorCode.BAD_STATE)

    @pytest.mark.asyncio
    async def test_disconnected_radio_reports_bad_state(self, proxy):
        server, radio, connect = proxy
        from app.radio import RadioDisconnectedError

        @asynccontextmanager
        async def broken(name, **_kwargs):
            raise RadioDisconnectedError("Radio disconnected")
            yield  # pragma: no cover

        radio.radio_operation = broken
        client = await connect()
        assert await client.command(b"\x07") == protocol.encode_error(ErrorCode.BAD_STATE)

    @pytest.mark.asyncio
    async def test_dangerous_commands_are_refused(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        for code in (CMD.REBOOT.value, CMD.FACTORY_RESET.value, CMD.IMPORT_PRIVATE_KEY.value):
            assert await client.command(bytes([code])) == protocol.encode_error(
                ErrorCode.UNSUPPORTED_CMD
            )
        assert await client.command(b"\x17") == protocol.encode_disabled()
        assert radio.meshcore.sent == []

    @pytest.mark.asyncio
    async def test_read_only_mode_refuses_transmit_and_writes(self, proxy):
        server, radio, connect = proxy
        server.read_only = True
        client = await connect()
        for payload in (b"\x07", b"\x08name", b"\x09" + bytes(143), b"\x20" + bytes(49)):
            assert await client.command(payload) == protocol.encode_error(ErrorCode.UNSUPPORTED_CMD)
        # Reads still work.
        assert (await client.command(b"\x05"))[0] == RESP.CURRENT_TIME.value
        assert radio.meshcore.sent == []

    @pytest.mark.asyncio
    async def test_battery_is_answered_from_the_stats_sampler(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        with patch(
            "app.services.radio_stats.get_latest_radio_stats",
            return_value={"timestamp": int(time.time()), "battery_mv": 4012},
        ):
            frame = await client.command(b"\x14")
        assert frame == protocol.encode_battery(4012)
        assert radio.meshcore.sent == []


class TestAdminCommandGate:
    """Radio configuration from apps is an explicit, default-off operator choice."""

    @pytest.mark.asyncio
    async def test_admin_commands_are_refused_by_default(self, proxy):
        server, radio, connect = proxy
        radio.meshcore.responses[CMD.SET_ADVERT_NAME.value] = protocol.encode_ok()
        client = await connect()
        for payload in (b"\x08NewName", b"\x0c" + (20).to_bytes(4, "little"), b"\x29a:2"):
            assert await client.command(payload) == protocol.encode_error(ErrorCode.UNSUPPORTED_CMD)
        assert radio.meshcore.sent == []
        # Everything that is not radio configuration still works.
        radio.meshcore.responses[CMD.SEND_SELF_ADVERT.value] = protocol.encode_ok()
        assert await client.command(b"\x07\x01") == protocol.encode_ok()

    @pytest.mark.asyncio
    async def test_admin_commands_forward_once_the_setting_is_on(self, proxy):
        from app.repository import AppSettingsRepository

        server, radio, connect = proxy
        await AppSettingsRepository.update(virtual_node_allow_admin_commands=True)
        radio.meshcore.responses[CMD.SET_ADVERT_NAME.value] = protocol.encode_ok()
        client = await connect()
        assert await client.command(b"\x08NewName") == protocol.encode_ok()
        assert radio.meshcore.sent == [b"\x08NewName"]
        # Switching it back off takes effect on the next command.
        await AppSettingsRepository.update(virtual_node_allow_admin_commands=False)
        assert await client.command(b"\x08Again") == protocol.encode_error(
            ErrorCode.UNSUPPORTED_CMD
        )
        assert len(radio.meshcore.sent) == 1

    @pytest.mark.asyncio
    async def test_send_shaping_commands_are_not_radio_configuration(self, proxy):
        """Regression: SET_FLOOD_SCOPE is how an app scopes the send it is making.

        MeshCore Open sends it immediately before a channel message. Gating it
        behind the admin switch made the app abandon the send: the message never
        went out and the app reported only that it could not send.
        """
        server, radio, connect = proxy
        radio.meshcore.responses[CMD.IMPORT_CONTACT.value] = protocol.encode_ok()
        client = await connect()
        assert not (await server.admin_commands_allowed()), "admin stays off for this test"

        scope = bytes([CMD.SET_FLOOD_SCOPE.value, 0]) + bytes(16)
        assert await client.command(scope) == protocol.encode_ok()
        assert await client.command(bytes([CMD.SET_PATH_HASH_MODE.value, 1])) == (
            protocol.encode_ok()
        )
        assert await client.command(bytes([CMD.IMPORT_CONTACT.value]) + bytes(32)) == (
            protocol.encode_ok()
        )
        # Genuine radio configuration is still refused.
        assert await client.command(b"\x08NewName") == protocol.encode_error(
            ErrorCode.UNSUPPORTED_CMD
        )

    @pytest.mark.asyncio
    async def test_send_shaping_is_acknowledged_but_never_reaches_the_radio(self, proxy):
        """Regression: the app's scope must not become the radio's standing scope.

        Flood scope and path hash mode stick on the radio until something else
        changes them, and RemoteTerm's channel send only overrides and restores
        them when the channel carries an explicit override. Forwarding the app's
        setting therefore shaped the message *and* every send after it: the
        packet went out and was repeated normally, scoped away from the people
        it was addressed to. Sent, never delivered.
        """
        server, radio, connect = proxy
        client = await connect()
        assert await client.command(bytes([CMD.SET_FLOOD_SCOPE.value, 0]) + bytes(16)) == (
            protocol.encode_ok()
        )
        assert await client.command(bytes([CMD.SET_PATH_HASH_MODE.value, 1])) == (
            protocol.encode_ok()
        )
        assert radio.meshcore.sent == []
        assert [c["detail"] for c in server.status()["recent_commands"]] == [
            "acknowledged; RemoteTerm shapes its own sends",
            "acknowledged; RemoteTerm shapes its own sends",
        ]

    @pytest.mark.asyncio
    async def test_read_only_answers_shaping_and_refuses_the_send_itself(self, proxy):
        """Read-only refuses what transmits, not the parameters for it.

        Refusing the parameter is what stopped MeshCore Open before it ever
        tried; refusing the send says plainly which command was not allowed.
        """
        server, radio, connect = proxy
        server.read_only = True
        client = await connect()
        assert await client.command(bytes([CMD.SET_FLOOD_SCOPE.value, 0]) + bytes(16)) == (
            protocol.encode_ok()
        )
        assert await client.command(bytes([CMD.SET_PATH_HASH_MODE.value, 1])) == (
            protocol.encode_ok()
        )
        assert await client.command(bytes([CMD.IMPORT_CONTACT.value]) + bytes(32)) == (
            protocol.encode_error(ErrorCode.UNSUPPORTED_CMD)
        )
        assert await client.command(
            bytes([CMD.SEND_CHANNEL_TXT_MSG.value, 0, 0]) + bytes(4) + b"hi"
        ) == protocol.encode_error(ErrorCode.UNSUPPORTED_CMD)
        assert radio.meshcore.sent == []

    @pytest.mark.asyncio
    async def test_read_only_wins_over_the_admin_setting(self, proxy):
        from app.repository import AppSettingsRepository

        server, radio, connect = proxy
        await AppSettingsRepository.update(virtual_node_allow_admin_commands=True)
        server.read_only = True
        client = await connect()
        assert await client.command(b"\x08NewName") == protocol.encode_error(
            ErrorCode.UNSUPPORTED_CMD
        )
        assert radio.meshcore.sent == []


class TestOperatorRouter:
    """GET /virtual-node and the two operator actions."""

    @pytest.mark.asyncio
    async def test_overview_lists_connected_and_remembered_apps(self, proxy):
        from app.repository import AppSettingsRepository
        from app.routers.virtual_node import get_virtual_node_overview

        server, radio, connect = proxy
        earlier = await connect()
        await earlier.command(APP_START_AS["cli"])
        await earlier.close()
        await wait_for_clients(server, 0)
        phone = await connect()
        await phone.command(APP_START_AS["phone"])
        await AppSettingsRepository.update(virtual_node_allow_admin_commands=True)

        with patch("app.routers.virtual_node.virtual_node", server):
            overview = await get_virtual_node_overview()

        assert overview.listening is True
        assert overview.port == server.port
        assert overview.admin_commands_allowed is True
        assert overview.client_count == 1
        assert [c.client_id for c in overview.connected] == ["MeshCore@127.0.0.1"]
        assert overview.connected[0].app_name == "MeshCore"
        known = {k.client_id: k for k in overview.known_clients}
        assert set(known) == {"MeshCore@127.0.0.1", "mccli@127.0.0.1"}
        assert known["MeshCore@127.0.0.1"].connected is True
        assert known["mccli@127.0.0.1"].connected is False

    @pytest.mark.asyncio
    async def test_forgetting_a_client_resets_its_history(self, proxy):
        from fastapi import HTTPException

        from app.routers.virtual_node import forget_virtual_node_client

        server, radio, connect = proxy
        first = await connect()
        await first.command(APP_START_AS["phone"])
        await first.close()
        await wait_for_clients(server, 0)
        await store_incoming_dm("would have been replayed", 1_700_000_000)

        with patch("app.routers.virtual_node.virtual_node", server):
            assert (await forget_virtual_node_client("MeshCore@127.0.0.1"))["status"] == "ok"
            with pytest.raises(HTTPException) as exc:
                await forget_virtual_node_client("MeshCore@127.0.0.1")
            assert exc.value.status_code == 404

        second = await connect()
        assert (await second.command(APP_START_AS["phone"]))[0] == RESP.SELF_INFO.value
        assert await second.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_operator_can_disconnect_an_app(self, proxy):
        from fastapi import HTTPException

        from app.routers.virtual_node import disconnect_virtual_node_client

        server, radio, connect = proxy
        client = await connect()
        await client.command(APP_START_AS["phone"])
        peer = server.status()["clients"][0]["peer"]
        with patch("app.routers.virtual_node.virtual_node", server):
            assert (await disconnect_virtual_node_client(peer))["status"] == "ok"
            await wait_for_clients(server, 0)
            with pytest.raises(HTTPException):
                await disconnect_virtual_node_client(peer)
        with pytest.raises((asyncio.IncompleteReadError, ConnectionError, TimeoutError)):
            await client.command(b"\x05")


class TestPushRelay:
    @pytest.mark.asyncio
    async def test_radio_pushes_are_relayed_except_the_synthesized_ones(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        ack = bytes([RESP.ACK.value, 1, 2, 3, 4])
        server.on_radio_frame(bytes([protocol.PUSH_CODE_ADVERT]) + bytes(32))
        server.on_radio_frame(protocol.encode_push_msg_waiting())
        server.on_radio_frame(ack)
        assert await client.read() == ack

    @pytest.mark.asyncio
    async def test_frame_tap_feeds_the_server_and_the_original_reader(self, proxy):
        server, radio, connect = proxy
        seen: list[bytes] = []

        async def original(data):
            seen.append(bytes(data))

        reader = SimpleNamespace(handle_rx=original)
        meshcore = SimpleNamespace(_reader=reader)
        with patch("app.virtual_node.server.virtual_node", server):
            install_frame_tap(meshcore)
            install_frame_tap(meshcore)  # idempotent
            raw = protocol.encode_self_info(radio.meshcore.self_info)
            await reader.handle_rx(bytearray(raw))
        assert seen == [raw]
        assert server._self_info_frame == raw


DATA_CHANNEL_KEY = ("ab" * 16).upper()


class TestChannelDataSends:
    """``CMD_SEND_CHANNEL_DATA`` (62): images, and compressed channel messages.

    MCO Advanced sends every picture this way, and every MCMP message too while
    its ``channelsSendAsBinary`` setting is on -- the default -- so this is the
    difference between an app that can send pictures and compressed text through
    the node and one that can only send plain text.
    """

    def _command(self, slot: int, data_type: int, blob: bytes) -> bytes:
        return (
            bytes([protocol.CMD_SEND_CHANNEL_DATA, slot, 0xFF, data_type & 0xFF, data_type >> 8])
            + blob
        )

    @pytest.mark.asyncio
    async def test_the_blob_is_re_addressed_to_the_radios_own_slot(self, proxy):
        """The app names its virtual slot; the radio holds a different table."""
        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        radio.meshcore.responses[protocol.CMD_SEND_CHANNEL_DATA] = protocol.encode_ok()
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)
        assert slot != 0  # the public channel owns slot 0, so this proves a remap

        blob = b"\xaa\xbb" + bytes(20)
        answer = await client.command(self._command(slot, 0xAE1C, blob))

        assert answer == protocol.encode_ok()
        sent = [f for f in radio.meshcore.sent if f[0] == protocol.CMD_SEND_CHANNEL_DATA]
        assert len(sent) == 1
        assert sent[0][1] == radio.get_cached_channel_slot(DATA_CHANNEL_KEY) != slot
        assert sent[0][3:5] == b"\x1c\xae"
        assert sent[0][5:] == blob

    @pytest.mark.asyncio
    async def test_an_empty_slot_is_refused_rather_than_sent_to_the_wrong_channel(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        answer = await client.command(self._command(200, 0xAE1C, b"blob"))
        assert answer == protocol.encode_error(ErrorCode.NOT_FOUND)
        assert [f for f in radio.meshcore.sent if f[0] == protocol.CMD_SEND_CHANNEL_DATA] == []

    @pytest.mark.asyncio
    async def test_a_radio_refusal_is_passed_back_to_the_app(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        radio.meshcore.responses[protocol.CMD_SEND_CHANNEL_DATA] = protocol.encode_error(
            ErrorCode.UNSUPPORTED_CMD
        )
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)
        answer = await client.command(self._command(slot, 0xAE1C, b"blob"))
        assert answer == protocol.encode_error(ErrorCode.UNSUPPORTED_CMD)

    @pytest.mark.asyncio
    async def test_a_compressed_message_from_an_app_is_stored_as_text(self, proxy):
        """The blob goes out verbatim; RemoteTerm keeps the words, not basE91."""
        from app.compression.mcmp import get_compressor
        from app.imaging.aeic.channel_data import DATA_TYPE_MCMP

        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        radio.meshcore.responses[protocol.CMD_SEND_CHANNEL_DATA] = protocol.encode_ok()
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)

        text = "the pictures are working again, and so is the compression"
        name = b"Phone"
        body = get_compressor().compress_to_bytes(text)
        blob = bytes([len(name)]) + name + body

        assert await client.command(self._command(slot, DATA_TYPE_MCMP, blob)) == (
            protocol.encode_ok()
        )
        sent = [f for f in radio.meshcore.sent if f[0] == protocol.CMD_SEND_CHANNEL_DATA]
        assert sent[0][5:] == blob, "the app's own bytes go on the air, not a re-encoding"

        for _ in range(50):
            messages = await MessageRepository.get_all(
                msg_type="CHAN", conversation_key=DATA_CHANNEL_KEY
            )
            if messages:
                break
            await asyncio.sleep(0.01)
        assert [m.text for m in messages] == [f"Phone: {text}"]

    @pytest.mark.asyncio
    async def test_the_other_apps_see_the_blob_and_the_sender_does_not(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        radio.meshcore.responses[protocol.CMD_SEND_CHANNEL_DATA] = protocol.encode_ok()
        sender = await connect()
        listener = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)

        blob = b"\x01\x02" + bytes(10)
        assert await sender.command(self._command(slot, 0xAE1C, blob)) == protocol.encode_ok()

        assert await listener.read() == protocol.encode_push_msg_waiting()
        queued = await listener.command(b"\x0a")
        assert queued == protocol.encode_channel_data_recv(slot, 0xAE1C, blob)
        assert await sender.command(b"\x0a") == protocol.encode_no_more_messages()


class TestChannelDataToApps:
    """Apps on this node have no other route to a GRP_DATA blob."""

    @pytest.mark.asyncio
    async def test_an_inbound_frame_is_re_addressed_to_the_virtual_slot(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        client = await connect()
        virtual_slot = await server._slot_for_channel(DATA_CHANNEL_KEY)
        radio_slot = 3
        assert radio_slot != virtual_slot
        radio.note_channel_slot_loaded(DATA_CHANNEL_KEY, radio_slot)

        blob = b"\x09\x08" + bytes(8)
        server.on_radio_frame(protocol.encode_channel_data_recv(radio_slot, 0xAE1C, blob))

        assert await client.read() == protocol.encode_push_msg_waiting()
        assert await client.command(b"\x0a") == protocol.encode_channel_data_recv(
            virtual_slot, 0xAE1C, blob
        )

    @pytest.mark.asyncio
    async def test_the_same_blob_is_not_relayed_twice(self, proxy):
        """Frame 27 and the raw-RF decode of the same packet both reach here."""
        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)
        radio.note_channel_slot_loaded(DATA_CHANNEL_KEY, 3)

        blob = b"\x07\x06" + bytes(6)
        server.on_radio_frame(protocol.encode_channel_data_recv(3, 0xAE1C, blob))
        assert await client.read() == protocol.encode_push_msg_waiting()
        server.mirror_channel_data(DATA_CHANNEL_KEY, 0xAE1C, blob)
        await asyncio.sleep(0.05)

        assert await client.command(b"\x0a") == protocol.encode_channel_data_recv(
            slot, 0xAE1C, blob
        )
        assert await client.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_our_own_send_is_mirrored_to_the_apps(self, proxy):
        """A picture sent from the web UI never crosses an app's radio."""
        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)

        blob = b"\x05\x04" + bytes(4)
        server.mirror_channel_data(DATA_CHANNEL_KEY, 0xAE1C, blob)

        assert await client.read() == protocol.encode_push_msg_waiting()
        assert await client.command(b"\x0a") == protocol.encode_channel_data_recv(
            slot, 0xAE1C, blob
        )

    @pytest.mark.asyncio
    async def test_an_image_sent_from_the_web_ui_reaches_the_apps(self, proxy):
        """End to end: the AEIC binary transport feeds the apps on this node."""
        from app.imaging.aeic.text_transport import AeicStreamMetadata
        from app.imaging.aeic.transport import AeicTarget, ChannelDataTransport

        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        radio.meshcore.commands.send = AsyncMock(return_value=Event(EventType.OK, {}))
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)

        with patch("app.virtual_node.server.virtual_node", server):
            result = await ChannelDataTransport().send(
                bytes(range(120)),
                AeicStreamMetadata(square_size=512, aspect_code=0),
                AeicTarget(
                    conversation_type="CHAN",
                    conversation_key=DATA_CHANNEL_KEY,
                    radio_manager=radio,
                ),
            )
        await asyncio.sleep(0.05)

        assert result.chunk_count == 2  # one data blob and its parity
        relayed = []
        for _ in range(result.chunk_count):
            assert await client.read() == protocol.encode_push_msg_waiting()
        while True:
            frame = await client.command(b"\x0a")
            if frame == protocol.encode_no_more_messages():
                break
            relayed.append(frame)
        assert len(relayed) == result.chunk_count
        assert all(f[0] == protocol.RESP_CODE_CHANNEL_DATA_RECV for f in relayed)
        assert all(f[protocol.CHANNEL_DATA_RECV_INDEX_OFFSET] == slot for f in relayed)

    @pytest.mark.asyncio
    async def test_our_own_prefix_is_aliased_so_the_app_does_not_drop_it(self, proxy, monkeypatch):
        """An app discards a chunk that claims to come from itself.

        MCO Advanced answers ``ImageChunkStatus.fromSelf`` before reassembly, and
        an app on this node believes its key is this radio's -- so every picture
        we relay carries, from its point of view, its own prefix. Correct on a
        real radio, where hearing your own prefix means a repeater re-flooded
        your packet; fatal here, where it is the only way a picture ever arrives.
        """
        from app.imaging.aeic import channel_data_ingest
        from app.imaging.aeic.channel_data import build_image_chunks, parse_chunk_blob
        from app.imaging.aeic.text_transport import AeicStreamMetadata

        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        monkeypatch.setattr(channel_data_ingest, "self_node_identity", lambda: ("cc" * 32, "Proxy"))
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)

        meta = AeicStreamMetadata(square_size=512, aspect_code=0).encode()
        blob = build_image_chunks(bytes(120), meta, sender_prefix=0xCCCC, img_id=3)[0]
        server.mirror_channel_data(DATA_CHANNEL_KEY, 0xAE1C, blob)

        assert await client.read() == protocol.encode_push_msg_waiting()
        frame = await client.command(b"\x0a")
        assert frame[protocol.CHANNEL_DATA_RECV_INDEX_OFFSET] == slot
        relayed = parse_chunk_blob(frame[protocol.CHANNEL_DATA_RECV_HEADER_BYTES :])
        assert relayed is not None
        assert relayed.sender_prefix == 0x3333, "the app would drop this as its own echo"
        # Everything else is the picture, and must be untouched.
        assert (relayed.img_id, relayed.index, relayed.total, relayed.body) == (3, 0, 1, blob[4:])

    @pytest.mark.asyncio
    async def test_a_peers_picture_is_relayed_untouched(self, proxy, monkeypatch):
        from app.imaging.aeic import channel_data_ingest
        from app.imaging.aeic.channel_data import build_image_chunks
        from app.imaging.aeic.text_transport import AeicStreamMetadata

        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        monkeypatch.setattr(channel_data_ingest, "self_node_identity", lambda: ("cc" * 32, "Proxy"))
        client = await connect()
        slot = await server._slot_for_channel(DATA_CHANNEL_KEY)
        radio.note_channel_slot_loaded(DATA_CHANNEL_KEY, 3)

        meta = AeicStreamMetadata(square_size=512, aspect_code=0).encode()
        blob = build_image_chunks(bytes(120), meta, sender_prefix=0x1234, img_id=3)[0]
        server.on_radio_frame(protocol.encode_channel_data_recv(3, 0xAE1C, blob))

        assert await client.read() == protocol.encode_push_msg_waiting()
        assert await client.command(b"\x0a") == protocol.encode_channel_data_recv(
            slot, 0xAE1C, blob
        )

    @pytest.mark.asyncio
    async def test_the_aliased_copy_still_reassembles(self, proxy, monkeypatch):
        """Relabelling every chunk must not cost the app its parity recovery."""
        from app.imaging.aeic import channel_data_ingest
        from app.imaging.aeic.channel_data import build_image_chunks
        from app.imaging.aeic.channel_data_ingest import ChannelDataReassembler
        from app.imaging.aeic.text_transport import AeicStreamMetadata

        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        monkeypatch.setattr(channel_data_ingest, "self_node_identity", lambda: ("cc" * 32, "Proxy"))
        client = await connect()

        bitstream = bytes(range(255)) * 2
        meta = AeicStreamMetadata(square_size=512, aspect_code=0).encode()
        blobs = build_image_chunks(bitstream, meta, sender_prefix=0xCCCC, img_id=3)
        assert len(blobs) >= 4, "need several data chunks plus parity to test recovery"
        for blob in blobs:
            server.mirror_channel_data(DATA_CHANNEL_KEY, 0xAE1C, blob)
        await asyncio.sleep(0.05)
        for _ in blobs:
            assert await client.read() == protocol.encode_push_msg_waiting()

        received = []
        while True:
            frame = await client.command(b"\x0a")
            if frame == protocol.encode_no_more_messages():
                break
            received.append(frame[protocol.CHANNEL_DATA_RECV_HEADER_BYTES :])
        assert len(received) == len(blobs)

        # Reassemble the way the app would, with the first data chunk lost.
        far_side = ChannelDataReassembler()
        outcome = None
        for blob in received[1:]:
            outcome = far_side.note_chunk("app", blob)
        assert outcome is not None
        assert outcome[0] == bitstream
        assert outcome[2] is True, "parity did not rebuild the missing chunk"

    @pytest.mark.asyncio
    async def test_a_marker_row_is_not_mirrored_as_text(self, proxy):
        """``aeib:`` is a convention between the server and its own UI."""
        server, radio, connect = proxy
        await ChannelRepository.upsert(DATA_CHANNEL_KEY, "#pics", is_hashtag=True)
        client = await connect()

        server.on_app_event(
            "message",
            {
                "type": "CHAN",
                "conversation_key": DATA_CHANNEL_KEY,
                "text": "aeib:chan-1-abc",
                "sender_timestamp": 1_700_000_000,
            },
        )
        await asyncio.sleep(0.05)
        assert await client.command(b"\x0a") == protocol.encode_no_more_messages()


class TestSendsGoThroughTheServiceLayer:
    @pytest.mark.asyncio
    async def test_direct_message_uses_the_shared_send_workflow(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Alice", type=1))
        fake_message = MagicMock(id=42, acked=0)
        send = AsyncMock(return_value=fake_message)
        dm_ack_tracker._pending_acks.clear()
        client = await connect()
        payload = (
            b"\x00\x00" + (1_700_000_000).to_bytes(4, "little") + bytes.fromhex(KEY_A[:12]) + b"hi"
        )

        async def _send(**kwargs):
            dm_ack_tracker.track_pending_ack("0a0b0c0d", 42, 5000)
            return fake_message

        send.side_effect = _send
        with patch("app.services.message_send.send_direct_message_to_contact", send):
            frame = await client.command(b"\x02" + payload)
            # The app's own retry must not create a second message row.
            retry = await client.command(b"\x02" + payload[:1] + b"\x01" + payload[2:])
        assert frame[0] == RESP.MSG_SENT.value
        assert frame[2:6] == bytes.fromhex("0a0b0c0d")
        assert int.from_bytes(frame[6:10], "little") == 5000
        assert retry == frame
        send.assert_awaited_once()
        assert send.await_args.kwargs["contact"].public_key == KEY_A
        assert send.await_args.kwargs["text"] == "hi"
        dm_ack_tracker._pending_acks.clear()

    @pytest.mark.asyncio
    async def test_direct_message_to_unknown_prefix_is_not_found(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        payload = b"\x00\x00" + bytes(4) + bytes.fromhex(KEY_B[:12]) + b"hi"
        assert await client.command(b"\x02" + payload) == protocol.encode_error(ErrorCode.NOT_FOUND)

    @pytest.mark.asyncio
    async def test_channel_message_resolves_the_virtual_slot(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert(CHANNEL_KEY, "#test", is_hashtag=True)
        send = AsyncMock(return_value=MagicMock())
        client = await connect()
        slot = await server._slot_for_channel(CHANNEL_KEY)
        payload = bytes([0, slot]) + (1_700_000_000).to_bytes(4, "little") + b"hello"
        with patch("app.services.message_send.send_channel_message_to_channel", send):
            assert await client.command(b"\x03" + payload) == protocol.encode_ok()
        send.assert_awaited_once()
        assert send.await_args.kwargs["channel"].key == CHANNEL_KEY
        assert send.await_args.kwargs["text"] == "hello"

    @pytest.mark.asyncio
    async def test_the_trace_records_what_a_send_resolved_to(self, proxy):
        """A send that goes out and reaches nobody looks identical to one that worked.

        The slot and the 6-byte prefix an app addresses are resolved here, so
        the trace is the only place that can say which channel or contact the
        message was actually encrypted for.
        """
        server, radio, connect = proxy
        await ChannelRepository.upsert(CHANNEL_KEY, "#test", is_hashtag=True)
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Alice", type=1))
        client = await connect()
        slot = await server._slot_for_channel(CHANNEL_KEY)
        dm = b"\x00\x00" + bytes(4) + bytes.fromhex(KEY_A[:12]) + b"hi"
        with (
            patch("app.services.message_send.send_channel_message_to_channel", AsyncMock()),
            patch(
                "app.services.message_send.send_direct_message_to_contact",
                AsyncMock(return_value=MagicMock(id=1, acked=0)),
            ),
        ):
            await client.command(b"\x03" + bytes([0, slot]) + bytes(4) + b"hello")
            await client.command(b"\x02" + dm)

        traced = {entry["command"]: entry["detail"] for entry in server.status()["recent_commands"]}
        assert traced["SEND_CHANNEL_TXT_MSG"] == f"slot {slot} -> #test"
        assert traced["SEND_TXT_MSG"] == "to Alice"

    @pytest.mark.asyncio
    async def test_channel_message_to_empty_slot_is_not_found(self, proxy):
        server, radio, connect = proxy
        client = await connect()
        payload = b"\x00\x27" + bytes(4) + b"hello"
        assert await client.command(b"\x03" + payload) == protocol.encode_error(ErrorCode.NOT_FOUND)

    @pytest.mark.asyncio
    async def test_cli_command_is_forwarded_raw_after_staging(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Repeater", type=2))
        radio.meshcore.responses[CMD.SEND_TXT_MSG.value] = protocol.encode_msg_sent(
            is_flood=False, expected_ack=b"\x09\x09\x09\x09", suggested_timeout_ms=2000
        )
        client = await connect()
        payload = b"\x01\x00" + bytes(4) + bytes.fromhex(KEY_A[:12]) + b"clock"
        frame = await client.command(b"\x02" + payload)
        assert frame[0] == RESP.MSG_SENT.value
        assert radio.meshcore.sent == [b"\x02" + payload]
        radio.meshcore.commands.add_contact.assert_awaited_once()


APP_START_AS = {
    "phone": b"\x01\x03      MeshCore",
    "cli": b"\x01\x03      mccli",
}


async def store_incoming_dm(text: str, timestamp: int) -> int:
    from app.repository import MessageRepository

    message_id = await MessageRepository.create(
        msg_type="PRIV",
        text=text,
        received_at=timestamp,
        conversation_key=KEY_A,
        sender_timestamp=timestamp,
        outgoing=False,
    )
    assert message_id is not None
    return message_id


async def wait_for_clients(server: VirtualNodeServer, count: int) -> None:
    for _ in range(100):
        if server.client_count == count:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {count} clients, have {server.client_count}")


class TestHistoryReplay:
    """A returning app is handed what it missed, keyed by app name + address."""

    @pytest.mark.asyncio
    async def test_first_connection_starts_at_the_present(self, proxy):
        server, radio, connect = proxy
        await store_incoming_dm("before anyone connected", 1_700_000_000)
        client = await connect()
        frame = await client.command(APP_START_AS["phone"])
        assert frame[0] == RESP.SELF_INFO.value
        # No MSG_WAITING follows and the inbox is empty: history starts now.
        assert (await client.command(b"\x05"))[0] == RESP.CURRENT_TIME.value
        assert await client.command(b"\x0a") == protocol.encode_no_more_messages()
        from app.repository import VirtualNodeClientRepository

        record = await VirtualNodeClientRepository.get("MeshCore@127.0.0.1")
        assert record is not None
        assert record.connections == 1

    @pytest.mark.asyncio
    async def test_returning_client_gets_what_it_missed_in_order(self, proxy):
        server, radio, connect = proxy
        first = await connect()
        await first.command(APP_START_AS["phone"])
        await first.close()
        await wait_for_clients(server, 0)

        ids = [await store_incoming_dm(f"missed {i}", 1_700_000_000 + i) for i in range(3)]

        second = await connect()
        frame = await second.command(APP_START_AS["phone"])
        assert frame[0] == RESP.SELF_INFO.value
        assert await second.read() == protocol.encode_push_msg_waiting()
        texts = []
        for _ in ids:
            events = await parse_with_library(await second.command(b"\x0a"))
            texts.append(events[0].payload["text"])
        assert texts == ["missed 0", "missed 1", "missed 2"]
        assert await second.command(b"\x0a") == protocol.encode_no_more_messages()
        assert radio.meshcore.sent == [], "replay never touches the radio"

    @pytest.mark.asyncio
    async def test_cursor_persists_so_history_is_not_replayed_twice(self, proxy):
        server, radio, connect = proxy
        first = await connect()
        await first.command(APP_START_AS["phone"])
        await first.close()
        await wait_for_clients(server, 0)
        await store_incoming_dm("once", 1_700_000_000)

        with patch("app.virtual_node.server.CURSOR_PERSIST_DELAY_SECONDS", 0.01):
            second = await connect()
            await second.command(APP_START_AS["phone"])
            assert await second.read() == protocol.encode_push_msg_waiting()
            await second.command(b"\x0a")
            await asyncio.sleep(0.05)
        await second.close()
        await wait_for_clients(server, 0)

        third = await connect()
        assert (await third.command(APP_START_AS["phone"]))[0] == RESP.SELF_INFO.value
        assert await third.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_live_messages_advance_the_cursor_too(self, proxy):
        server, radio, connect = proxy
        first = await connect()
        await first.command(APP_START_AS["phone"])
        live_id = await store_incoming_dm("live", 1_700_000_000)
        server.on_app_event(
            "message",
            {
                "id": live_id,
                "type": "PRIV",
                "conversation_key": KEY_A,
                "text": "live",
                "sender_timestamp": 1_700_000_000,
                "received_at": 1_700_000_000,
                "outgoing": False,
            },
        )
        assert await first.read() == protocol.encode_push_msg_waiting()
        await first.command(b"\x0a")
        await first.close()  # disconnect persists the cursor immediately
        await wait_for_clients(server, 0)

        second = await connect()
        await second.command(APP_START_AS["phone"])
        assert await second.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_replay_limit_keeps_the_newest_messages(self, proxy):
        server, radio, connect = proxy
        server.replay_limit = 2
        first = await connect()
        await first.command(APP_START_AS["phone"])
        await first.close()
        await wait_for_clients(server, 0)
        for i in range(5):
            await store_incoming_dm(f"m{i}", 1_700_000_000 + i)

        second = await connect()
        await second.command(APP_START_AS["phone"])
        assert await second.read() == protocol.encode_push_msg_waiting()
        texts = [
            (await parse_with_library(await second.command(b"\x0a")))[0].payload["text"]
            for _ in range(2)
        ]
        assert texts == ["m3", "m4"]
        assert await second.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_replay_can_be_disabled(self, proxy):
        server, radio, connect = proxy
        server.replay_limit = 0
        first = await connect()
        await first.command(APP_START_AS["phone"])
        await first.close()
        await wait_for_clients(server, 0)
        await store_incoming_dm("lost", 1_700_000_000)
        second = await connect()
        assert (await second.command(APP_START_AS["phone"]))[0] == RESP.SELF_INFO.value
        assert await second.command(b"\x0a") == protocol.encode_no_more_messages()

    @pytest.mark.asyncio
    async def test_different_apps_from_the_same_host_keep_separate_cursors(self, proxy):
        server, radio, connect = proxy
        phone = await connect()
        await phone.command(APP_START_AS["phone"])
        await phone.close()
        await wait_for_clients(server, 0)
        await store_incoming_dm("for the phone", 1_700_000_000)

        cli = await connect()
        assert (await cli.command(APP_START_AS["cli"]))[0] == RESP.SELF_INFO.value
        # First time this app connects: nothing to catch up on.
        assert await cli.command(b"\x0a") == protocol.encode_no_more_messages()

        phone_again = await connect()
        await phone_again.command(APP_START_AS["phone"])
        assert await phone_again.read() == protocol.encode_push_msg_waiting()
        events = await parse_with_library(await phone_again.command(b"\x0a"))
        assert events[0].payload["text"] == "for the phone"
        assert server.status()["clients"][0]["client_id"] in {
            "mccli@127.0.0.1",
            "MeshCore@127.0.0.1",
        }

    @pytest.mark.asyncio
    async def test_outgoing_messages_are_not_replayed(self, proxy):
        from app.repository import MessageRepository

        server, radio, connect = proxy
        first = await connect()
        await first.command(APP_START_AS["phone"])
        await first.close()
        await wait_for_clients(server, 0)
        await MessageRepository.create(
            msg_type="PRIV",
            text="ours",
            received_at=1_700_000_000,
            conversation_key=KEY_A,
            sender_timestamp=1_700_000_000,
            outgoing=True,
        )
        second = await connect()
        await second.command(APP_START_AS["phone"])
        assert await second.command(b"\x0a") == protocol.encode_no_more_messages()


class TestRealLibraryClient:
    """meshcore-py itself, pointed at the virtual node like it would be at a WiFi companion."""

    @pytest.mark.asyncio
    async def test_meshcore_py_connects_and_reads_mirrored_state(self, proxy):
        from meshcore import MeshCore

        server, radio, connect = proxy
        await ContactRepository.upsert(
            ContactUpsert(public_key=KEY_A, name="Alice", type=1, last_seen=1_700_000_000)
        )
        await ChannelRepository.upsert(CHANNEL_KEY, "#test", is_hashtag=True)

        mc = await MeshCore.create_tcp("127.0.0.1", server.port)
        try:
            assert mc.self_info["name"] == "Proxy"
            assert mc.self_info["public_key"] == "cc" * 32

            result = await mc.commands.get_contacts()
            assert result.type == EventType.CONTACTS
            assert KEY_A in result.payload
            assert result.payload[KEY_A]["adv_name"] == "Alice"

            slot = await server._slot_for_channel(CHANNEL_KEY)
            channel = await mc.commands.get_channel(slot)
            assert channel.type == EventType.CHANNEL_INFO
            assert channel.payload["channel_name"] == "#test"

            clock = await mc.commands.get_time()
            assert clock.type == EventType.CURRENT_TIME
            assert abs(clock.payload["time"] - int(time.time())) < 5

            pulled = await mc.commands.get_msg()
            assert pulled.type == EventType.NO_MORE_MSGS
        finally:
            await mc.disconnect()
        assert radio.meshcore.sent == [], "the whole session was answered without the radio"


class TestRealSendPath:
    """The send services for real, over TCP -- no mocked send workflow."""

    @pytest.mark.asyncio
    async def test_dm_send_answers_with_the_radios_own_sent_frame(self, proxy):
        server, radio, connect = proxy
        await ContactRepository.upsert(ContactUpsert(public_key=KEY_A, name="Alice", type=1))
        client = await connect()
        await client.command(APP_START_AS["phone"])

        payload = (
            b"\x02\x00\x00"
            + (1_700_000_000).to_bytes(4, "little")
            + bytes.fromhex(KEY_A[:12])
            + b"hello from the app"
        )
        frame = await client.command(payload)

        events = await parse_with_library(frame)
        assert events[0].type == EventType.MSG_SENT
        # The app needs the radio's real ACK code and timeout, or the message
        # sits at "sending" forever waiting on an ACK that never matches.
        assert events[0].payload["expected_ack"] == b"\x01\x02\x03\x04"
        assert events[0].payload["suggested_timeout"] == 3000
        assert radio.meshcore.sent_direct[0][1] == "hello from the app"
        stored = await MessageRepository.get_all(limit=5)
        assert [(m.text, m.outgoing) for m in stored] == [("hello from the app", True)]

    @pytest.mark.asyncio
    async def test_public_channel_is_always_slot_zero(self, proxy):
        """Regression: clients send on the public channel as index 0, always.

        Slots used to be handed out by sorted key, so a channel whose key sorts
        below the public one took slot 0 and a message sent on Public from an
        app landed on the wrong channel (or on nothing, and was refused).
        """
        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        # Sorts before the public key, and used to win slot 0.
        await ChannelRepository.upsert("0011" + "22" * 14, "#early")
        client = await connect()
        await client.command(APP_START_AS["phone"])

        slots = await server._ensure_channel_slots()
        assert slots[0] == PUBLIC_CHANNEL_KEY
        info = await parse_with_library(await client.command(b"\x1f\x00"))
        assert info[0].payload["channel_name"] == "Public"

        payload = b"\x03\x00\x00" + (1_700_000_000).to_bytes(4, "little") + b"hello channel"
        assert await client.command(payload) == protocol.encode_ok()
        assert radio.meshcore.sent_channel[0][1] == "hello channel"
        stored = await MessageRepository.get_all(limit=5)
        assert stored[0].conversation_key == PUBLIC_CHANNEL_KEY
        assert stored[0].text == "Proxy: hello channel"

    @pytest.mark.asyncio
    async def test_public_channel_reclaims_slot_zero_from_an_earlier_occupant(self, proxy):
        server, radio, connect = proxy
        await ChannelRepository.upsert("0011" + "22" * 14, "#early")
        first = await server._ensure_channel_slots()
        assert first[0] == "0011" + "22" * 14
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        after = await server._ensure_channel_slots()
        assert after[0] == PUBLIC_CHANNEL_KEY
        # The displaced channel keeps a slot rather than falling off the table.
        assert ("0011" + "22" * 14) in after

    @pytest.mark.asyncio
    async def test_slot_assignments_survive_a_restart(self, proxy):
        """Regression: an app caches the slot index, so it has to keep its meaning.

        Slots used to be derived afresh each start, from the channel keys in
        sorted order. Adding a channel that sorted early therefore pushed every
        later channel down one, and an app's cached "slot 2" came back pointing
        at a different channel. The send still went out and was still repeated
        -- encrypted for a channel nobody in that conversation was listening to.
        """
        server, _radio, _connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        await ChannelRepository.upsert("ff" * 32, "Work")
        before = await server._ensure_channel_slots()
        work_slot = before.index("FF" * 32)

        # A channel added later sorts first but must not displace anything.
        await ChannelRepository.upsert("00" * 32, "Family")
        restarted = VirtualNodeServer(radio=_radio)
        after = await restarted._ensure_channel_slots()

        assert after[0] == PUBLIC_CHANNEL_KEY
        assert after[work_slot] == "FF" * 32
        assert ("00" * 32).upper() in after

    @pytest.mark.asyncio
    async def test_a_slot_bound_by_an_app_is_remembered(self, proxy):
        server, _radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        client = await connect()
        secret = bytes.fromhex("ab" * 16)
        payload = bytes([CMD.SET_CHANNEL.value, 5]) + b"Ham".ljust(32, b"\x00") + secret
        assert await client.command(payload) == protocol.encode_ok()
        bound = (await server._ensure_channel_slots())[5]

        restarted = VirtualNodeServer(radio=_radio)
        assert (await restarted._ensure_channel_slots())[5] == bound


class TestRefusalDiagnostics:
    """An app only says "could not send"; the node has to say why."""

    @pytest.mark.asyncio
    async def test_every_command_is_traced_with_what_it_was_answered(self, proxy):
        from app.routers.virtual_node import get_virtual_node_overview

        server, radio, connect = proxy
        client = await connect()
        await client.command(APP_START_AS["phone"])
        # Slot 39 holds no channel.
        payload = b"\x03\x00\x27" + (1_700_000_000).to_bytes(4, "little") + b"hi"
        assert await client.command(payload) == protocol.encode_error(ErrorCode.NOT_FOUND)

        with patch("app.routers.virtual_node.virtual_node", server):
            overview = await get_virtual_node_overview()

        traced = {entry.command: entry for entry in overview.recent_commands}
        # The successful command is traced too: the first question is always
        # whether the app sent anything at all.
        assert traced["APP_START"].result == "SELF_INFO"
        assert traced["APP_START"].failed is False
        send = traced["SEND_CHANNEL_TXT_MSG"]
        assert send.failed is True
        assert send.result == "NOT_FOUND"
        assert "no channel in that slot" in send.detail
        assert send.app_name == "MeshCore"

    @pytest.mark.asyncio
    async def test_a_crashing_handler_is_traced_with_the_exception(self, proxy):
        from app.routers.virtual_node import get_virtual_node_overview

        server, radio, connect = proxy
        client = await connect()
        with patch.object(VirtualNodeServer, "_contacts", side_effect=RuntimeError("boom")):
            assert await client.command(b"\x04") == protocol.encode_error(ErrorCode.BAD_STATE)
        with patch("app.routers.virtual_node.virtual_node", server):
            overview = await get_virtual_node_overview()
        entry = next(e for e in overview.recent_commands if e.command == "GET_CONTACTS")
        assert entry.failed is True
        assert "RuntimeError: boom" in entry.detail

    @pytest.mark.asyncio
    async def test_overview_reports_the_channel_slot_map(self, proxy):
        from app.routers.virtual_node import get_virtual_node_overview

        server, radio, connect = proxy
        await ChannelRepository.upsert(PUBLIC_CHANNEL_KEY, "Public")
        await server._ensure_channel_slots()
        with patch("app.routers.virtual_node.virtual_node", server):
            overview = await get_virtual_node_overview()
        assert overview.channel_slots[0].index == 0
        assert overview.channel_slots[0].name == "Public"


class TestStatusSurface:
    @pytest.mark.asyncio
    async def test_status_reports_listener_and_clients(self, proxy):
        server, radio, connect = proxy
        await connect()
        status = server.status()
        assert status["listening"] is True
        assert status["port"] == server.port
        assert status["client_count"] == 1
        assert status["clients"][0]["peer"].startswith("127.0.0.1:")

    @pytest.mark.asyncio
    async def test_health_payload_includes_virtual_node(self, test_db):
        from app.routers.health import build_health_data

        data = await build_health_data(False, None)
        assert data["virtual_node"] is not None
        assert data["virtual_node"]["enabled"] is False
        assert "clients" not in data["virtual_node"]
