import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import func, text
from sqlalchemy.future import select

from app.config import settings
from app.db import async_session_maker
from app.models.audit import AuditEvent
from app.models.job import Job
from app.models.candidate import CandidateReview, ImageCandidate
from app.services.candidate_scoring_service import score_candidate
from app.services.search_service import execute_search_for_mistake, execute_search_query, generate_search_queries_for_mistake
from app.services.mistake_extraction_service import extract_mistakes_for_video
from app.services.review_service import process_candidate_review
from app.services.candidate_review_runner import run_candidate_reviewer
from app.services.export_service import export_video_manifest
from app.services.final_asset_service import process_final_asset_files
from app.services.storage_service import cleanup_storage, cleanup_storage_targets, download_candidate_image
from app.services.reference_brief_service import create_or_update_reference_brief, mark_reference_brief_failed

logger = logging.getLogger(__name__)

JOB_CLAIM_ADVISORY_LOCK_ID = 836402517
IMAGE_PROCESSING_JOB_TYPES = {"download_candidate", "process_final_asset", "export_video", "export_final_assets", "review_candidate"}


async def acquire_job_claim_lock(db: AsyncSession) -> None:
    bind = db.get_bind()
    if bind.dialect.name == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": JOB_CLAIM_ADVISORY_LOCK_ID},
        )


def allowed_worker_job_types() -> list[str] | None:
    if not settings.worker_job_types:
        return None
    job_types = [item.strip() for item in settings.worker_job_types.split(",") if item.strip()]
    return job_types or None

