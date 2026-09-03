# Virtual companion node (`app/virtual_node/`)

A TCP server that impersonates a MeshCore companion radio so other MeshCore
apps can use *this server* as their radio. It is the MeshCore equivalent of
MeshMonitor's "Virtual Node": one physical radio, many apps, with RemoteTerm
holding the single real connection and shielding the radio from the apps'
traffic.

Off by default (`MESHCORE_VIRTUAL_NODE_ENABLED=true` turns it on; default port
5000, the same one a WiFi companion listens on). It has no authentication,
exactly like a real WiFi companion: trusted networks only.

## Files

- `protocol.py` — pure codecs. Stream framing (`<` host→radio, `>` radio→host,
  2-byte little-endian length), encoders for every frame the node synthesizes
  (`SELF_INFO`, `CONTACT`, `CHANNEL_INFO`, `CURRENT_TIME`, `BATTERY`,
  `MSG_SENT`, `*_MSG_RECV_V3`, pushes) and parsers for the host commands it
  interprets locally (`GET_CONTACTS since`, `ADD_UPDATE_CONTACT`,
  `SET_CHANNEL`, `SEND_TXT_MSG`, `SEND_CHANNEL_TXT_MSG`,
  `SEND_CHANNEL_DATA`), plus the `CHANNEL_DATA_RECV` frame it builds for blobs
  no radio hands back and the index rewrite that re-addresses a real one. Byte layouts are the
  inverse of `meshcore/reader.py` and `meshcore/commands/*.py`; the tests
  round-trip every synthesized frame through the library's own reader.
- `server.py` — `VirtualNodeServer` (singleton `virtual_node`): the asyncio TCP
  server, per-client sessions, command dispatch, the response cache, forwarding,
  the radio frame tap and the app-event mirror.

## How it plugs in

- **Frame tap** (`install_frame_tap`, installed by
  `event_handlers.register_event_handlers` on every connect): wraps
  `reader.handle_rx` like the other reader adapters, so *every* inbound radio
  frame passes through `on_radio_frame` first. It caches raw `SELF_INFO` /
  `DEVICE_INFO` frames, completes the pending forwarded command, and relays
  push frames to clients.
- **App-event mirror** (`websocket.broadcast_event` → `on_app_event`): every
  realtime `message`, `contact`, `channel`/`channel_deleted` broadcast is
  mirrored into the companion protocol. This is how clients get their inbound
  messages — from RemoteTerm's ingest pipeline, not from the radio's queue.
- **Lifespan**: started in `main.py` after the database is up, independent of
  the radio; stopped on shutdown. `GET /api/health` carries a `virtual_node`
  block (enabled/listening/port/read_only/client_count and local/cached/
  forwarded command counters), shown in Settings → About.

## Command policy (the whole point)

Every host command is one of:

