import time

from fastapi import APIRouter, HTTPException, Query

from app.models import StatisticsResponse
from app.repository import StatisticsRepository
from app.services.radio_stats import get_noise_floor_history
from app.stats_windows import DEFAULT_STATS_WINDOW, STATS_WINDOWS, is_valid_window, window_cutoff

router = APIRouter(prefix="/statistics", tags=["statistics"])


@router.get("", response_model=StatisticsResponse)
async def get_statistics(
    window: str = Query(
        DEFAULT_STATS_WINDOW,
        description=(
            "Time window for every bounded metric. One of: " + ", ".join(STATS_WINDOWS) + "."
        ),
    ),
) -> StatisticsResponse:
    if not is_valid_window(window):
        raise HTTPException(
            status_code=422,
            detail=f"Unknown window '{window}'. Expected one of: {', '.join(STATS_WINDOWS)}",
        )

    data = await StatisticsRepository.get_all(window)
    data["noise_floor"] = await get_noise_floor_history(window_cutoff(window, int(time.time())))
    return StatisticsResponse(**data)
