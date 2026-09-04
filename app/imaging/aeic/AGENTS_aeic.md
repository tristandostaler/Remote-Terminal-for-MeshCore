# AEIC neural image codec

**Read this before changing anything below `service.py`.**

AEIC-SE is the "AI reconstructs the image" codec from meshcore-open's MCO
Advanced fork (branch `origin/rename-mco-advanced`). A 512×512 colour photo
becomes a **117–209 byte** rANS bitstream; the receiver turns it back into a
picture by running an ~886M-parameter ONNX synthesis network.

It is *generative*, not pixel-faithful — measured PSNR is ~17–20 dB. The reader
gets a recognisably-the-same scene, not the same pixels. That is the trade the
codec exists to make, and it is why the UI says "AI reconstruction" rather than
implying a normal photo.

## Why it is worth a 958 MiB dependency

| | IE4 (`app/image_protocol.py`) | AEIC |
| --- | --- | --- |
| picture | 256×256 greyscale | 512×512 colour |
| on air | envelope + 15–40 raw fragments | **1–2 text messages** |
| transport | raw packets + fetch/retry protocol | ordinary messages, ACKed on DMs |
| receiver needs | an AVIF/JPEG decoder | the 958 MiB model bundle |

## THE FAILURE MODE IS SILENT

This is the single most important thing to know. AEIC's rANS coder is
*synchronous with the entropy model*: the decoder re-runs `h_s`, `g_c` and the
adapters to reproduce the exact symbol probabilities the encoder used. If the two
sides disagree anywhere — one wrong symbol position, one ULP of float drift —
the coder desynchronises and emits a corrupt latent. **Nothing raises.** You get
a sharp, plausible, wrong image.

Upstream measured a 2.76e-7 drift in one convolution corrupting 15,728 of 65,536
latents with the decode reporting success.

So:

* Never "clean up" the arithmetic in `entropy.py`. The float association in
  `squeeze` (`(g0+g1)+(g2+g3)`), the truncation in `build_indexes`, and the
  float32-ness of every intermediate are all load-bearing.
* **Never lower `graph_optimization_level`.** See below.
* Treat any change here as needing the golden fixtures to re-pass, not a
  read-through.

### The ONNX Runtime session-option trap

Leave `graph_optimization_level` at ORT's **default**. This is the opposite of
the instinct, and it is measured, not guessed:

| level | agreement with the reference tensors |
| --- | --- |
| default (`ORT_ENABLE_ALL`) | **bit-identical** |
| `ORT_DISABLE_ALL` | ~63,000 of 65,536 values differ, up to 6.6e-07 |
| `ORT_ENABLE_BASIC` | same |
| `ORT_ENABLE_EXTENDED` | same |

"Turn optimisation off to be deterministic" is exactly the change that breaks
this codec. `tests/test_aeic_onnx.py` pins it. Thread count, by contrast, does
*not* affect the result — the entropy sessions are pinned to one thread for
predictable CPU use and that was verified bit-identical.

## Layer map

Bottom-up. The bottom layers are stdlib-only on purpose, so the wire format
stays testable on an install that never opted into the `aeic` extra.

| module | what it owns | needs |
| --- | --- | --- |
| `tables.py` | the CDF table file parser | stdlib |
| `rans.py` | the rANS coder, byte-identical to the C++ reference | stdlib |
| `png.py` | a minimal PNG writer for decoded output | stdlib |
| `text_transport.py` | the `aei1:` basE91 message framing | stdlib |
| `channel_data.py` | binary GRP_DATA framing, XOR parity, companion frames | stdlib |
| `channel_data_ingest.py` | inbound GRP_DATA reassembly and storage | stdlib |
| `channel_data_text.py` | MCMP text riding the same GRP_DATA envelope | stdlib |
| `entropy.py` | masks, squeeze, `build_indexes`, the four-stage loop | numpy |
| `onnx_backend.py` | ORT sessions and tensor marshalling | numpy + ORT |
| `bundle.py` | the model registry, digests, resumable download | httpx |
| `ingest.py` | inbound chunk reassembly | — |
| `service.py` | process-wide orchestration; what routers call | — |

Routers call **only** `service.py` and `ingest.py`.

## The four-stage masked context model

The codec codes the latent in five coder calls, always in this order:

```
z, y0, y1, y2, y3
```

