"""The AEIC model bundle: what it is made of, where it lives, how it is fetched.

The codec is not one file. It is a five-asset bundle totalling 958 MiB:

======================================  =========  ==============================
asset                                   size       role
======================================  =========  ==============================
``aeic_decoder_qdq_conv_pct.onnx``       2.9 MiB   synthesis graph
``aeic_decoder_qdq_conv_pct.onnx.data``  832 MiB   its external weights
``aeic_entropy_side_fp32_op17.onnx``      64 MiB   send-side entropy graph
``aeic_entropy_decode_fp32_op17.onnx``    58 MiB   decode-side entropy graph
``aeic_cdf_ft32.bin``                    795 KiB   quantised CDF tables
======================================  =========  ==============================

Nothing here is bundled with RemoteTerm and nothing is committed. The files are
downloaded on demand into :attr:`app.config.Settings.aeic_model_dir` and
verified against the SHA-256 digests below.

## Why every digest is mandatory

The CDF tables and the entropy graphs are per-checkpoint and are NOT
interchangeable. Decoding an ft32 bitstream with a table set from another
checkpoint desynchronises rANS and produces a sharp, plausible, WRONG image with
no error raised anywhere. A truncated or mixed-provenance download is therefore
not a recoverable degradation, it is silent corruption -- so
:func:`verify_asset` hard-fails and there is no "skip verification" path.

## Why the weights file's name is load-bearing

``aeic_decoder_qdq_conv_pct.onnx`` references its weights sibling by a *literal
relative filename* baked into the graph. ONNX Runtime resolves it from the
graph's own directory, so the sibling must sit next to it under exactly that
name. Downloading only the 2.9 MiB graph yields an ``ORT_INVALID_PROTOBUF``-class
failure at session creation, indistinguishable from a corrupt download.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)

HUGGINGFACE_REPO = "zjs81/aeic-se-onnx"
_BASE_URL = f"https://huggingface.co/{HUGGINGFACE_REPO}/resolve/main"

DOWNLOAD_CHUNK_BYTES = 1 << 20


class AeicAssetRole(str, Enum):
    """What a bundle file *is*, so nothing has to infer it from a filename.

    Three of the five assets are ``.onnx`` files and no property of a filename
    distinguishes the two entropy graphs. Handing the decode-side export to the
    encoder fails at the first run; handing the send-side one to the decoder
    would feed rANS the wrong probabilities, which is the silent-corruption mode.
    """

    DECODER_GRAPH = "decoder_graph"
    DECODER_WEIGHTS = "decoder_weights"
    ENTROPY_GRAPH = "entropy_graph"
    ENTROPY_DECODE_GRAPH = "entropy_decode_graph"
    CDF_TABLES = "cdf_tables"


@dataclass(frozen=True)
class AeicAsset:
    """One downloadable file belonging to the bundle."""

    role: AeicAssetRole
    file_name: str
    """Name the file must have on disk. NOT derived from the URL: the external
    data reference inside the decoder graph is a literal filename."""

    size_bytes: int
    sha256: str

    @property
    def source_url(self) -> str:
        return f"{_BASE_URL}/{self.file_name}?download=true"


AEIC_SE_FT32_ASSETS: tuple[AeicAsset, ...] = (
    AeicAsset(
        role=AeicAssetRole.DECODER_GRAPH,
        file_name="aeic_decoder_qdq_conv_pct.onnx",
        size_bytes=3066597,
        sha256="fa1ca65c52ecb9e1ec43c05ef792ac8b95ecab21dcb6ab89a825b4dad6a5a571",
    ),
    AeicAsset(
        role=AeicAssetRole.DECODER_WEIGHTS,
        file_name="aeic_decoder_qdq_conv_pct.onnx.data",
        size_bytes=872896480,
        sha256="f7714df0ec8cc495be1fb4bad3be0458c186c8a61d87b8487f2e8e6b84b8242a",
    ),
    AeicAsset(
        role=AeicAssetRole.ENTROPY_GRAPH,
        file_name="aeic_entropy_side_fp32_op17.onnx",
        size_bytes=67262167,
        sha256="b7b55b0f6a8a02ec2e8f6e85820c064c741c870c226d276c78df45e83ca1a9d6",
    ),
    AeicAsset(
        role=AeicAssetRole.ENTROPY_DECODE_GRAPH,
        file_name="aeic_entropy_decode_fp32_op17.onnx",
        size_bytes=60509540,
        sha256="efcbbc4829a0029f487f17b7b52373c6af339d7f74a6417463981d0778d6d444",
    ),
    AeicAsset(
        role=AeicAssetRole.CDF_TABLES,
        file_name="aeic_cdf_ft32.bin",
        size_bytes=813648,
        sha256="4089fde2af16c340642a5c857be42f6d0f21caf71dd5b4f32d62efcd41c77bd5",
    ),
)

BUNDLE_ID = "aeic-se-ft32-bundle-v1"
RATE_POINT = "ft32"
"""The one rate point this build encodes and decodes at.

