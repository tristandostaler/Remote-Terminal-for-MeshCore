"""Process-wide orchestration for the AEIC image codec.

One instance, held by :data:`aeic_service`. It owns:

* the installed :class:`~app.imaging.aeic.bundle.AeicBundle` and its ORT
  sessions (through :class:`~app.imaging.aeic.onnx_backend.OnnxAeicBackend`),
* the model-bundle download, its progress, and its cancellation,
* the encode and decode entry points the routers and the ingest path call.

## Everything heavy runs off the event loop

The synthesis pass is ~5 s of solid CPU and the encode pass ~0.3 s. RemoteTerm's
event loop is also carrying the radio: a BLE notify stream stalled for five
seconds drops mesh traffic. So every inference call goes through
:func:`asyncio.to_thread`, and a semaphore of one serialises them -- two
concurrent decodes would mean two 2.16 GiB sessions and an OOM kill on a small
gateway.

## Sessions are lazy and evictable

See the memory contract in :mod:`app.imaging.aeic.onnx_backend`. In short:
encoding only ever creates the 64 MiB send-side entropy graph, and a decode
drops the entropy sessions before it creates the 2.16 GiB synthesis one.
:meth:`AeicService.release_idle_sessions` sheds the expensive half and is what a
caller should use after a burst.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from pathlib import Path

from app.config import settings
from app.imaging.aeic.bundle import (
    AEIC_SE_FT32_ASSETS,
    BUNDLE_TOTAL_BYTES,
    RATE_POINT,
    AeicBundle,
    AeicDownloadCancelled,
    download_bundle,
)
from app.imaging.aeic.constants import SQUARE_SIZE, onnxruntime_available
from app.imaging.aeic.prepare import prepare_square_rgb
from app.imaging.aeic.text_transport import (
    AeicStreamMetadata,
    aspect_code_for,
    encode_chunks,
)
from app.imaging.aeic.transport import (
    AeicSendResult,
    AeicTarget,
    AeicTransport,
    select_transport,
)

logger = logging.getLogger(__name__)

RGB_BYTES_EXPECTED = SQUARE_SIZE * SQUARE_SIZE * 3
"""What the frontend must POST: a 512x512 square of packed 8-bit RGB.