| Class | Commands | What happens |
|---|---|---|
| **Local read** | `APP_START`, `DEVICE_QUERY`, `GET_CONTACTS`, `GET_CONTACT_BY_KEY`, `GET_DEVICE_TIME`, `SYNC_NEXT_MESSAGE`, `GET_CHANNEL`, `GET_BATT_AND_STORAGE` (while the 60 s stats sample is fresh) | Answered from RemoteTerm state. No radio I/O. |
| **Local write** | `ADD_UPDATE_CONTACT`, `RESET_PATH`, `REMOVE_CONTACT`, `SET_CHANNEL`, `SET_DEVICE_TIME` (acknowledged, ignored) | Written to the contact/channel store and broadcast to the web UI; the radio is touched only best-effort (`RESET_PATH`, `REMOVE_CONTACT` when the contact is loaded). |
| **Sends** | `SEND_TXT_MSG` (txt_type 0), `SEND_CHANNEL_TXT_MSG`, `SEND_CHANNEL_DATA` | Routed through `services/message_send.py`, the same workflow as the web UI: contact staged on the radio, row stored, `message` broadcast, ACK tracking and retries. A DM is answered with **the radio's own `RESP_CODE_SENT` frame**, captured by the frame tap during the send (`_last_msg_sent`) — it carries the true expected-ACK code and firmware timeout, which is what lets the app move the message from "sending" to "delivered" when the ACK push relays through; the ACK tracker is only a fallback. Channel sends answer `OK`, matching firmware and `meshcore_py.send_chan_msg`. The app's own DM retries (attempt > 0, same dest/timestamp/text) are answered from the pending ACK code instead of sending again. `SEND_CHANNEL_DATA` (62) does not go through the message service at all — see below. |
| **Cached query** | `GET_CUSTOM_VARS`, `GET_ADVERT_PATH`, `GET_TUNING_PARAMS`, `GET_STATS`, `GET_AUTOADD_CONFIG`, `GET_ALLOWED_REPEAT_FREQ`, `GET_DEFAULT_FLOOD_SCOPE`, stale `GET_BATT_AND_STORAGE` | Forwarded once, then served from a 30 s cache keyed by the full command bytes. The cache is re-checked *after* the radio lock is acquired and written before it is released, so N apps asking the same thing at the same moment cost one round trip. Any admin write clears the cache. |
| **Forwarded** | `SEND_SELF_ADVERT`, `SEND_LOGIN`, `SEND_STATUS_REQ`, `LOGOUT`, `BINARY_REQ`, `PATH_DISCOVERY`, `SEND_ANON_REQ`, `SEND_TELEMETRY_REQ`, `SEND_TRACE_PATH`, `SEND_RAW_DATA`, `SEND_CONTROL_DATA`, `SHARE_CONTACT`, `EXPORT_CONTACT`, CLI text (`SEND_TXT_MSG` txt_type 1/2), all `SET_*` config writes, `SIGN_*`, `HAS_CONNECTION`, anything unknown | Raw bytes sent to the radio under the shared `radio_operation` lock; the reply is the first frame whose code is in the command's expected set. Contact-addressed commands re-stage the contact on the radio first (RemoteTerm offloads contacts, so the radio may not have it). Identity-changing writes trigger a `SELF_INFO`/`DEVICE_INFO` refresh and update `radio_manager.path_hash_mode` / `repeat_enabled`. |
| **Refused** | `REBOOT`, `FACTORY_RESET`, `IMPORT_PRIVATE_KEY`; `EXPORT_PRIVATE_KEY` unless `MESHCORE_ENABLE_LOCAL_PRIVATE_KEY_EXPORT=true` | `ERR_CODE_UNSUPPORTED_CMD` / `RESP_CODE_DISABLED`. They would take the radio away from RemoteTerm. |

**Admin commands are off by default.** Every `ADMIN_COMMANDS` entry (name,
location, radio params, TX power, tuning, other params, device PIN, custom
vars, autoadd, default flood scope, signing) is refused with
`ERR_CODE_UNSUPPORTED_CMD` unless the app setting
`virtual_node_allow_admin_commands` (migration 085) is on.

`SEND_SHAPING_COMMANDS` — `SET_FLOOD_SCOPE` and `SET_PATH_HASH_MODE` — are
neither admin commands nor forwarded: they are **acknowledged and dropped**.
They are how a client says "send the next one this way", and MeshCore Open
sends `SET_FLOOD_SCOPE` immediately before every channel message. Both ends of
the obvious treatment are wrong:

- *Refusing* them (they were admin commands once) made the app abandon the
  send. The message never went out and the app reported only that it could not
  send, with the reason nowhere but the server log.
- *Forwarding* them is worse, because it fails silently. Both settings stick on
  the radio until something changes them, and
  `send_channel_message_with_effective_scope` only overrides and restores the
  scope when the channel carries an explicit override — so in the default case
  the app's scope shaped the message, stayed on the radio afterwards, and
  shaped RemoteTerm's own sends too. The packet goes out and is repeated
  normally but is scoped away from the people it was addressed to: sent,
  repeated, never delivered.

