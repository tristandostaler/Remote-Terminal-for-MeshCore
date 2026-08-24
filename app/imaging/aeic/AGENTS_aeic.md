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

## Memory contract (not an optimisation)

Measured peaks: entropy graph alone 0.35 GiB, synthesis decoder alone 2.16 GiB,
both resident **2.44 GiB**. RemoteTerm often runs on a Pi or a small VPS.

* `encode` creates the send-side entropy session and **keeps** it. A second
  photo is then free. The synthesis session is never touched.
* `decode` runs the entropy loop, then **releases the entropy sessions before**
  creating the synthesis session. This release is mandatory.
* Only one entropy direction is ever resident.
* Memory pressure drops the synthesis half first — it is 86% of the cost and the
  send path does not need it.

Everything heavy goes through `asyncio.to_thread` with a semaphore of one. The
event loop is also carrying the radio; a BLE notify stream stalled for the ~5 s
synthesis pass drops mesh traffic.

## Transport: why text and not GRP_DATA

MCO Advanced carries AEIC as a binary GRP_DATA (0x06) chunk stream with data type
`0xAE1C`, via `CMD_SEND_CHANNEL_DATA` (62). RemoteTerm does not, for two
concrete reasons: the Python `meshcore` library does not expose command 62, and
RemoteTerm's channel path only decrypts and ingests GROUP_TEXT (0x05).

So the bitstream is basE91-framed as `aei1:` **text**, the way
`app/compression/mcmp.py` frames compressed prose. That transport already works
on every route: ACKed on DMs, survives the channel crypto path, visible to bots,
stored in the message table. And it fits — 117 B → 144 chars against a 156-byte
budget, so a typical photo is one message.

The chunk-0 metadata byte is deliberately bit-for-bit the same byte MCO Advanced
puts in *its* chunk 0 (`aspect(4) | resolution(2) | rate(2)`), so a future binary
transport or a bridge between the two needs no second definition.

### Planned: migrate to 0xAE1C when command 62 lands

**The text transport is interim.** When `CMD_SEND_CHANNEL_DATA` (62) becomes
available in the Python `meshcore` library, the binary 0xAE1C GRP_DATA transport
takes over the outbound path. It is what MCO Advanced speaks, so AEIC images
start interoperating instead of being RemoteTerm-to-RemoteTerm only, and it drops
basE91's ~23% expansion — a 156-byte bitstream (the measured mean) is *one*
binary chunk but *two* text messages.

`transport.py` is built for that swap and is the only file that should need
editing:

| piece | where | state |
| --- | --- | --- |
| the seam every sender goes through | `select_transport()` | done |
| capability probe for command 62 | `channel_data_transport_available()` | done — returns True on its own once the library exposes it, and selection follows |
| text framing | `TextChunkTransport` | done |
| binary framing + send | `ChannelDataTransport.send` | **to write** — full wire format is in its docstring |

Everything that sends an image — `POST /aeic/send` and the bot
`reply_image`/`send_image`/`send_dm_image` — already goes through
`aeic_service.send_image`, which picks the transport. Neither knows which one it
got, so neither changes.

Three things live outside `transport.py` and still need doing:

1. A GROUP_DATA (0x06) decrypt/ingest route beside the existing GROUP_TEXT
   (0x05) one in `app/packet_processor.py`, dispatching on data type 0xAE1C.
   (Outbound can go via a raw frame the way `app/services/voice.py` builds raw
   DM frames, if the library still lacks command 62.)
2. Reassembly that honours upstream's XOR parity chunk — the binary framing
   carries it and `ingest.py` currently reassembles text chunks only.
3. **Keep `aei1:` decoding inbound.** Peers on the text-only form must keep
   working, so this is an addition, not a replacement; the per-conversation
   selector likely grows a transport choice rather than switching silently.

`AeicStreamMetadata` needs no change: its byte already *is* the upstream one.
`tests/test_aeic_transport.py` pins the shared contract, so a
`ChannelDataTransport` that satisfies those tests is a drop-in.

### One interaction worth knowing about

`aei1:` chunks must never be MCMP-compressed. MCMP v2 has an "only if smaller"
gate and leaves them alone by luck, but **v3 always wraps** — which would inflate
a chunk sized exactly to the 156-byte radio budget and get it truncated,
corrupting the image with nothing raised. `encode_outbound` therefore skips any
already-framed payload (`is_framed_payload`: `aei1`, `IE4:`, `mcmp2:`, `mcmp3:`).

Two things are deliberately absent: **no parity chunk** (upstream spends a third
packet on XOR parity; here an image is 1–2 messages, so parity would cost
+50–100% airtime, and DMs are ACKed) and **no app-level checksum** (the LoRa PHY
CRCs every packet and MeshCore verifies an HMAC, so a corrupt chunk never reaches
this layer). What *is* defended against is two senders colliding on one session
id, which no lower layer can see — hence sessions are keyed by sender **and** id.

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
