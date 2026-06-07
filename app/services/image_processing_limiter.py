from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings

IMAGE_PROCESSING_ADVISORY_LOCK_BASE = 836402600
_LOCAL_SEMAPHORES: dict[int, asyncio.Semaphore] = {}


def _local_semaphore(limit: int) -> asyncio.Semaphore:
    semaphore = _LOCAL_SEMAPHORES.get(limit)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        _LOCAL_SEMAPHORES[limit] = semaphore
    return semaphore


@asynccontextmanager
async def image_processing_slot(db: AsyncSession):
    limit = settings.max_image_processing_jobs
    if limit <= 0:
        yield
        return

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        semaphore = _local_semaphore(limit)
        async with semaphore:
            yield
        return

    while True:
        for slot in range(limit):
            acquired = await db.scalar(
                text("SELECT pg_try_advisory_xact_lock(:lock_id)"),
                {"lock_id": IMAGE_PROCESSING_ADVISORY_LOCK_BASE + slot},
            )
            if acquired:
                yield
                return
        await asyncio.sleep(0.1)
