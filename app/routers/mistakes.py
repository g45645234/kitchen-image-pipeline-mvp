from datetime import datetime, timezone
import hashlib
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.auth import require_admin_api_token
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.candidate import ImageCandidate, SearchQuery
from app.models.job import Job
from app.models.mistake import Mistake
from app.models.feedback import MistakeSideFeedback
from app.models.video import Video
from app.services.job_service import get_or_create_active_job
from app.schemas.mistake import (
    MistakeCreate,
    MistakeCreateForVideo,
    MistakeResponse,
    MistakeSideFeedbackResponse,
    MistakeSideFeedbackUpdate,
    MistakeUpdate,
)
from app.schemas.candidate import SearchQueryGenerateRequest, SearchQueryManualCreateRequest, SearchQueryResponse, SearchQueryUpdateRequest
from app.schemas.job import JobResponse
from app.services.search_service import SUPPORTED_SEARCH_PROVIDERS, generate_search_queries_for_mistake, normalize_search_provider

router = APIRouter(prefix="", tags=["mistakes"], dependencies=[Depends(require_admin_api_token)])


def _storage_keys_for_cleanup(record) -> dict:
    return {
        "storage_key_original": getattr(record, "storage_key_original", None),
        "storage_key_thumbnail": getattr(record, "storage_key_thumbnail", None),
        "storage_key_processed": getattr(record, "storage_key_processed", None),
        "metadata_storage_key": getattr(record, "metadata_storage_key", None),
    }


def _clear_storage_keys(record) -> None:
    for field in ["storage_key_original", "storage_key_thumbnail", "storage_key_processed", "metadata_storage_key"]:
        if hasattr(record, field):
            setattr(record, field, None)


async def _create_mistake_for_video_id(video_id: int, data: dict, db: AsyncSession, actor: str = "admin-ui") -> Mistake:
    result = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)))
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Video not found")

    mistake = Mistake(video_id=video_id, **data)
    db.add(mistake)
    try:
        await db.flush()
        db.add(
            AuditEvent(
                actor=actor,
                entity_type="mistake",
                entity_id=mistake.id,
                action="mistake.created",
                before=None,
                after={"video_id": video_id, **data},
                comment="Mistake created manually",
            )
        )
        await db.commit()
        await db.refresh(mistake)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Ошибка сохранения. Возможно, order_index уже занят.")
    return mistake


