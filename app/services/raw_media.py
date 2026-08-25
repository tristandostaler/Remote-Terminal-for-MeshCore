"""The raw-data transport shared by the SAR image and voice formats.

Both formats advertise themselves as a text envelope and then move their
fragments as raw MeshCore packets, through ``CMD_SEND_RAW_DATA``. That send is
one piece of code with two callers, and it used to live in
``app/services/voice.py`` simply because voice landed first -- so a fix to how
images are fetched showed up as a change to the voice module, and a failure while
opening a picture reported itself as a voice error. It has its own home now.

Nothing here knows which format it is carrying, so nothing here may say "voice"
or "image": these messages surface verbatim in the UI.
"""

from __future__ import annotations

import logging

from meshcore import EventType

from app.repository import ContactAdvertPathRepository

logger = logging.getLogger(__name__)

MAX_RAW_MEDIA_PATH_LEN = 0x3F
"""Ceiling of the 6-bit path-length field: the top two bits carry the hash mode."""
MAX_RAW_MEDIA_PATH_BYTES = 64
"""MeshCore's ``MAX_PATH_SIZE``. A wider path hash buys fewer hops inside it."""
RAW_MEDIA_FRAGMENT_DELAY_SECONDS = 0.350
UNSUPPORTED_CMD_ERROR_CODE = 1
"""``ERR_CODE_UNSUPPORTED_CMD`` in the companion protocol's error table."""


class RawDataUnsupportedError(RuntimeError):
    """The node's firmware does not implement ``CMD_SEND_RAW_DATA`` (25).

    A distinct type because this is not a transient radio failure: without that
    command neither the SAR image nor the voice transport can move a single
    fragment in either direction, so retrying can never succeed and the caller
    should say what has to change instead.
    """


def _is_unsupported_cmd(payload: object) -> bool:
    return isinstance(payload, dict) and (
        payload.get("error_code") == UNSUPPORTED_CMD_ERROR_CODE
        or payload.get("code_string") == "ERR_CODE_UNSUPPORTED_CMD"
    )


def _raw_frame_for_contact(
    contact, payload: bytes, *, route: tuple[str, int, int] | None = None
) -> bytes:
    path, path_len, hash_mode = route or contact.effective_route_tuple()
    if path_len < 0:
        raise ValueError("raw media transfer requires a direct or learned route")
    if hash_mode not in (0, 1, 2):
        raise ValueError("contact route has an unsupported path hash mode")
    # Bound by what the packet header can express, nothing tighter. This used to
    # refuse anything past 3 hops, which is not a protocol limit -- it made
    # pictures and recordings unfetchable from any contact further away than
    # that, reported as though the mesh forbade it. Ordinary messages already
    # travel those routes; a fetch request is one small packet on the same path.
    hop_width = hash_mode + 1
    max_hops = min(MAX_RAW_MEDIA_PATH_LEN, MAX_RAW_MEDIA_PATH_BYTES // hop_width)
    if path_len > max_hops:
        raise ValueError(
            f"raw media transfer cannot use a {path_len}-hop route: the packet header "
            f"holds at most {max_hops} hops at this path hash width"
        )
    path_bytes = bytes.fromhex(path)
    if len(path_bytes) != path_len * hop_width:
        raise ValueError("contact route is not valid for raw media")
    packed_path_len = (hash_mode << 6) | path_len
    return bytes([packed_path_len]) + path_bytes + payload


async def _raw_route_for_contact(contact) -> tuple[str, int, int]:
    """Resolve a non-flood raw route, with a direct-advert zero-hop fallback."""
    route = contact.effective_route_tuple()
    if route[1] >= 0:
        return route

    advert_paths = await ContactAdvertPathRepository.get_recent_for_contact(
        contact.public_key, limit=1
    )
    if advert_paths and advert_paths[0].path_len == 0 and not advert_paths[0].path:
        logger.info(
            "Using most recently observed direct advert as zero-hop raw media route for %s",
            contact.public_key[:12],
        )
        return "", 0, 0
    return route


async def send_raw_to_contact(radio_manager, contact, payload: bytes) -> None:
    """Send one raw-data frame to a contact over a non-flood route."""
    route = await _raw_route_for_contact(contact)
    frame = _raw_frame_for_contact(contact, payload, route=route)
    async with radio_manager.radio_operation("raw_media_send", blocking=True) as mc:
        result = await mc.commands.send_raw_data(frame)
    if result is None or result.type == EventType.ERROR:
        detail = result.payload if result is not None else "no radio response"
        if result is not None and _is_unsupported_cmd(result.payload):
            version = getattr(radio_manager, "firmware_version", None)
            raise RawDataUnsupportedError(
                f"This node's firmware{f' ({version})' if version else ''} cannot send "
                "raw data packets, which the standard image and voice formats use to "
                "move fragments. Update the node to a firmware build that supports "
                "CMD_SEND_RAW_DATA."
            )
        raise RuntimeError(f"raw data send failed: {detail}")