async def process_single_job(job: Job, db: AsyncSession):
    """
    Выполняет одну задачу на основе её типа.
    """
    job_type = job.type
    payload = job.payload
    
    if job_type == "extract_mistakes":
        return await extract_mistakes_for_video(payload.get("video_id"), db)

    if job_type == "search_all_queries":
        providers = payload.get("providers") or [None]
        provider_results = []
        for provider in providers:
            provider_results.append(
                await execute_search_for_mistake(
                    payload.get("mistake_id"),
                    db,
                    sides=payload.get("sides"),
                    provider=provider,
                    limit_per_query=payload.get("limit_per_query"),
                )
            )
        failed_results = [result for result in provider_results if result.get("status") == "failed"]
        partial_results = [result for result in provider_results if result.get("status") == "partially_failed"]
        if failed_results and len(failed_results) == len(provider_results):
            raise ValueError("All search providers failed")
        if failed_results or partial_results:
            return {"status": "partially_failed", "providers": provider_results}
        return {"status": "completed", "providers": provider_results}

    elif job_type == "create_search_queries":
        providers = payload.get("providers") or [None]
        queries = []
        for provider in providers:
            queries.extend(
                await generate_search_queries_for_mistake(
                    payload.get("mistake_id"),
                    db,
                    sides=payload.get("sides"),
                    provider=provider,
                    limit_per_query=payload.get("limit_per_query", 20),
                )
            )
        return {
            "mistake_id": payload.get("mistake_id"),
            "query_ids": [query.id for query in queries],
            "query_count": len(queries),
        }

    elif job_type == "run_search":
        result = await execute_search_query(
            payload.get("query_id"),
            db,
            limit_per_query=payload.get("limit_per_query"),
        )
        if result.get("status") == "failed":
            raise ValueError("Search provider failed")
        return result

    elif job_type == "score_candidates":
        query = select(ImageCandidate)
        if payload.get("candidate_id") is not None:
            query = query.where(ImageCandidate.id == payload.get("candidate_id"))
        if payload.get("mistake_id") is not None:
            query = query.where(ImageCandidate.mistake_id == payload.get("mistake_id"))
        if payload.get("side") is not None:
            query = query.where(ImageCandidate.side == payload.get("side"))
        result = await db.execute(query.order_by(ImageCandidate.id).with_for_update())
        candidates = result.scalars().all()
        updated_ids = []
        for candidate in candidates:
            review_count = await db.scalar(
                select(func.count()).select_from(CandidateReview).where(CandidateReview.candidate_id == candidate.id)
            )
            existing_review_score = candidate.review_score
            score_candidate(candidate)
            if review_count:
                candidate.review_score = existing_review_score
            updated_ids.append(candidate.id)
        await db.flush()
        return {"candidate_ids": updated_ids, "candidate_count": len(updated_ids)}

    elif job_type == "review_candidate":
        await process_candidate_review(
            candidate_id=payload.get("candidate_id"),
            action=payload.get("action"),
            reason=payload.get("reject_reason") or payload.get("reason"),
            comment=payload.get("comment"),
            db=db
        )
        return None

    elif job_type in {"export_video", "export_final_assets"}:
        manifest_path = Path(await export_video_manifest(payload.get("video_id"), db))
        return {
            "export_dir": str(manifest_path.parent),
            "manifest_path": str(manifest_path),
            "assets_csv_path": str(manifest_path.parent / "assets.csv"),
        }

    elif job_type == "download_candidate":
        candidate_id = payload.get("candidate_id")
        result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
        candidate = result.scalars().first()
        if not candidate:
            raise ValueError(f"Candidate {candidate_id} not found")
        storage_key = await download_candidate_image(candidate.id, candidate.image_url, db)
        return {"candidate_id": candidate.id, "storage_key_original": storage_key}

    elif job_type == "process_final_asset":
        asset = await process_final_asset_files(payload.get("asset_id"), db, original_filename=payload.get("original_filename"))
        return {
            "asset_id": asset.id,
            "storage_key_original": asset.storage_key_original,
            "storage_key_thumbnail": asset.storage_key_thumbnail,
            "storage_key_processed": asset.storage_key_processed,
            "metadata_storage_key": asset.metadata_storage_key,
        }

    elif job_type == "run_candidate_reviewer":
        return await run_candidate_reviewer(
            candidate_id=payload.get("candidate_id"),
            reviewer_name=payload.get("reviewer_name"),
            prompt_version=payload.get("prompt_version"),
            force=payload.get("force", False),
            db=db,
        )

    elif job_type == "create_reference_brief":
        candidate_id = payload.get("candidate_id")
        prompt_version = payload.get("prompt_version") or "mock-v1"
        try:
            brief = await create_or_update_reference_brief(
                candidate_id=candidate_id,
                prompt_version=prompt_version,
                db=db,
            )
        except Exception as e:
            try:
                await mark_reference_brief_failed(
                    candidate_id=candidate_id,
                    prompt_version=prompt_version,
                    error_message=str(e),
                    db=db,
                )
            except Exception as mark_error:
                e.add_note(f"Failed to persist reference brief failure state: {mark_error}")
            raise
        return {"reference_brief_id": brief.id, "candidate_id": brief.candidate_id}

    elif job_type == "cleanup_storage":
        if payload.get("mode") == "targeted" or "old_storage_keys" in payload or "targets" in payload:
            return await cleanup_storage_targets(payload, db)
        return await cleanup_storage(payload.get("dry_run", True), db)

    else:
        raise ValueError(f"Unknown job type: {job_type}")


async def running_jobs_count(db: AsyncSession) -> int:
    return await db.scalar(
        select(func.count()).select_from(Job).where(Job.status.in_(["processing", "running"]))
    ) or 0


async def can_claim_job(db: AsyncSession) -> bool:
    max_running = settings.max_running_jobs
    if max_running <= 0:
        return True
    return await running_jobs_count(db) < max_running


async def running_image_processing_jobs_count(db: AsyncSession) -> int:
    return await db.scalar(
        select(func.count())
        .select_from(Job)
        .where(Job.status.in_(["processing", "running"]))
        .where(Job.type.in_(IMAGE_PROCESSING_JOB_TYPES))
    ) or 0