@router.post("/mistakes", response_model=MistakeResponse, status_code=status.HTTP_201_CREATED)
async def create_mistake(mistake_in: MistakeCreate, actor: str = "admin-ui", _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    data = mistake_in.model_dump()
    video_id = data.pop("video_id")
    return await _create_mistake_for_video_id(video_id, data, db, actor=actor)


@router.post("/videos/{video_id}/mistakes", response_model=MistakeResponse, status_code=status.HTTP_201_CREATED)
async def create_mistake_for_video(
    video_id: int,
    mistake_in: MistakeCreateForVideo,
    actor: str = "admin-ui",
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    return await _create_mistake_for_video_id(video_id, mistake_in.model_dump(), db, actor=actor)


@router.put(
    "/mistakes/{mistake_id}/side-feedback/{side}",
    response_model=MistakeSideFeedbackResponse,
)
async def upsert_mistake_side_feedback(
    mistake_id: int,
    side: str,
    feedback_in: MistakeSideFeedbackUpdate,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    if side not in {"wrong", "right"}:
        raise HTTPException(status_code=422, detail="side must be wrong or right")

    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None)))
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")

    result = await db.execute(
        select(MistakeSideFeedback).where(
            MistakeSideFeedback.mistake_id == mistake_id,
            MistakeSideFeedback.side == side,
        )
    )
    feedback = result.scalars().first()
    previous_text = feedback.feedback_text if feedback else None
    if not feedback:
        feedback = MistakeSideFeedback(mistake_id=mistake_id, side=side)
        db.add(feedback)

    feedback.feedback_text = feedback_in.feedback_text.strip()
    feedback.actor = feedback_in.actor or "admin-ui"
    await db.flush()

    db.add(
        AuditEvent(
            actor=feedback.actor,
            entity_type="mistake_side_feedback",
            entity_id=feedback.id,
            action="mistake_side_feedback.updated",
            before={"feedback_text": previous_text},
            after={
                "mistake_id": mistake_id,
                "side": side,
                "feedback_text": feedback.feedback_text,
            },
            comment="Search/AI-review feedback updated",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    await db.refresh(feedback)
    return feedback


EXTRACT_MISTAKES_PROMPT_VERSION = "extract-mistakes-v1"


@router.post(
    "/videos/{video_id}/extract-mistakes",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def extract_mistakes(video_id: int, _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)))
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    if not video.transcript:
        raise HTTPException(status_code=422, detail="У видео нет расшифровки (поле transcript пустое)")

    transcript_hash = hashlib.sha256(video.transcript.encode("utf-8")).hexdigest()[:16]
    idempotency_key = f"extract_mistakes:{video_id}:{transcript_hash}:{EXTRACT_MISTAKES_PROMPT_VERSION}"
    result = await db.execute(select(Job).where(Job.idempotency_key == idempotency_key))
    job = result.scalars().first()
    if not job:
        job = Job(
            type="extract_mistakes",
            status="pending",
            idempotency_key=idempotency_key,
            payload={
                "video_id": video_id,
                "transcript_hash": transcript_hash,
                "prompt_version": EXTRACT_MISTAKES_PROMPT_VERSION,
            },
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)
    return job


@router.get("/videos/{video_id}/mistakes", response_model=List[MistakeResponse])
async def list_mistakes_for_video(
    video_id: int,
    skip: int = 0,
    offset: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
):
    page_offset = max(offset if offset is not None else skip, 0)
    video_query = select(Video).where(Video.id == video_id)
    if not include_deleted:
        video_query = video_query.where(Video.deleted_at.is_(None))
    video = (await db.execute(video_query)).scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    query = select(Mistake).where(Mistake.video_id == video_id)
    if not include_deleted:
        query = query.where(Mistake.deleted_at.is_(None))
    result = await db.execute(query.order_by(Mistake.order_index, Mistake.id).offset(page_offset).limit(limit))
    return result.scalars().all()


@router.get("/mistakes/{mistake_id}", response_model=MistakeResponse)
async def get_mistake(mistake_id: int, include_deleted: bool = False, db: AsyncSession = Depends(get_db)):
    query = select(Mistake).where(Mistake.id == mistake_id)
    if not include_deleted:
        query = query.where(Mistake.deleted_at.is_(None))
    result = await db.execute(query)
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")
    return mistake


@router.patch("/mistakes/{mistake_id}", response_model=MistakeResponse)
async def update_mistake(
    mistake_id: int,
    mistake_in: MistakeUpdate,
    actor: str = "admin-ui",
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Mistake)
        .join(Video, Video.id == Mistake.video_id)
        .where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None), Video.deleted_at.is_(None))
        .with_for_update()
    )
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")

    changes = mistake_in.model_dump(exclude_unset=True)
    if not changes:
        return mistake

    before = {field: getattr(mistake, field) for field in changes}
    for field, value in changes.items():
        setattr(mistake, field, value)
    mistake.updated_at = datetime.now(timezone.utc)

    db.add(
        AuditEvent(
            actor=actor,
            entity_type="mistake",
            entity_id=mistake.id,
            action="mistake.updated",
            before=before,
            after={field: getattr(mistake, field) for field in changes},
            comment="Mistake updated manually",
        )
    )
    try:
        await db.commit()
        await db.refresh(mistake)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Mistake update failed. order_index may already be used.")
    return mistake