Absorbing them keeps the app sending while RemoteTerm shapes its own
transmissions, so a message from an app goes out exactly like one sent from the
web UI. An operator who wants a different scope for a channel sets it in
RemoteTerm, the one place that knows about it.

### `CMD_SEND_CHANNEL_DATA` (62): pictures, and most compressed messages

A GRP_DATA blob is how MCO Advanced puts an **image** on a channel — and, while
its `channelsSendAsBinary` setting is on, which is the **default**, how it sends
every MCMP-compressed channel message too. So this one command is the difference
between an app that can send pictures and compressed text through the node and
one that can only send plain text.

Forwarded blindly it fails, and fails in the two ways this file keeps
describing. The frame names the app's **virtual** slot, which means nothing to
the radio: RemoteTerm holds a handful of physical slots and re-uses them as it
sends, so the blob was refused outright (`ERR_CODE_NOT_FOUND`, "could not send"
in the app and nothing else anywhere) or encrypted for whichever channel
happened to occupy that index and put on the air perfectly, for nobody.

`_send_channel_data` therefore resolves the slot the way a channel *text* send
does, loads the channel into a radio slot with the same planner the AEIC binary
transport uses (`load_channel_for_binary_send`), and re-addresses the frame to
that slot. The blob itself is passed on **verbatim**: the app framed and possibly
signed it, and re-encoding a payload is not something a radio does. Then two
things that no radio does, because nothing else can:

- the blob is **absorbed locally** (`handle_channel_data`), so a compressed
  message from an app becomes a real message row and an image from an app is
  stored and decoded like any other — otherwise a send that worked and one that
  vanished look identical from the web UI;
- it is **relayed to the other apps** on this node (never to the sender, which
  already has it), because they share this radio and will never hear it on air.

### GRP_DATA to apps: frame 27 in, and our own sends out

Inbound `RESP_CODE_CHANNEL_DATA_RECV` (27) is relayed to every client, with the
channel index rewritten to the virtual slot table — the same re-addressing as
above, in reverse, and just as invisible when it is wrong. RemoteTerm's channel
data adapter consumes frame 27 before the library sees it, so the frame tap is
the only place an app can be handed one.

`mirror_channel_data` covers the two arrivals a frame 27 cannot: blobs decoded
off the **RF log** (some firmware never queues GRP_DATA for a companion at all,
so frame 27 never comes) and the blobs **RemoteTerm itself sends** (the AEIC
binary transport calls it per chunk; an app's radio *is* this radio, so it never
hears our transmissions). One blob can reach the relay by more than one of these
routes, so the last few are remembered and relayed once.

Marker rows (`aeib:`, `mediax:`) are **not** mirrored as messages. They stand in
for media that never crossed the air as text and are a convention between this
server and its own web UI; sending one to an app puts the literal string
`aeib:17` in someone's conversation. The app gets the blobs instead.

`IMPORT_CONTACT` is contact management rather than radio configuration, and
sits with the other contact writes. Read-only mode refuses the transmit,
admin and local-write commands; send shaping is still answered there, because
refusing the parameter stops the app before it can be told plainly that the
*send* is what is not allowed. It is an
*app* setting rather than an env var so the operator can flip it from
Settings → Virtual Node while apps are connected; the server reads it per
command (`admin_commands_allowed`), so a change takes effect on the next
command. `MESHCORE_VIRTUAL_NODE_READ_ONLY=true` wins over it and additionally
refuses every transmit and local-write command; apps become viewers.

## Operator surface

`app/routers/virtual_node.py`:

- `GET /api/virtual-node` — one payload for the settings page: listener state,
  counters, `admin_commands_allowed`, the connected sessions (peer, client
  id, app name, connected since, commands, queued, replayed) and every
  remembered client from `virtual_node_clients` with a `connected` flag.
- `DELETE /api/virtual-node/clients/{client_id}` — forget a remembered app;
  its next connection starts at the present.
- `POST /api/virtual-node/connections/{peer}/disconnect` — close one app's
  socket (`VirtualNodeServer.disconnect_peer`); its cursor is persisted on the
  way out like any disconnect.

