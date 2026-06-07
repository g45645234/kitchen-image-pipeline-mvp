from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import require_admin_api_token
from app.db import get_db
from app.schemas.job import JobResponse
from app.services.job_service import get_or_create_active_job

router = APIRouter(prefix="/storage", tags=["storage"], dependencies=[Depends(require_admin_api_token)])


async def _enqueue_cleanup_storage(dry_run: bool, db: AsyncSession):
    job = await get_or_create_active_job(
        db,
        job_type="cleanup_storage",
        payload={"dry_run": dry_run},
        idempotency_key=f"cleanup_storage:dry_run:{str(dry_run).lower()}",
    )
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/cleanup-dry-run", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def cleanup_storage_dry_run(db: AsyncSession = Depends(get_db)):
    return await _enqueue_cleanup_storage(True, db)


@router.post("/cleanup", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def cleanup_storage(dry_run: bool = False, db: AsyncSession = Depends(get_db)):
    return await _enqueue_cleanup_storage(dry_run, db)