@router.delete("/mistakes/{mistake_id}", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_mistake(mistake_id: int, actor: str = "admin-ui", _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id).with_for_update())
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")

    now = datetime.now(timezone.utc)
    before = {"deleted_at": mistake.deleted_at.isoformat() if mistake.deleted_at else None}
    mistake.deleted_at = mistake.deleted_at or now

    cleared_final_assets = []
    result = await db.execute(select(FinalAsset).where(FinalAsset.mistake_id == mistake_id).with_for_update())
    for asset in result.scalars().all():
        previous = _storage_keys_for_cleanup(asset)
        cleared_final_assets.append({"id": asset.id, **previous})
        asset.status = "rejected"
        asset.storage_status = "cleanup_pending"
        _clear_storage_keys(asset)

    cleared_candidates = []
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.mistake_id == mistake_id).with_for_update())
    for candidate in result.scalars().all():
        previous = _storage_keys_for_cleanup(candidate)
        if any(previous.values()):
            cleared_candidates.append({"id": candidate.id, **previous})
        candidate.storage_status = "cleanup_pending" if any(previous.values()) else candidate.storage_status
        _clear_storage_keys(candidate)

    cleanup_targets = {"final_assets": cleared_final_assets, "candidates": cleared_candidates}
    db.add(
        AuditEvent(
            actor=actor,
            entity_type="mistake",
            entity_id=mistake.id,
            action="mistake.deleted",
            before={**before, **cleanup_targets},
            after={"deleted_at": mistake.deleted_at.isoformat()},
            comment="Mistake soft-deleted; related storage scheduled for cleanup",
        )
    )

    cleanup_job = await get_or_create_active_job(
        db,
        job_type="cleanup_storage",
        payload={"dry_run": False, "mode": "targeted", "reason": "delete_mistake", "mistake_id": mistake.id, "targets": cleanup_targets},
        idempotency_key=f"cleanup_storage:mistake:{mistake.id}",
    )
    await db.commit()
    await db.refresh(cleanup_job)
    return cleanup_job


async def _get_active_mistake(mistake_id: int, db: AsyncSession) -> Mistake:
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None)))
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")
    return mistake


async def _enqueue_legacy_search_all_job(mistake_id: int, db: AsyncSession):
    await _get_active_mistake(mistake_id, db)
    job = await get_or_create_active_job(
        db,
        job_type="search_all_queries",
        payload={"mistake_id": mistake_id},
        idempotency_key=f"search_all_queries:{mistake_id}",
    )
    await db.commit()
    await db.refresh(job)
    return job


async def _generate_queries_from_request(
    mistake_id: int,
    db: AsyncSession,
    search_in: SearchQueryGenerateRequest,
):
    providers = search_in.providers or [None]
    queries = []
    try:
        for provider in providers:
            queries.extend(
                await generate_search_queries_for_mistake(
                    mistake_id,
                    db,
                    sides=search_in.sides,
                    provider=provider,
                    limit_per_query=search_in.limit_per_query,
                )
            )
    except ValueError as e:
        message = str(e)
        if "not found" in message.lower():
            raise HTTPException(status_code=404, detail="Mistake not found")
        raise HTTPException(status_code=422, detail=message)
    return queries


def _validate_search_request(search_in: SearchQueryGenerateRequest) -> tuple[list[str], list[str] | None]:
    sides = search_in.sides or ["wrong", "right"]
    invalid_sides = [side for side in sides if side not in {"wrong", "right"}]
    if invalid_sides:
        raise HTTPException(status_code=422, detail=f"Invalid sides: {', '.join(invalid_sides)}")
    providers = None
    if search_in.providers is not None:
        providers = [normalize_search_provider(provider) for provider in search_in.providers]
        invalid_providers = [provider for provider in providers if provider not in SUPPORTED_SEARCH_PROVIDERS]
        if invalid_providers:
            raise HTTPException(status_code=422, detail=f"Invalid providers: {', '.join(invalid_providers)}")
    return sides, providers


async def _existing_queries_from_request(
    mistake_id: int,
    db: AsyncSession,
    search_in: SearchQueryGenerateRequest,
) -> list[SearchQuery]:
    await _get_active_mistake(mistake_id, db)
    sides, providers = _validate_search_request(search_in)
    query = select(SearchQuery).where(SearchQuery.mistake_id == mistake_id, SearchQuery.side.in_(sides))
    if providers is not None:
        query = query.where(SearchQuery.source_provider.in_(providers))
    result = await db.execute(query.order_by(SearchQuery.side, SearchQuery.source_provider, SearchQuery.id.desc()))
    return result.scalars().all()


async def _enqueue_run_search_jobs(
    mistake_id: int,
    db: AsyncSession,
    search_in: SearchQueryGenerateRequest,
):
    queries = await _existing_queries_from_request(mistake_id, db, search_in)
    if not queries:
        queries = await _generate_queries_from_request(mistake_id, db, search_in)

    jobs = []
    seen_search_scopes: set[tuple[int, str, str]] = set()
    for query in queries:
        search_scope = (mistake_id, query.side, query.source_provider)
        if search_scope in seen_search_scopes:
            continue
        seen_search_scopes.add(search_scope)
        job = await get_or_create_active_job(
            db,
            job_type="run_search",
            payload={"query_id": query.id, "limit_per_query": search_in.limit_per_query},
            idempotency_key=f"run_search:{mistake_id}:{query.side}:{query.source_provider}",
        )
        jobs.append(job)
    await db.commit()
    for job in jobs:
        await db.refresh(job)
    return jobs