async def image_processing_job_limit_reached(db: AsyncSession) -> bool:
    max_image_processing = settings.max_image_processing_jobs
    if max_image_processing <= 0:
        return False
    return await running_image_processing_jobs_count(db) >= max_image_processing


async def requeue_stale_processing_jobs(db: AsyncSession, *, commit: bool = True) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=settings.job_lock_timeout_minutes)
    result = await db.execute(
        select(Job)
        .where(Job.status == "processing")
        .where(Job.locked_at.is_not(None))
        .where(Job.locked_at < cutoff)
        .order_by(Job.locked_at.asc())
        .with_for_update(skip_locked=True)
    )
    jobs = result.scalars().all()
    for job in jobs:
        job.status = "pending" if job.attempts < job.max_attempts else "failed"
        job.error_message = "Requeued stale processing job" if job.status == "pending" else "Stale processing job exceeded max attempts"
        job.locked_by = None
        job.locked_at = None
        job.finished_at = datetime.now(timezone.utc) if job.status == "failed" else None
    if jobs and commit:
        await db.commit()
    return len(jobs)

async def fetch_and_run_one_job(db: AsyncSession):
    await acquire_job_claim_lock(db)
    await requeue_stale_processing_jobs(db, commit=False)
    if not await can_claim_job(db):
        logger.info("Max running jobs reached; not claiming a new job")
        await db.commit()
        return None
    query = select(Job).where(Job.status == "pending")
    allowed_job_types = allowed_worker_job_types()
    if allowed_job_types:
        query = query.where(Job.type.in_(allowed_job_types))
    if await image_processing_job_limit_reached(db):
        logger.info("Max image processing jobs reached; skipping image-heavy jobs")
        query = query.where(Job.type.not_in(IMAGE_PROCESSING_JOB_TYPES))
    result = await db.execute(
        query
        .order_by(Job.created_at.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    job = result.scalars().first()

    if not job:
        await db.commit()
        return None

    logger.info(f"Picked up job {job.id} of type {job.type}")

    job.status = "processing"
    job.started_at = datetime.now(timezone.utc)
    job.locked_by = "local_worker_1"
    job.locked_at = datetime.now(timezone.utc)
    job.attempts += 1
    await db.commit()

    job_id = job.id
    error_msg = None
    job_result = None
    success = False
    try:
        job_result = await process_single_job(job, db)
        success = True
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        error_msg = str(e)[:2000]

    await db.rollback()

    result = await db.execute(select(Job).where(Job.id == job_id))
    job = result.scalars().first()
    if job:
        job.finished_at = datetime.now(timezone.utc)
        if success:
            if isinstance(job_result, dict) and job_result.get("status") == "partially_failed":
                job.status = "partially_failed"
                job.error_message = "One or more providers failed"
            else:
                job.status = "completed"
                job.error_message = None
            job.result = job_result
        else:
            job.error_message = error_msg
            if job.attempts >= job.max_attempts:
                job.status = "failed"
                if job.type == "download_candidate" and job.payload.get("candidate_id") is not None:
                    failed_candidate = await db.get(ImageCandidate, job.payload.get("candidate_id"))
                    if failed_candidate:
                        failed_candidate.storage_status = "failed"
                        failed_candidate.status = "failed_download"
            else:
                job.status = "pending"
            db.add(
                AuditEvent(
                    actor="system",
                    entity_type="job",
                    entity_id=job.id,
                    action="job.failed",
                    before={"type": job.type, "payload": job.payload, "attempts": job.attempts, "max_attempts": job.max_attempts},
                    after={"status": job.status, "error_message": error_msg},
                    comment=f"Job {job.id} failed while processing",
                )
            )
        job.locked_by = None
        job.locked_at = None
        await db.commit()
    return job


async def fetch_and_run_jobs():
    """
    Один цикл воркера: ищет 1 pending job, лочит его, выполняет, записывает результат.
    """
    async with async_session_maker() as db:
        return await fetch_and_run_one_job(db)

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