The overview also carries the **channel slot map** and a trace of the **last 60
commands** (`_note_command`: command name, the response frame or `ERR_CODE_*`
it was answered with, reason on failure, app, duration). The app is the one
piece of this we cannot see into, and it reports "could not send" and nothing
more — so the questions are whether it sent the command at all and what came
back, and that answer used to exist only in the server log. Successful commands
are traced too for exactly that reason; the settings page filters to failures.

The switch itself is `PATCH /api/settings` with
`virtual_node_allow_admin_commands`. The frontend section is
`frontend/src/components/settings/SettingsVirtualNodeSection.tsx` (polls the
overview every 5 s while open).

## Why the reply matching is safe without pausing auto-fetch

Forwarding holds the operation lock, so RemoteTerm's own command sequences
never interleave. The one thing that runs *without* the lock is the library's
auto message fetch (`get_msg` on `MESSAGES_WAITING`), whose replies are frames
7/8/10/16/17/27. Those are in `_NEVER_A_FORWARD_RESPONSE` and every
forwarded command has an explicit expected-code set, so a concurrent fetch
cannot be mistaken for the answer. Pushes (`>= 0x80`) never match.

## Inbound messages: per-client queues

Each client has a bounded inbox (500 live frames; a history replay may fill it
further up to the replay limit). An incoming `message` broadcast
(not outgoing, not a reaction) becomes a `CONTACT_MSG_RECV_V3` or
`CHANNEL_MSG_RECV_V3` frame appended to every inbox, followed by a
`PUSH_CODE_MSG_WAITING`; `SYNC_NEXT_MESSAGE` pops one or answers
`NO_MORE_MSGS`. Channel frames carry the *virtual* slot index (below).

Repeater CLI replies are the exception: RemoteTerm's auto-fetch pulls them
and `on_contact_message` drops txt_type 1, so the tap enqueues those raw
frames (7/16 with txt_type 1) verbatim — otherwise an app's repeater
dashboard would never see an answer.

The radio's own `PUSH_CODE_MSG_WAITING`, `PUSH_CODE_ADVERT` and
`PUSH_CODE_NEW_ADVERT` are **not** relayed: message-waiting is synthesized
from the inboxes, and adverts are synthesized from `contact` broadcasts (only
for full 64-hex keys), so clients are not told twice. Every other push
(ACK, PATH_UPDATE, LOGIN_*, STATUS/TELEMETRY/BINARY responses, TRACE_DATA,
LOG_DATA, CONTROL_DATA, ...) is relayed to every client as-is.

## Virtual channel slots

Apps address channels by a 1-byte slot index, but the server keeps far more
channels than the radio has slots. `_channel_slots` is a **durable** table,
persisted in `virtual_node_channel_slots` (migration 086): channels get a slot
on first sight (sorted by key), keep it across restarts, and a deleted channel
leaves a blank slot that is reused only once the table (255) is full.

The persistence is the point, not a convenience. An app caches the slot index
along with the channel name it read once, and re-derived-on-boot slots shift
whenever the channel set has changed — sorting a newly added key in front of
the others pushes every later channel down one. An app's cached "slot 2" then
addressed a different channel, and the send still went out and was still
repeated, encrypted for a channel nobody in that conversation was listening
to. `SEND_CHANNEL_TXT_MSG` records the channel a slot resolved to in the
activity trace for the same reason: a message delivered to the wrong channel
and one delivered correctly are indistinguishable from the app's side.

**Slot 0 is always the public channel** (`_pin_public_channel_slot`), the way
firmware lays it out. Clients send on the public channel as index 0 without
ever reading the slot back, so assigning by sorted key alone gave slot 0 to
whichever channel sorted first — a `#hashtag` channel about half the time,
including the `#remoteterm` channel a default install creates — and a message
sent on Public from an app went to the wrong channel or to an empty slot,
which the node refused outright. The pin also survives an app clearing slot 0
with `SET_CHANNEL`; it is re-seated on the next reconcile, and any occupant it
displaces keeps a slot rather than falling off the table. `DEVICE_INFO` is
rewritten to advertise `max(40, radio max_channels, slots used)` so apps keep
probing new slots. `SET_CHANNEL` binds a slot and upserts the channel;
clearing a slot (blank name, zero secret) only unbinds it — the channel and
its history stay on the server, because deleting server data from a proxy
client is not something a blank frame should do.

