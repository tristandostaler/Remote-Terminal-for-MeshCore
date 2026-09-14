"""Live feed comparison: this node's channel reception vs live.meshcore.ca (CoreScope)."""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.models import (
    LiveCompareMessagesResponse,
    LiveCompareStats,
    LiveFeedRegionsResponse,
    LiveFeedStatus,
)
from app.services import live_feed
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
    """Run one sync now (even while disabled) and report the outcome."""
    await live_feed.sync_once(force=True)
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