The browser does the resize, exactly as it already does for IE4. The codec
encodes a SQUARE with the whole frame stretched to fit -- not a crop, so nothing
outside the frame is lost -- and the source aspect travels in the metadata byte
so the receiver can letterbox back.
"""


class AeicUnavailable(RuntimeError):
    """The codec cannot run, with a sentence fit to show the user."""


class AeicService:
    def __init__(self) -> None:
        self._model_dir = Path(settings.aeic_model_dir)
        self._backend = None
        self._tables = None
        self._inference_lock = asyncio.Semaphore(1)
        self._download_task: asyncio.Task | None = None
        self._download_cancelled = False
        self._download_file: str | None = None
        self._downloaded_bytes = 0
        self._download_total = 0
        self._last_error: str | None = None

    # ---- status -------------------------------------------------------------

    @property
    def model_dir(self) -> Path:
        return self._model_dir

    def bundle(self) -> AeicBundle:
        return AeicBundle(root=self._model_dir, rate_point=RATE_POINT)

    @property
    def is_downloading(self) -> bool:
        return self._download_task is not None and not self._download_task.done()

    def status(self) -> dict:
        bundle = self.bundle()
        runtime = onnxruntime_available()
        return {
            "runtime_available": runtime,
            "supports_encode": runtime and bundle.supports_encode,
            "supports_decode": runtime and bundle.supports_decode,
            "downloading": self.is_downloading,
            "download_file": self._download_file if self.is_downloading else None,
            "downloaded_bytes": self._downloaded_bytes,
            "download_total_bytes": self._download_total,
            "installed_bytes": bundle.installed_bytes(),
            "bundle_total_bytes": BUNDLE_TOTAL_BYTES,
            "model_dir": str(self._model_dir),
            "rate_point": RATE_POINT,
            "last_error": self._last_error,
            "assets": [
                {
                    "file_name": asset.file_name,
                    "role": asset.role.value,
                    "size_bytes": asset.size_bytes,
                    "installed": bundle.path_for(asset).is_file(),
                }
                for asset in AEIC_SE_FT32_ASSETS
            ],
        }

    def unavailable_reason(self, *, for_decode: bool) -> str | None:
        """A sentence to show the user, or None when the codec is usable."""
        if not onnxruntime_available():
            return (
                "The AI image codec needs the optional onnxruntime dependency, "
                "which is not installed on this server."
            )
        bundle = self.bundle()
        ready = bundle.supports_decode if for_decode else bundle.supports_encode
        if not ready:
            missing = len(bundle.missing_assets())
            return (
                f"The AI image codec model is not installed ({missing} of "
                f"{len(AEIC_SE_FT32_ASSETS)} files missing, 958 MiB total). "
                "Download it from the image codec settings."
            )
        return None

    def _require_ready(self, *, for_decode: bool) -> None:
        reason = self.unavailable_reason(for_decode=for_decode)
        if reason is not None:
            raise AeicUnavailable(reason)

    # ---- lazy resources ----------------------------------------------------

    def _get_backend(self):
        from app.imaging.aeic.onnx_backend import OnnxAeicBackend

        bundle = self.bundle()
        if self._backend is None or self._backend.bundle.root != bundle.root:
            bundle.verify_layout()
            self._backend = OnnxAeicBackend(bundle)
            self._tables = None
        return self._backend

    def _get_tables(self):
        from app.imaging.aeic.tables import parse_entropy_tables

        if self._tables is None:
            bundle = self.bundle()
            path = bundle.require(bundle.tables_path, "CDF tables")
            self._tables = parse_entropy_tables(path.read_bytes())
        return self._tables

    def release_idle_sessions(self) -> None:
        """Shed the ~2.16 GiB synthesis session, keeping the send path warm."""
        if self._backend is not None:
            self._backend.handle_memory_pressure()

    def reset(self) -> None:
        """Drop every session and cached table, e.g. after a re-download."""
        if self._backend is not None:
            self._backend.close()
        self._backend = None
        self._tables = None

    # ---- codec -------------------------------------------------------------

    def _codec(self, *, for_decode: bool):
        from app.imaging.aeic.entropy import AeicEntropyCodec, AeicGeometry

        backend = self._get_backend()
        return AeicEntropyCodec(
            AeicGeometry.for_resolution(SQUARE_SIZE),
            backend.entropy_network(for_decode=for_decode),
            self._get_tables(),
        )

    async def encode_rgb(self, rgb: bytes) -> bytes:
        """512x512 packed RGB -> the rANS bitstream that goes on the air."""
        self._require_ready(for_decode=False)
        if len(rgb) != RGB_BYTES_EXPECTED:
            raise AeicUnavailable(
                f"expected {RGB_BYTES_EXPECTED} bytes of {SQUARE_SIZE}x{SQUARE_SIZE} "
                f"RGB, got {len(rgb)}"
            )

        def run() -> bytes:
            return self._codec(for_decode=False).encode(rgb)

        async with self._inference_lock:
            stream = await asyncio.to_thread(run)
        logger.info("AEIC encoded a %dpx image to %d bytes", SQUARE_SIZE, len(stream))
        return stream

    async def decode_to_png(self, bitstream: bytes) -> bytes:
        """A received bitstream -> a PNG of the reconstructed picture.

        The two halves are separated by an explicit session release: holding the
        entropy graph and the synthesis decoder at once measures 2.44 GiB.
        """
        self._require_ready(for_decode=True)

        def run() -> bytes:
            from app.imaging.aeic.png import encode_png

            backend = self._get_backend()
            try:
                y_hat = self._codec(for_decode=True).decode_to_latent(bitstream)
                # Mandatory, not tidy-up: see the memory contract in onnx_backend.
                backend.release_entropy_sessions()
                rgb = backend.decode_latent_to_rgb(y_hat)
            finally:
                # Both releases repeat in the finally because a raise here is
                # SWALLOWED by the caller (ingest.decode_session logs and moves
                # on), so anything still held stays held for the life of the
                # process -- and the next decode would then allocate the entropy
                # graph on top of the 2.16 GiB synthesis session, which is
                # exactly the 2.44 GiB the memory contract exists to prevent.
                # Both calls are idempotent, so repeating the success-path
                # release above costs nothing.
                backend.release_entropy_sessions()
                backend.release_decoder_session()
            return encode_png(rgb, SQUARE_SIZE, SQUARE_SIZE)

        async with self._inference_lock:
            png = await asyncio.to_thread(run)
        logger.info("AEIC decoded %d bytes into a %dpx image", len(bitstream), SQUARE_SIZE)
        return png

    async def send_image(
        self,
        data: bytes,
        target: AeicTarget,
        *,
        source_width: int | None = None,
        source_height: int | None = None,
        session_id: int | None = None,
        transport: AeicTransport | None = None,
    ) -> tuple[AeicSendResult, bytes, AeicStreamMetadata]:
        """Encode one image and put it on air. The single send entry point.

        ``data`` is either a 512x512 packed-RGB square (the browser path) or any
        image Pillow can open (the bot path); see
        :func:`app.imaging.aeic.prepare.prepare_square_rgb`.

        ``source_width``/``source_height`` override the shape recorded in the
        metadata byte. Pass them when the caller knows the original dimensions
        but is supplying already-squared pixels -- which is exactly the browser
        case, since raw pixels carry no aspect of their own.

        The transport comes from :func:`select_transport` unless one is passed,
        so this method does not change when the binary 0xAE1C transport lands.
        """
        rgb, detected_width, detected_height = prepare_square_rgb(data)
        width = source_width if source_width is not None else detected_width
        height = source_height if source_height is not None else detected_height

        bitstream = await self.encode_rgb(rgb)
        metadata = AeicStreamMetadata(
            square_size=SQUARE_SIZE,
            aspect_code=aspect_code_for(width, height),
        )
        chosen = transport or select_transport()
        result = await chosen.send(bitstream, metadata, target, session_id=session_id)
        storage_key = await self._record_outgoing(result, metadata, target, bitstream)
        return replace(result, storage_key=storage_key), bitstream, metadata

    async def _record_outgoing(
        self,
        result: AeicSendResult,
        metadata: AeicStreamMetadata,
        target: AeicTarget,
        bitstream: bytes,
    ) -> str | None:
        """Store the sent image as a session so the UI renders it as a picture.

        Without this the sender's own outgoing message shows as raw ``aei1:``
        text in their conversation while the recipient sees the photo. Done here
        rather than in each caller so the route and the bot path cannot drift.

        Returns the row key, or None if recording failed.
        """
        from app.repository import AeicImageRepository
        from app.repository.aeic_image import outgoing_session_key

        # The message id has to be resolved BEFORE the key, because the key is
        # derived from it -- see outgoing_session_key for why it is not the wire
        # session id. Written straight into the row rather than through a later
        # set_message_id, so there is no COALESCE that could pin a stale id.
        first = next((m for m in result.emitted if m is not None and getattr(m, "id", None)), None)
        message_id = getattr(first, "id", None) if first is not None else None
        key = outgoing_session_key(message_id)
        try:
            await AeicImageRepository.enforce_cache_limit()
            await AeicImageRepository.create_session(
                key=key,
                message_id=message_id,
                direction="outgoing",
                conversation_type=target.conversation_type,
                conversation_key=target.conversation_key,
                peer_public_key=(
                    target.conversation_key if target.conversation_type == "PRIV" else None
                ),
                square_size=metadata.square_size,
                aspect_code=metadata.aspect_code,
                rate_code=metadata.rate_code,
                total_chunks=result.chunk_count,
                state="complete",
            )
            await AeicImageRepository.store_bitstream(key, bitstream)
        except Exception:
            # The image is already on air; failing to record it locally must not
            # turn a delivered photo into a raised error for the caller.
            logger.exception("Could not record the outgoing AEIC session %s", key)
            return None
        return key

    async def frame_for_send(
        self,
        rgb: bytes,
        *,
        source_width: int,
        source_height: int,
        message_budget: int,
        session_id: int | None = None,
    ) -> tuple[list[str], bytes, AeicStreamMetadata]:
        """Encode and frame one photo: ``(messages, bitstream, metadata)``.

        Text-transport specific, kept for callers that need the framed chunks
        themselves rather than having them sent. Prefer :meth:`send_image`.
        """
        bitstream = await self.encode_rgb(rgb)
        metadata = AeicStreamMetadata(
            square_size=SQUARE_SIZE,
            aspect_code=aspect_code_for(source_width, source_height),
        )
        chunks = encode_chunks(
            bitstream,
            metadata,
            session_id=session_id,
            message_budget=message_budget,
        )
        return chunks, bitstream, metadata

    # ---- model download ----------------------------------------------------

    def start_download(self) -> bool:
        """Kick off the bundle download. False if one is already running."""
        if self.is_downloading:
            return False
        self._download_cancelled = False
        self._last_error = None
        self._downloaded_bytes = 0
        self._download_total = 0
        self._download_task = asyncio.create_task(self._run_download())
        return True

    def cancel_download(self) -> bool:
        """Ask the download to stop. Partial files are kept for resume."""
        if not self.is_downloading:
            return False
        self._download_cancelled = True
        return True

    async def _run_download(self) -> None:
        from app.websocket import broadcast_event

        def on_progress(file_name: str, done: int, total: int) -> None:
            self._download_file = file_name
            self._downloaded_bytes = done
            self._download_total = total

        async def announce() -> None:
            # Progress is polled into one event per second rather than per 1 MiB
            # chunk: an 832 MiB file would otherwise emit ~832 websocket frames.
            while self.is_downloading:
                broadcast_event("aeic_model_download", self.status())
                await asyncio.sleep(1.0)

        announcer = asyncio.create_task(announce())
        try:
            await download_bundle(
                self._model_dir,
                on_progress=on_progress,
                should_cancel=lambda: self._download_cancelled,
            )
            self.reset()
            logger.info("AEIC model bundle installed in %s", self._model_dir)
        except AeicDownloadCancelled:
            self._last_error = "Download cancelled. Partial files were kept for resume."
            logger.info("AEIC model download cancelled")
        except Exception as exc:  # noqa: BLE001 - surfaced to the user verbatim
            self._last_error = f"{type(exc).__name__}: {exc}"
            logger.exception("AEIC model download failed")
        finally:
            self._download_file = None
            announcer.cancel()
            broadcast_event("aeic_model_download", self.status())


aeic_service = AeicService()