`z` is the hyper-latent. Each `y` stage codes one checkerboard quarter of the
256-channel latent: the channels are split into four groups, and each group
carries one 2×2 micro-pattern. The stage↔group↔pattern mapping is
`micro = group XOR perm[stage]` with `perm = (0, 3, 2, 1)`.

Encoding is one forward pass — the encoder already knows `y`. **Decoding is
inherently sequential**: stage *i*'s CDF rows come from scales that only exist
once stages *< i* have been decoded and pushed back through the context model.
That is why there are two entropy graphs, not one, and why `decode_to_latent`
interleaves network calls with rANS.

## The bundle has two halves, and only one is a decision

| half | files | disk | memory | who chooses |
| --- | --- | --- | --- | --- |
| send | `aeic_entropy_side_fp32_op17.onnx`, `aeic_cdf_ft32.bin` | 65 MiB | ~0.35 GiB | nobody: fetched automatically |
| receive | the 832 MiB weights, its graph, the decode-side graph | 893 MiB | ~1.4 GiB **per picture** | an explicit button |

`SEND_HALF_ASSETS` is defined as exactly what `supports_encode` needs, and a test
pins that: a file in there that encoding does not need is 64 MiB of somebody's
uplink spent for nothing.

`ensure_send_half_installed()` runs at startup and again from `_require_ready`
when a send finds the half missing, so a gateway that had no uplink at boot fixes
itself on first use rather than needing a restart. It is refused by a missing
runtime and by a download already in flight -- and *not* by
`MESHCORE_ENABLE_AEIC=false`, which is the next section.

## `MESHCORE_ENABLE_AEIC` switches off rebuilding, not the codec

`false` means "never reconstruct a received picture". It does not stop sending:
that is the 65 MiB half and ~0.35 GiB, none of it the synthesis weights, and the
host most likely to have turned rebuilding off is exactly the host that still
wants to send. So the switch is read in `unavailable_reason` **only** when
`for_decode`, `status()` reports it as `reconstruction_enabled` separately from
`runtime_available` (which is the dependency and nothing else), and the download
route refuses `scope=full` while allowing `scope=send`.

Two consequences worth keeping straight:

* A received picture under `false` is *kept*, not dropped -- switching the value
  back on decodes it later. The refusal sentence says so.
* On a fresh install `false` still means nothing at all, because `run.sh` only
  installs the ~120 MiB extra when the value is on. What `false` cannot do is
  uninstall what an earlier `true` already put there, and that server -- extra
  present, rebuilding unaffordable -- is the one this distinction is for.

The halves compose (`download_bundle(assets=...)` skips whatever is installed and
intact) but are **never mixable across checkpoints**: every asset here is
per-checkpoint, and a send half from one with a receive half from another
desynchronises rANS silently. Progress is reported against the half in flight --
65 MiB measured against 958 MiB reads 3% and then stops, which looks exactly like
a download that died.

## Memory contract (not an optimisation)

Measured peaks: entropy graph alone 0.35 GiB, synthesis decoder alone **~1.3 GiB
with the weights mapped** (~2 GiB when ORT prepacks them onto the heap, which is
why `_session_options(mmap_weights=True)` tells it not to -- see the cap-by-cap
table in `memory.py`). RemoteTerm often runs on a Pi or a small VPS.

**A decode runs in a worker process** (`decode_worker.py`), because ~1.4 GiB
end to end is more than some hosts have and the OOM killer takes the biggest process: that used
to be uvicorn, so one received picture killed the server and its radio link. The
worker also raises its own `oom_score_adj`, returns every byte to the OS when it
exits, and keeps inference sessions out of the server entirely on the receive
path. `service._decode_in_process` remains for hosts that cannot spawn at all,
and is the only reason the release discipline below still matters.

A worker that *starts and dies* is never retried in-process. The likeliest reason
it died is that the host is too small, and retrying in the server's own process
is precisely how the server gets killed instead. Only an OS-level refusal to
spawn falls back.

Below ~900 MB free, `unavailable_reason(for_decode=True)` refuses up front and
says so with both numbers. `memory.py` reads the cgroup limit as well as
`MemAvailable`, because inside a container the latter reports the *host's* free
memory -- a 512 MB container on a 16 GB box reads as roomy and then gets killed.
Sending stays available either way: encoding never touches the synthesis graph.

