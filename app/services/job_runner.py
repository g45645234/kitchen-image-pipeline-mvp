import asyncio
import logging
from datetime import datetime, timezone
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.db import async_session_maker
from app.models.job import Job
from app.services.search_service import execute_search_for_mistake
from app.services.review_service import process_candidate_review
from app.services.export_service import export_video_manifest
from app.services.storage_service import cleanup_storage

logger = logging.getLogger(__name__)

async def process_single_job(job: Job, db: AsyncSession):
    """
    Выполняет одну задачу на основе её типа.
    """
    job_type = job.type
    payload = job.payload
    
    if job_type == "search_all_queries":
        await execute_search_for_mistake(payload.get("mistake_id"), db)
        
    elif job_type == "review_candidate":
        await process_candidate_review(
            candidate_id=payload.get("candidate_id"),
            action=payload.get("action"),
            reason=payload.get("reason"),
            db=db
        )
        
    elif job_type == "export_final_assets":
        manifest_path = await export_video_manifest(payload.get("video_id"), db)
        job.result = {"manifest_path": manifest_path}
        
    elif job_type == "cleanup_storage":
        result = await cleanup_storage(payload.get("dry_run", True), db)
        job.result = result
        
    else:
        raise ValueError(f"Unknown job type: {job_type}")

async def fetch_and_run_jobs():
    """
    Один цикл воркера: ищет 1 pending job, лочит его, выполняет, записывает результат.
    """
    async with async_session_maker() as db:
        # Ищем 1 задачу (для MVP достаточно)
        result = await db.execute(
            select(Job)
            .where(Job.status == "pending")
            .order_by(Job.created_at.asc())
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        job = result.scalars().first()
        
        if not job:
            return # Нет задач
            
        logger.info(f"Picked up job {job.id} of type {job.type}")
        
        # Захватываем лок
        job.status = "processing"
        job.started_at = datetime.now(timezone.utc)
        job.locked_by = "local_worker_1"
        job.locked_at = datetime.now(timezone.utc)
        job.attempts += 1
        await db.commit()
        
        # Выполняем
        try:
            await process_single_job(job, db)
            job.status = "completed"
        except Exception as e:
            logger.error(f"Job {job.id} failed: {e}")
            job.error_message = str(e)
            if job.attempts >= job.max_attempts:
                job.status = "failed"
            else:
                job.status = "pending" # Вернем в очередь (или retrying)
                job.locked_by = None
                
        job.finished_at = datetime.now(timezone.utc)
        await db.commit()

async def background_worker():
    """
    Бесконечный цикл, который мы запустим в lifespan приложения.
    """
    logger.info("Background job runner started")
    while True:
        try:
            await fetch_and_run_jobs()
        except Exception as e:
            logger.error(f"Error in background_worker loop: {e}")
        await asyncio.sleep(5) # Пауза между проверками очереди
