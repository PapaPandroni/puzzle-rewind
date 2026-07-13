from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.models import Job
from app.rate_limit import limiter
from app.schemas import JobStatus

router = APIRouter()


@router.get("/api/jobs/{job_id}", response_model=JobStatus)
@limiter.limit("60/minute")  # the banner polls every ~3s (20/min); headroom for a second tab
async def get_job(
    request: Request,
    response: Response,
    job_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
):
    job = await db.get(Job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job_not_found")
    status = JobStatus.model_validate(job)
    # Attach the tripped budget's limit so the banner can name the real
    # (env-tunable) number instead of the misleading per-job progress/total.
    if job.error == "daily_budget_reached":
        status.daily_limit = settings.max_engine_games_per_day
    elif job.error == "player_budget_reached":
        status.daily_limit = settings.max_engine_games_per_day_per_player
    return status