## Identity while the radio is down

`SELF_INFO` is answered from the last raw frame seen (or re-encoded from
`mc.self_info`), and `DEVICE_INFO` from the last raw frame seen, so apps can
connect, browse contacts/channels and read history while the radio is
disconnected. Forwarded commands then answer `ERR_CODE_BAD_STATE`.

## History replay and client identity

The protocol has no client identity. `CMD_APP_START` carries only the app's
*name* (`[ver][6 reserved][name...]`, e.g. `MeshCore`, `mccli`), so the most
stable handle available is that name plus the address the connection came
from: `client_id = "<app_name>@<peer_host>"`. It is a heuristic and is
documented as one — two phones on the same app *and* the same IP share a
cursor, and a phone that changes IP starts over.

Cursors live in `virtual_node_clients` (migration 084, repository
`VirtualNodeClientRepository`): one row per client id with
`last_message_id`, first/last seen and a connection counter. On the first
`APP_START` of a TCP session:

1. The client is looked up or created. A **first-time** client starts at the
   present: its cursor is set to the newest message id and nothing is
   replayed — a brand-new app should not receive a thousand old messages as
   if they had just arrived.
2. A **returning** client gets the incoming, non-reaction messages with
   `id > cursor`, oldest first, as regular inbox frames followed by one
   `MSG_WAITING` push. `MESHCORE_VIRTUAL_NODE_REPLAY_LIMIT` (default 1000, 0
   disables) caps it; when more were missed, the *newest* `limit` are
   delivered so the app lands on the present, and the cursor jumps past the
   skipped ones (`MessageRepository.get_incoming_after_id`).
3. The inbox is cleared before replay: whatever was queued before the app
   identified itself is either inside the replay window or predates this
   client.

Each inbox entry is `(message_id | None, frame)`. When the client pulls one
with `SYNC_NEXT_MESSAGE` the cursor advances to that id (`_note_delivered`)
and is written back after a 1 s debounce, on disconnect, and on server
stop — so a full replay costs one write, and a client that disconnects
mid-drain resumes from what it actually pulled. Raw relayed frames (CLI
replies) carry `None` and never move the cursor. Outgoing messages are never
replayed, matching the firmware, which never hands the host its own sends.

## Not done / known gaps

- An image RemoteTerm sends in a **direct message** goes out as `aei1:` text,
  which is a RemoteTerm-only dialect: MCO Advanced has no DM image transport at
  all (its own picture button exists only in a channel chat), so it shows those
  chunks as text it cannot read. Nothing on this side fixes that; on channels
  both ends speak the binary `0xAE1C` form.
- What an app sends as GRP_DATA is stored as an *arrival* rather than as one of
  this node's own sends -- a picture because `channel_data_ingest` has one shape
  for a picture and it is the inbound one, a compressed message because it is
  written by the same ingest. It carries the sender name the app put in the
  envelope, which is this node's name. The alternative was not storing it at all.
- An MCMP message an app sends as *text* (with MCO Advanced's binary setting
  turned off, or in a DM) is stored as the basE91 body rather than as its words:
  the send services compress on the way out and nothing decodes on the way in
  for an outgoing row. It goes out correctly and peers read it; RemoteTerm's own
  conversation, search, bots and fanout see `mcmp2:...`.
- An app cannot choose the flood scope or path hash mode for its own send:
  both are acknowledged and dropped, and the message goes out under
  RemoteTerm's scope for that channel. Per-channel overrides live in
  Settings → Channels.
- `IMPORT_CONTACT` is forwarded to the radio only; the periodic contact sync
  picks the contact up into the store afterwards.