* `encode` creates the send-side entropy session and **keeps** it. A second
  photo is then free. The synthesis session is never touched.
* `decode` runs the entropy loop, then **releases the entropy sessions before**
  creating the synthesis session. This release is mandatory.
* Both releases also sit in a `finally`. A decode failure is *swallowed* by
  `ingest.decode_session`, so a session still held after a raise stays held for
  the life of the process — and the next decode then stacks the entropy graph on
  top of the 2.16 GiB synthesis one, which is the 2.44 GiB this contract exists
  to prevent. Do not move them back onto the success path.
* Only one entropy direction is ever resident.
* Memory pressure drops the synthesis half first — it is 86% of the cost and the
  send path does not need it.

Everything heavy goes through `asyncio.to_thread` with a semaphore of one -- the
worker spawn included, since it blocks for as long as the decode. The event loop
is also carrying the radio; a BLE notify stream stalled for the ~5 s synthesis
pass drops mesh traffic, and on a Pi paging mapped weights it is minutes.

## Two transports, and which one carries what

An AEIC bitstream reaches the air one of two ways. Everything that sends an
image — `POST /aeic/send` and the bot `reply_image`/`send_image`/`send_dm_image`
— goes through `aeic_service.send_image`, which picks via `select_transport()`;
no call site knows which it got.

| transport | payload | used for |
| --- | --- | --- |
| `ChannelDataTransport` | binary GRP_DATA (0x06), data type `0xAE1C` | **channels** — the default |
| `TextChunkTransport` | `aei1:` basE91 text messages | **DMs**, and channels when the binary one is refused |

**Binary is preferred on channels because it is the interoperable one.** It is
what MCO Advanced actually speaks, so images render on a peer's phone instead of
arriving as a line of basE91; and it avoids basE91's ~23% expansion, which puts
the measured 156-byte mean into *one* chunk where text needs two.

**Direct messages are always text.** GRP_DATA is a group payload type; there is
no DM equivalent.

The chunk-0 metadata byte is bit-for-bit the same in both (`aspect(4) |
resolution(2) | rate(2)`), so nothing has to be re-derived when crossing between
them.

### The library does not need to expose command 62

It was previously believed this was blocked on meshcore-py growing a
`send_channel_data` helper. It is not: `commands.send` is a **generic
dispatcher**, so `channel_data.build_send_command` builds the frame
(`[0x3E][channel_idx][0xFF][type_lo][type_hi][blob]`) and hands it over directly.

What genuinely cannot be determined from Python is whether the *firmware*
implements command 62 — there is no capability flag. The radio answers by
rejecting the first blob, and that is why `AeicChannelDataUnsupported` is a
distinct exception: it is raised **only** when blob 0 was refused, i.e. nothing
reached the air, so `send_image` can cleanly fall back to text. A failure on any
later blob raises the plain `AeicTransportUnavailable` and is NOT retried,
because part of the image is already out and resending would duplicate it.

### Receiving: a hook, because meshcore-py drops frame 27

Inbound GRP_DATA arrives as companion frame `RESP_CODE_CHANNEL_DATA_RECV` (27).
meshcore-py's `PacketType` jumps straight from 26 to 28, so such a frame reaches
`reader.handle_rx`'s final `else` and is logged away as "Unhandled packet type" —
which is exactly why an image sent from MCO Advanced produced *nothing* in
RemoteTerm, not even garbled text.

`event_handlers.install_channel_data_adapter` wraps `handle_rx` to intercept it,
the same strategy (and idempotency flag) `install_full_raw_data_adapter` already
uses for a different library gap.

**This path deliberately does no crypto.** The firmware has already decrypted the
packet and split off the data type. We do *not* add a raw-RF GROUP_DATA decoder
beside the GROUP_TEXT one, because the on-air plaintext layout for GRP_DATA is
not documented in any source we can check — MCO Advanced never sees it either —
and guessing where the blob starts inside the plaintext does not fail loudly in
this codec, it reconstructs a sharp, wrong picture. The cost of that choice: this
only sees frames the *local* radio decrypted, so the channel must be loaded in
one of its slots. `radio.channel_key_for_slot` maps the frame's slot index back
to a channel; a slot this process never loaded is logged and skipped.

### Who sent a picture: the 2-byte prefix, and when it is us

