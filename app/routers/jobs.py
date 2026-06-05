from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.db import get_db
from app.models.job import Job
from app.schemas.job import JobResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=List[JobResponse])
async def list_jobs(skip: int = 0, limit: int = 50, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).order_by(Job.created_at.desc()).offset(skip).limit(limit))
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
    new_job = Job(
        type="cleanup_storage",
        status="pending",
        payload={"dry_run": dry_run}
    )
    db.add(new_job)
    await db.commit()
    await db.refresh(new_job)
    
    return new_job
