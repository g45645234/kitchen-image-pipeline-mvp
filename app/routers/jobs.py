from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.auth import require_admin_api_token
from app.db import get_db
from app.models.job import Job
from app.services.job_service import get_or_create_active_job
from app.schemas.job import JobResponse

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=[Depends(require_admin_api_token)])


def _job_status_filter_values(status_filter: str) -> list[str]:
    aliases = {
        "running": ["running", "processing"],
        "succeeded": ["succeeded", "completed"],
        "done": ["done", "completed", "succeeded"],
    }
    return aliases.get(status_filter, [status_filter])


@router.get("", response_model=List[JobResponse])
async def list_jobs(
    skip: int = 0,
    offset: int | None = None,
    limit: int = Query(50, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
    db: AsyncSession = Depends(get_db),
):
    page_offset = max(offset if offset is not None else skip, 0)
    query = select(Job)
    if status_filter:
        query = query.where(Job.status.in_(_job_status_filter_values(status_filter)))
    result = await db.execute(query.order_by(Job.created_at.desc(), Job.id.desc()).offset(page_offset).limit(limit))
    jobs = result.scalars().all()
    return jobs


@router.get("/{job_id}", response_model=JobResponse)
async def get_job_status(job_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalars().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("/cleanup", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def cleanup_storage(dry_run: bool = True, db: AsyncSession = Depends(get_db)):
    """
    Запускает сборщик мусора по хранилищу.
    """
    new_job = await get_or_create_active_job(
        db,
        job_type="cleanup_storage",
        payload={"dry_run": dry_run},
        idempotency_key=f"cleanup_storage:dry_run:{str(dry_run).lower()}",
    )
    await db.commit()
    await db.refresh(new_job)

    return new_job