Every chunk header carries `senderPublicKey[0..1]`, and `_attribute_image` reads
it three ways. It is **ours** when it matches this radio's prefix — which is what
an app on the virtual node sends, since its identity is this radio's — so the row
is stored `outgoing`, the way an app's *text* message already is. Otherwise
`ContactRepository.get_by_key_prefix` names the peer, declining an ambiguous
match rather than guessing. Failing both, the row stays unattributed.

The same prefix is the RF echo filter in `packet_processor`, and both call
`self_sender_prefix()` so they cannot disagree about what "ours" means. Before
this, an image row was written with no sender at all and the client fell through
its whole resolution chain to the conversation key — rendering a channel's shared
secret as the author.

### More than one image codec rides GRP_DATA

MCO Advanced ships **AEIC** (`0xAE1C`) *and* **MCOimg** (`0xFFF0`), plus MCMP
text (`0xFFF1`), plus its own official application type **`0x0120`** which
supersedes the two `0xFFF*` developer types and carries MCOimg (subtype 1) or
MCMP (subtype 2) inside a `nameLen | name | subtypeVersion | body` envelope. Only
AEIC is the image codec RemoteTerm has. `channel_data_ingest` recognises the
others by type — reading `0x0120`'s subtype nibble to name it — and reports them
as unsupported rather than handing them to the AEIC decoder, which would turn
them confidently into garbage.

**MCMP is the exception, because we do have that codec.** `channel_data_text.py`
unwraps the envelope (`senderNameLen varuint | senderName | [subtypeVersion] |
body`) and decodes the body with `app.compression.mcmp` — the same bodies the
`mcmp2:`/`mcmp3:` text transports carry, without the basE91 wrapper — and
`handle_channel_data` stores the result as an ordinary channel message. This is
not a corner: `channelsSendAsBinary` is **on by default** in MCO Advanced, so
until this landed every compressed channel message from a current build was
named in a log line and dropped. The arithmetic decoder is not self-checking, so
a body that decodes to something that is not prose is refused and the blob falls
back to being kept as unsupported media.

**AEIC did not move into `0x0120`.** Upstream's `channel_app_data_helper.dart`
defines subtypes for MCOimg and MCMP only, and `image_chunk_transport.dart` still
puts AEIC on the air as a bare `0xAE1C`. Worth re-checking on an upstream bump: if
AEIC ever did move, every inbound AEIC image would be dropped as an unknown type,
and the symptom would be indistinguishable from the codec mismatch below.

### A received channel image is announced as a message

`_store_and_decode` mints the `aeib:` marker row and then has to **push it**. It
used to write the row and stop: the only event it emitted was
`aeic_image_session`, which **no client code handles** — the bubbles poll over
HTTP instead — so a picture received on a channel appeared no earlier than the
next fetch of that conversation. Sitting in the channel it arrived on you saw
nothing at all, which made a working transfer indistinguishable from a dropped
one, and made a stored-but-undecodable image look like silence rather than a
reason. `_announce_marker_row` broadcasts it as an ordinary `message`, the way
every other inbound message reaches the UI.

### One picture, one completion

A one-data-chunk image — upstream's *typical* ft32 size, its own capacity note
puts the mean at 155.8 B — goes out as **two** packets: the data chunk and the
parity chunk. It used to complete on both. The data chunk finished it and the
pending entry was dropped; the parity chunk then started a fresh entry in which
the single missing body was recoverable *from parity alone*, so it finished again
and the caller minted a second message row and a second session. One picture, two
identical bubbles, for the commonest image size there is. Multi-chunk images never
showed it, because parity alone cannot rebuild two missing bodies.

`ChannelDataReassembler._completed` remembers finished images for
`SESSION_TTL_SECONDS` and ignores later chunks of them. It matches on the chunk
`total` as well as the key, so a genuinely different image that reused the id
inside the window is still accepted — the same signal the "restarted with a new
chunk count" reset path already trusts. The map is capped at
`MAX_PENDING_IMAGES`, since a peer cycling image ids would otherwise grow it.

Note it completes on the last *data* chunk, not on the parity chunk. Parity is
redundancy; waiting for it would delay every image by a packet.

### An undecodable image is kept, and gets a box

A picture in a codec this build has no decoder for used to be identified, refused
and dropped. Correct, and invisible twice over: nothing in the conversation said a
picture had been sent, and the bytes were gone, so adding the decoder later could
not bring back a single image already received.