@router.post(
    "/mistakes/{mistake_id}/generate-search-queries",
    response_model=List[SearchQueryResponse],
)
async def generate_search_queries(
    mistake_id: int,
    search_in: SearchQueryGenerateRequest = SearchQueryGenerateRequest(),
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    return await _generate_queries_from_request(mistake_id, db, search_in)


@router.post(
    "/mistakes/{mistake_id}/search-queries",
    response_model=SearchQueryResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_search_query(
    mistake_id: int,
    query_in: SearchQueryManualCreateRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    await _get_active_mistake(mistake_id, db)
    source_provider = normalize_search_provider(query_in.source_provider)
    if source_provider not in SUPPORTED_SEARCH_PROVIDERS:
        raise HTTPException(status_code=422, detail=f"Invalid provider: {source_provider}")
    query_text = query_in.query_text.strip()
    if not query_text:
        raise HTTPException(status_code=422, detail="query_text must not be blank")
    query = SearchQuery(
        mistake_id=mistake_id,
        side=query_in.side,
        source_provider=source_provider,
        query_text=query_text,
        language=query_in.language,
        status="pending",
        results_count=query_in.results_count,
    )
    db.add(query)
    await db.commit()
    await db.refresh(query)
    return query


async def _get_search_query_for_mistake(mistake_id: int, query_id: int, db: AsyncSession) -> SearchQuery:
    await _get_active_mistake(mistake_id, db)
    result = await db.execute(
        select(SearchQuery).where(SearchQuery.id == query_id, SearchQuery.mistake_id == mistake_id)
    )
    query = result.scalars().first()
    if not query:
        raise HTTPException(status_code=404, detail="SearchQuery not found")
    return query


@router.patch(
    "/mistakes/{mistake_id}/search-queries/{query_id}",
    response_model=SearchQueryResponse,
)
async def update_search_query(
    mistake_id: int,
    query_id: int,
    query_in: SearchQueryUpdateRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    query = await _get_search_query_for_mistake(mistake_id, query_id, db)
    changes = query_in.model_dump(exclude_unset=True)
    if "source_provider" in changes and changes["source_provider"] is not None:
        provider = normalize_search_provider(changes["source_provider"])
        if provider not in SUPPORTED_SEARCH_PROVIDERS:
            raise HTTPException(status_code=422, detail=f"Invalid provider: {provider}")
        changes["source_provider"] = provider
    if "query_text" in changes and changes["query_text"] is not None:
        query_text = changes["query_text"].strip()
        if not query_text:
            raise HTTPException(status_code=422, detail="query_text must not be blank")
        changes["query_text"] = query_text
    for field, value in changes.items():
        setattr(query, field, value)
    await db.commit()
    await db.refresh(query)
    return query


@router.delete(
    "/mistakes/{mistake_id}/search-queries/{query_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_search_query(
    mistake_id: int,
    query_id: int,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    query = await _get_search_query_for_mistake(mistake_id, query_id, db)
    await db.delete(query)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/mistakes/{mistake_id}/search-queries",
    response_model=List[SearchQueryResponse],
)
async def list_search_queries(
    mistake_id: int,
    side: str | None = Query(None, pattern="^(wrong|right)$"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    await _get_active_mistake(mistake_id, db)
    query = select(SearchQuery).where(SearchQuery.mistake_id == mistake_id)
    if side:
        query = query.where(SearchQuery.side == side)
    result = await db.execute(query.order_by(SearchQuery.id).offset(offset).limit(limit))
    return result.scalars().all()


@router.post(
    "/mistakes/{mistake_id}/search",
    response_model=List[JobResponse],
    status_code=status.HTTP_202_ACCEPTED,
)
async def search_mistake(
    mistake_id: int,
    search_in: SearchQueryGenerateRequest = SearchQueryGenerateRequest(),
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    return await _enqueue_run_search_jobs(mistake_id, db, search_in)


@router.post(
    "/mistakes/{mistake_id}/candidates/search",
    response_model=JobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def search_candidates_for_mistake(mistake_id: int, _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    return await _enqueue_legacy_search_all_job(mistake_id, db)