ft32 measures mean 156 B, min 110 B, max 209 B over upstream's 26-image corpus.
ft16 was dropped upstream: it needs 2-3 chunks and left too little headroom.
"""

BUNDLE_TOTAL_BYTES = sum(asset.size_bytes for asset in AEIC_SE_FT32_ASSETS)

SEND_HALF_ROLES = (AeicAssetRole.ENTROPY_GRAPH, AeicAssetRole.CDF_TABLES)
"""Exactly what :attr:`AeicBundle.supports_encode` asks for, and nothing else.

Sending is 65 MiB of the 958 and 0.35 GiB of the ~1.4: the send-side entropy
graph and the CDF tables. The 832 MiB of synthesis weights only ever run on
*receipt*, so a host that cannot reconstruct a picture -- for want of disk, or of
the 1.4 GiB -- can still send them all day. Keeping that true is the point of
splitting the download at all.

The two halves are NOT independently versioned: every asset here is
per-checkpoint, and a send half from one checkpoint with a receive half from
another desynchronises rANS silently. They are separable, never mixable.
"""

SEND_HALF_ASSETS: tuple[AeicAsset, ...] = tuple(
    asset for asset in AEIC_SE_FT32_ASSETS if asset.role in SEND_HALF_ROLES
)

SEND_HALF_TOTAL_BYTES = sum(asset.size_bytes for asset in SEND_HALF_ASSETS)
"""958.0 MiB. Named so callers and tests share one number.