`_note_undecodable` now does three things for an image. It **keeps the payload**
(`UnsupportedMediaRepository`, migration 079) verbatim and in arrival order —
verbatim because a format we cannot parse is one we must not normalise, and a
future decoder needs exactly what the radio handed us. It **mints a marker row**,
`mediax:<id>`, on the first blob of an arrival, so the conversation shows a box
saying a picture came in and why it is not shown. And it logs at INFO.

**Nothing expires on a timer.** The arrival is pinned to its marker message and
cascades from it (the same rule migration 075 uses for image and voice sessions),
so deleting that message is the way to reclaim the space — and the only way. That
is deliberate: the value of these bytes is that they are still there when support
arrives, which may be a long time, so a TTL would quietly defeat the feature. The
cost is that a channel carrying foreign images grows the table until those
messages are deleted.

Grouping is a heuristic, and it is confined to one column. An unknown format gives
no image id and no chunk count, so blobs of the same type on the same channel
within `BLOB_GROUPING_WINDOW_SECONDS` are treated as one arrival; a gap starts a
new one. Grouping too eagerly costs one box covering two pictures, never a wrong
decode. `MAX_BLOBS_PER_ARRIVAL` bounds what one arrival can accumulate, since
every blob extends the window.

Text over GRP_DATA is **not** kept and stays at debug: it arrives decoded by other
means, so storing it would be hoarding rather than recovery.

`POST /api/unsupported-media/{id}/decode` is wired up knowing it will fail today.
Without it, the box would have no way to tell the reader anything changed and the
kept bytes would be unreachable; when a decoder lands it is called from there and
every arrival already in the database becomes readable.

### An undecodable image says so, at INFO

A picture in a codec this build cannot decode used to be dropped at DEBUG, and
the root logger defaults to INFO — so a frame that was correctly identified and
correctly refused vanished without a trace anywhere, `/api/debug` included. "I
sent a picture from MCO Advanced and nothing happened" was the whole diagnostic
surface, and the cause (its photo codec was MCOimg, not AEIC) was unguessable.

`_note_undecodable` now reports it at INFO and names the way out. Two things keep
it from becoming noise: only a dropped **image** is raised (`carries_an_image`) —
text over GRP_DATA is ordinary traffic that also arrives by other means — and it
is suppressed per `(channel, data type)` for
`UNDECODABLE_NOTICE_INTERVAL_SECONDS`, because one image is up to sixteen chunks
and every one of them lands here. The window is deliberately not permanent: trying
again after changing a setting has to say something, or a second attempt looks
identical to a dead one.

The matching UI half is in the conversation features modal: on a **channel**, it
states that AEIC is the only photo codec MCO Advanced also reads, whichever codec
is currently picked. The default is Standard, so the interoperable choice is the
one nobody would guess they had to make.

### A received binary image has no message text

Nothing textual crossed the air, so unlike the `aei1:` path there is no body to
keep. The backend writes a synthetic message row whose text is the local marker
`aeib:<session_key>` purely to give the picture a place in the conversation; the
frontend matches it with `parseAeicBinaryRef`. **It is a local server↔UI
convention and never goes on air** — do not mistake it for a wire format.

### Parity is on for binary, off for text

The binary framing spends a third packet on upstream's XOR parity chunk, and
`channel_data.recover_missing_body` implements its single-loss recovery including
every guard — notably that only the LAST data chunk may be short, without which a
flipped bit in the parity length byte yields a truncated image reported as
complete. Text framing still omits parity: there each chunk is an ACKed, retried
message, whereas a GRP_DATA blob is fire-and-forget.

### One interaction worth knowing about

`aei1:` chunks must never be MCMP-compressed. MCMP v2 has an "only if smaller"
gate and leaves them alone by luck, but **v3 always wraps** — which would inflate
a chunk sized exactly to the 156-byte radio budget and get it truncated,
corrupting the image with nothing raised. `encode_outbound` therefore skips any
already-framed payload (`is_framed_payload`: `aei1`, `IE4:`, `mcmp2:`, `mcmp3:`).

The bot engine's **profanity filter** needs the same guard, for the same reason.
Its word list is `\b`-anchored and basE91 is full of non-word characters, so a
payload can contain a bare match: censoring substitutes bytes inside the stream
(same length, different bytes → garbage image), and "drop" mode removes one chunk
of an image whose other chunk already went out. `send_bot_message` skips
moderation for a framed payload and moderates prose exactly as before.

