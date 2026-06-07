from __future__ import annotations

from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_
from sqlalchemy.future import select

from app.models.job import Job

ACTIVE_JOB_STATUSES = {"pending", "processing", "running"}


def _active_idempotency_filter(idempotency_key: str):
    return or_(
        Job.idempotency_key == idempotency_key,
        Job.idempotency_key.like(f"{idempotency_key}:rerun:%"),
    )


async def get_or_create_active_job(
    db: AsyncSession,
    *,
    job_type: str,
    payload: dict,
    idempotency_key: str,
    max_attempts: int = 3,
) -> Job:
    result = await db.execute(
        select(Job).where(
            _active_idempotency_filter(idempotency_key),
            Job.status.in_(ACTIVE_JOB_STATUSES),
        )
    )
    existing = result.scalars().first()
    if existing:
        return existing

    def build_job(key: str) -> Job:
        return Job(
            type=job_type,
            status="pending",
            idempotency_key=key,
            payload=payload,
            max_attempts=max_attempts,
        )

    job = build_job(idempotency_key)
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
        return job
    except IntegrityError:
        result = await db.execute(
            select(Job).where(
                _active_idempotency_filter(idempotency_key),
                Job.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        existing = result.scalars().first()
        if existing:
            return existing

    job = build_job(f"{idempotency_key}:rerun:{uuid4().hex}")
    async with db.begin_nested():
        db.add(job)
        await db.flush()
    return job