NOTE: there is deliberately no pre-flight free-space check yet. On a small SD
card the download can fill the volume, after which SQLite writes start failing --
worse than refusing up front. Tracked separately.
"""


class AeicBundleIncomplete(RuntimeError):
    """The installed bundle is missing a file the requested direction needs."""


class AeicAssetCorrupt(RuntimeError):
    """A file on disk does not match its published digest."""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_asset(path: Path, asset: AeicAsset) -> None:
    """Hard-fail unless ``path`` is exactly the published bytes for ``asset``."""
    actual_size = path.stat().st_size
    if actual_size != asset.size_bytes:
        raise AeicAssetCorrupt(
            f"{asset.file_name} is {actual_size} B on disk, expected "
            f"{asset.size_bytes} B; the download is incomplete"
        )
    actual = file_sha256(path)
    if actual != asset.sha256:
        raise AeicAssetCorrupt(
            f"{asset.file_name} has SHA-256 {actual}, expected {asset.sha256}. "
            "A table or graph set that disagrees with the checkpoint does not "
            "fail loudly at decode time -- it silently produces a wrong image."
        )


@dataclass(frozen=True)
class AeicBundle:
    """The set of on-disk paths a codec session needs."""

    root: Path
    rate_point: str = RATE_POINT

    def path_for(self, asset: AeicAsset) -> Path:
        return self.root / asset.file_name

    def _path_if_present(self, role: AeicAssetRole) -> Path | None:
        asset = asset_for_role(role)
        path = self.path_for(asset)
        return path if path.is_file() else None

    @property
    def decoder_graph_path(self) -> Path | None:
        return self._path_if_present(AeicAssetRole.DECODER_GRAPH)

    @property
    def decoder_weights_path(self) -> Path | None:
        return self._path_if_present(AeicAssetRole.DECODER_WEIGHTS)

    @property
    def entropy_graph_path(self) -> Path | None:
        return self._path_if_present(AeicAssetRole.ENTROPY_GRAPH)

    @property
    def entropy_decode_graph_path(self) -> Path | None:
        return self._path_if_present(AeicAssetRole.ENTROPY_DECODE_GRAPH)

    @property
    def tables_path(self) -> Path | None:
        return self._path_if_present(AeicAssetRole.CDF_TABLES)

    @property
    def supports_encode(self) -> bool:
        """The send half needs the send-side entropy graph and the tables only."""
        return self.entropy_graph_path is not None and self.tables_path is not None

    @property
    def supports_decode(self) -> bool:
        """Decoding additionally needs the decode-side graph and the synthesis pair."""
        return (
            self.supports_encode
            and self.entropy_decode_graph_path is not None
            and self.decoder_graph_path is not None
            and self.decoder_weights_path is not None
        )

    @property
    def is_complete(self) -> bool:
        return self.supports_decode

    def missing_assets(self) -> tuple[AeicAsset, ...]:
        return tuple(asset for asset in AEIC_SE_FT32_ASSETS if not self.path_for(asset).is_file())

    def installed_bytes(self, assets: tuple[AeicAsset, ...] | None = None) -> int:
        """Bytes on disk, over the whole bundle or over one half of it."""
        return sum(
            self.path_for(asset).stat().st_size
            for asset in (assets if assets is not None else AEIC_SE_FT32_ASSETS)
            if self.path_for(asset).is_file()
        )

    def require(self, path: Path | None, what: str) -> Path:
        if path is None:
            raise AeicBundleIncomplete(
                f"the AEIC {what} is not installed in {self.root}. Download the "
                "model bundle from the image-codec settings first."
            )
        return path

    def verify_layout(self) -> None:
        """Check every present file's size against the registry.

        Cheap (a ``stat`` per file) and run at load time. The full digest check
        is :func:`verify_asset`, which the download path runs once per file --
        re-hashing 958 MiB on every send would be absurd.
        """
        for asset in AEIC_SE_FT32_ASSETS:
            path = self.path_for(asset)
            if not path.is_file():
                continue
            actual = path.stat().st_size
            if actual != asset.size_bytes:
                raise AeicAssetCorrupt(
                    f"{asset.file_name} is {actual} B on disk, expected "
                    f"{asset.size_bytes} B; re-download the bundle"
                )


def asset_for_role(role: AeicAssetRole) -> AeicAsset:
    for asset in AEIC_SE_FT32_ASSETS:
        if asset.role is role:
            return asset
    raise KeyError(role)  # pragma: no cover - the enum and the tuple agree


ProgressCallback = Callable[[str, int, int], None]
"""``(file_name, downloaded_bytes, total_bytes)``."""


async def download_bundle(
    root: Path,
    *,
    assets: tuple[AeicAsset, ...] = AEIC_SE_FT32_ASSETS,
    on_progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> AeicBundle:
    """Fetch every missing asset in ``assets`` into ``root``, verifying each.

    ``assets`` defaults to the whole bundle; pass :data:`SEND_HALF_ASSETS` for the
    65 MiB that makes sending work. Anything already installed and intact is
    skipped, so the two calls compose: a send half fetched first is not fetched
    again by a later full download.

    Resumable because the weights file is 832 MiB and a mesh gateway's uplink is
    not always a datacentre's: a partial file is kept as ``<name>.part`` and
    continued with a ``Range`` request. Verified because a silently truncated
    graph is indistinguishable from a corrupt one, and the failure mode is a
    wrong picture rather than an error.
    """
    import httpx

    root.mkdir(parents=True, exist_ok=True)
    bundle = AeicBundle(root=root)

    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        for asset in assets:
            final = bundle.path_for(asset)
            if final.is_file():
                try:
                    await asyncio.to_thread(verify_asset, final, asset)
                except AeicAssetCorrupt as exc:
                    logger.warning("Re-downloading %s: %s", asset.file_name, exc)
                    final.unlink()
                else:
                    if on_progress is not None:
                        on_progress(asset.file_name, asset.size_bytes, asset.size_bytes)
                    continue

            partial = final.with_name(final.name + ".part")
            have = partial.stat().st_size if partial.is_file() else 0
            if have > asset.size_bytes:
                # A .part longer than the published file is not a resume point.
                partial.unlink()
                have = 0

            headers = {"Range": f"bytes={have}-"} if have else {}
            mode = "ab" if have else "wb"
            async with client.stream("GET", asset.source_url, headers=headers) as response:
                if have and response.status_code == 200:
                    # The server ignored the Range header; start over rather than
                    # appending a second copy of the file to the first partial one.
                    logger.info("%s: server ignored Range, restarting", asset.file_name)
                    have, mode = 0, "wb"
                elif have and response.status_code != 206:
                    response.raise_for_status()
                else:
                    response.raise_for_status()
                with partial.open(mode) as handle:
                    async for chunk in response.aiter_bytes(DOWNLOAD_CHUNK_BYTES):
                        if should_cancel is not None and should_cancel():
                            raise AeicDownloadCancelled(asset.file_name)
                        handle.write(chunk)
                        have += len(chunk)
                        if on_progress is not None:
                            on_progress(asset.file_name, have, asset.size_bytes)

            # SHA-256 over 832 MB is seconds of solid CPU. Run on the event loop
            # it froze the radio link and the websocket for the whole hash --
            # long enough on a Pi to look like the app had hung.
            await asyncio.to_thread(verify_asset, partial, asset)
            partial.replace(final)
            logger.info("Installed AEIC asset %s (%d B)", asset.file_name, asset.size_bytes)

    return bundle


class AeicDownloadCancelled(RuntimeError):
    """The bundle download was cancelled; the ``.part`` file is kept for resume."""