Two things are deliberately absent: **no parity chunk** (upstream spends a third
packet on XOR parity; here an image is 1–2 messages, so parity would cost
+50–100% airtime, and DMs are ACKed) and **no app-level checksum** (the LoRa PHY
CRCs every packet and MeshCore verifies an HMAC, so a corrupt chunk never reaches
this layer). What *is* defended against is two senders colliding on one session
id, which no lower layer can see — hence inbound sessions are keyed by sender
**and** id.

**Outbound sessions must not use that key.** The wire id is 1296 values because
all it has to be is unique per sender inside one receiver's reassembly window; as
a local storage key under a constant `self` prefix it collided at roughly 14% for
twenty photos a day, and silently — the second send passed `create_session`'s
metadata check, overwrote the first's bitstream, and `COALESCE(message_id, ?)`
kept the first message on the row, so the older bubble rendered the newer
picture. `outgoing_session_key` keys on the sent message's id instead.

A session that **cannot** be decoded records the reason via
`store_decode_error`. Without it the row reads `decoded=false,
decode_error=null`, which the UI cannot tell apart from "the 5 s synthesis pass
is still running" — so a server without onnxruntime left every received image
polling once a second for a full minute. `store_png` clears the field, so the
retry path is unaffected.

## Interop caveat

`build_indexes` calls `log` on float32. numpy's and ORT's agree on the whole
reference corpus, but the measured boundary margin is only 1.12× — the tightest
number in the system. Two RemoteTerm instances always agree (same code); against
an MCO Advanced peer, a scale sitting within one ULP of a bucket edge is the one
place a divergence could appear.

## Validating a change

```bash
# Wire format and framing; no extra, no model.
.venv/bin/python -m pytest tests/test_aeic_rans.py tests/test_aeic_tables.py \
    tests/test_aeic_text_transport.py

# Entropy arithmetic; needs numpy.
.venv/bin/python -m pytest tests/test_aeic_entropy.py

# The strongest tests. Needs the .aeicrec recordings (see tests/aeic_fixtures.py)
# and, for the live-graph half, the installed bundle.
.venv/bin/python -m pytest tests/test_aeic_onnx.py
```

`tests/test_aeic_onnx.py` skips silently when the recordings or the bundle are
absent, so a green run locally does **not** mean the codec was exercised. Check
for skips before trusting a change to `entropy.py` or `onnx_backend.py`.

## Installing it

Dependencies (onnxruntime + numpy + Pillow):

| deployment | how |
| --- | --- |
| Docker / compose / HA add-on | `MESHCORE_ENABLE_AEIC=true` — `run.sh` installs on first start, no rebuild |
| clone-and-build | `uv sync --extra aeic` |
| systemd installer | `MESHCORE_ENABLE_AEIC=1 bash scripts/setup/install_service.sh` |
| pre-baked image | `docker build --build-arg ENABLE_AEIC=1 .` (makes the runtime step a no-op) |

Then download the bundle from the conversation features panel, or
`POST /api/aeic/model/download`. It lands in `settings.aeic_model_dir`
(`data/models/aeic` by default) and every file is SHA-256 verified — a table set
that disagrees with the checkpoint is the silent-corruption case, so there is no
skip-verification path.

### THE EXTRA MUST STAY GENUINELY OPTIONAL

`app/services/messages.py` imports `note_inbound_chunk` at module level, so this
package is on the import path of the **whole application**. If importing it pulls
numpy, a base install cannot start at all — uvicorn dies with
`ModuleNotFoundError: No module named 'numpy'` before the radio connects.

That shipped once and no test caught it, because numpy is installed in the dev
environment and in CI. So:

- `constants.py` holds every shape, graph-IO name and the runtime probe, and is
  **stdlib-only**. Light modules take what they need from there.
- `entropy.py` and `onnx_backend.py` are the only modules allowed to import
  numpy / onnxruntime at module level, and nothing light may import them at
  module level — `service.py` reaches them inside the functions that run
  inference.
- `prepare.py` imports Pillow inside the function, and passes raw pixels through
  without touching it at all.
- `tests/test_aeic_optional_extra.py` enforces this **statically**, by parsing
  the import graph. It is the only way to catch it without a second,
  dependency-free environment.
