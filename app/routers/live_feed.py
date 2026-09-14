"""Live feed comparison: this node's channel reception vs live.meshcore.ca (CoreScope)."""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.models import (
    LiveCompareMessagesResponse,
    LiveCompareStats,
    LiveCompareTrace,
    LiveFeedRegionsResponse,
    LiveFeedStatus,
)
from app.services import live_feed, live_feed_trace
from app.stats_windows import DEFAULT_STATS_WINDOW, STATS_WINDOWS, is_valid_window

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/live-feed", tags=["live-feed"])

WINDOW_DESCRIPTION = "Time window. One of: " + ", ".join(STATS_WINDOWS) + "."


def _check_window(window: str) -> None:
    if not is_valid_window(window):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown window '{window}'. Expected one of: {', '.join(STATS_WINDOWS)}",
        )


@router.get("/status", response_model=LiveFeedStatus)
async def get_live_feed_status() -> LiveFeedStatus:
    """Configuration in effect plus the sync loop's last outcome."""
    return LiveFeedStatus(**await live_feed.get_status())


@router.post("/sync", response_model=LiveFeedStatus)
async def sync_live_feed() -> LiveFeedStatus:
    """Start a sync now (even while disabled).

    Waits a bounded time for it; a long first walk answers with ``syncing``
    still true and the outcome shows up in ``GET /live-feed/status``.
    """
    await live_feed.request_sync()
    return LiveFeedStatus(**await live_feed.get_status())


@router.get("/regions", response_model=LiveFeedRegionsResponse)
async def get_live_feed_regions() -> LiveFeedRegionsResponse:
    """Regions the configured CoreScope instance knows (observer IATA codes)."""
    try:
        return LiveFeedRegionsResponse(**await live_feed.get_regions())
    except live_feed.LiveFeedError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("/stats", response_model=LiveCompareStats | None)
async def get_live_compare_stats(
    window: str = Query(DEFAULT_STATS_WINDOW, description=WINDOW_DESCRIPTION),
) -> LiveCompareStats | None:
    """Both / node-only / live-only counts for the window; null when nothing is mirrored."""
    _check_window(window)
    stats = await live_feed.get_compare_stats(window)
    return LiveCompareStats(**stats) if stats else None


@router.get("/messages", response_model=LiveCompareMessagesResponse)
async def get_live_compare_messages(
    window: str = Query(DEFAULT_STATS_WINDOW, description=WINDOW_DESCRIPTION),
    channel_key: str | None = Query(None, description="Restrict to one local channel key"),
    source: str | None = Query(
        None,
        pattern="^(both|node|live)$",
        description="Restrict to messages seen by both, only this node, or only the live feed",
    ),
    q: str | None = Query(None, max_length=200, description="Substring filter on the text"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> LiveCompareMessagesResponse:
    """Node and live-feed channel messages merged without duplicates, newest first."""
    _check_window(window)
    return LiveCompareMessagesResponse(
        **await live_feed.list_messages(
            window,
            channel_key=channel_key or None,
            source=source,
            q=(q or "").strip() or None,
            limit=limit,
            offset=offset,
        )
    )


@router.get("/trace", response_model=LiveCompareTrace)
async def get_live_compare_trace(
    packet_hash: str | None = Query(
        None, max_length=80, description="Remote packet hash of the row (from the merged list)"
    ),
    message_id: int | None = Query(None, ge=1, description="Local message id of the row"),
) -> LiveCompareTrace:
    """How one message travelled: every observer's reception and this node's, hops resolved.

    Pass whichever identifiers the merged row carries; a live-only row has just
    the hash, a node-only row just the id. Observations come from the instance
    at request time (``live_error`` says when they could not), the node side
    from the local database.
    """
    if not packet_hash and message_id is None:
        raise HTTPException(status_code=422, detail="packet_hash or message_id is required")
    trace = await live_feed_trace.get_trace(packet_hash=packet_hash or None, message_id=message_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Unknown message")
    return LiveCompareTrace(**trace)
