import mimetypes

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy import func
from sqlalchemy.future import select
from typing import List, Literal
from uuid import uuid4
from pydantic import BaseModel

from app.auth import require_admin_api_token
from app.config import settings
from app.db import get_db
from app.models.audit import BlockedDomain
from app.models.candidate import CandidateReview, ImageCandidate
from app.models.job import Job
from app.schemas.candidate import (
    CandidateReviewAggregate,
    CandidateReviewCreate,
    CandidateReviewRunRequest,
    CandidateReviewResponse,
    CandidateRightsConfirmRequest,
    CandidateReferenceRequest,
    CandidateBlockDomainRequest,
    ImageCandidateResponse,
    ReferenceBriefResponse,
    ReferenceBriefUpdate,
    ReviewerCliStatus,
)
from app.schemas.job import JobResponse
from app.services.candidate_status import candidate_status_filter_values
from app.services.job_service import get_or_create_active_job
from app.services.rights_service import confirm_candidate_rights
from app.services.candidate_action_service import mark_candidate_as_reference, block_candidate_domain
from app.services.candidate_review_runner import build_candidate_reviewer_payload, get_reviewer_cli_readiness
from app.services.candidate_review_service import (
    EXPECTED_REVIEWERS,
    get_candidate_review_aggregate as load_candidate_review_aggregate,
    upsert_candidate_review,
)
from app.services.storage_service import _normalize_storage_key, _path_for_storage_key
from app.services.reference_brief_service import (
    get_reference_brief,
    get_reference_candidate,
    update_reference_brief_manual,
)

router = APIRouter(prefix="", tags=["candidates"], dependencies=[Depends(require_admin_api_token)])


async def _get_or_create_reviewer_job(
    candidate_id: int,
    reviewer_name: str,
    prompt_version: str,
    force: bool,
    db: AsyncSession,
) -> Job:
    idempotency_key = f"candidate_review:{candidate_id}:{reviewer_name}:{prompt_version}"
    if force:
        idempotency_key = f"{idempotency_key}:force:{uuid4().hex}"
    else:
        result = await db.execute(select(Job).where(Job.idempotency_key == idempotency_key))
        existing = result.scalars().first()
        if existing:
            return existing

    job = Job(
        type="run_candidate_reviewer",
        status="pending",
        idempotency_key=idempotency_key,
        payload={
            "candidate_id": candidate_id,
            "reviewer_name": reviewer_name,
            "prompt_version": prompt_version,
            "force": force,
        },
    )
    try:
        async with db.begin_nested():
            db.add(job)
            await db.flush()
    except IntegrityError:
        result = await db.execute(select(Job).where(Job.idempotency_key == idempotency_key))
        existing = result.scalars().first()
        if existing:
            return existing
        raise
    return job

class ReviewRequest(BaseModel):
    action: Literal['approve', 'approve_final', 'approve_reference', 'reject']
    reject_reason: str | None = None
    reason: str | None = None
    comment: str | None = None

    @property
    def normalized_action(self) -> str:
        return 'approve_final' if self.action == 'approve' else self.action

    @property
    def effective_reason(self) -> str | None:
        return self.reject_reason or self.reason


@router.get("/mistakes/{mistake_id}/candidates", response_model=List[ImageCandidateResponse])
async def list_candidates_for_mistake(
    mistake_id: int,
    skip: int = 0,
    offset: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    side: Literal["wrong", "right"] | None = None,
    status_filter: str | None = Query(None, alias="status"),
    rights_status: str | None = None,
    source_provider: str | None = None,
    sort: str = "id",
    db: AsyncSession = Depends(get_db),
):
    page_offset = max(offset if offset is not None else skip, 0)
    query = select(ImageCandidate).where(ImageCandidate.mistake_id == mistake_id)
    if side:
        query = query.where(ImageCandidate.side == side)
    if status_filter:
        query = query.where(ImageCandidate.status.in_(candidate_status_filter_values(status_filter)))
    if rights_status:
        query = query.where(ImageCandidate.rights_status == rights_status)
    if source_provider:
        query = query.where(ImageCandidate.source_provider == source_provider)

    resolution = func.coalesce(ImageCandidate.original_width, 0) * func.coalesce(ImageCandidate.original_height, 0)
    sort_options = {
        "id": [ImageCandidate.id.asc()],
        "-id": [ImageCandidate.id.desc()],
        "review_score": [ImageCandidate.review_score.asc().nullslast(), ImageCandidate.id.asc()],
        "-review_score": [ImageCandidate.review_score.desc().nullslast(), ImageCandidate.id.asc()],
        "resolution": [resolution.asc(), ImageCandidate.id.asc()],
        "-resolution": [resolution.desc(), ImageCandidate.id.asc()],
        "created_at": [ImageCandidate.created_at.asc(), ImageCandidate.id.asc()],
        "-created_at": [ImageCandidate.created_at.desc(), ImageCandidate.id.asc()],
    }
    if sort not in sort_options:
        raise HTTPException(status_code=422, detail="Unsupported sort. Use id, -id, review_score, -review_score, resolution, -resolution, created_at, or -created_at.")

    result = await db.execute(query.order_by(*sort_options[sort]).offset(page_offset).limit(limit))
    return result.scalars().all()


@router.get("/candidates/{candidate_id}", response_model=ImageCandidateResponse)
async def get_candidate(candidate_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return candidate


def _candidate_storage_file_response(candidate: ImageCandidate, storage_key: str | None, filename_prefix: str) -> FileResponse:
    normalized_key = _normalize_storage_key(storage_key)
    if not normalized_key:
        raise HTTPException(status_code=404, detail="Candidate file is not available")
    try:
        path = _path_for_storage_key(settings.storage_root, normalized_key)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid candidate storage key")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Candidate file is missing")
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    suffix = path.suffix or ".jpg"
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"{filename_prefix}{suffix}",
        content_disposition_type="inline",
    )


@router.get("/candidates/{candidate_id}/original")
async def get_candidate_original(candidate_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return _candidate_storage_file_response(candidate, candidate.storage_key_original, f"candidate_{candidate_id}_original")


@router.get("/candidates/{candidate_id}/thumbnail")
async def get_candidate_thumbnail(candidate_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    return _candidate_storage_file_response(candidate, candidate.storage_key_thumbnail, f"candidate_{candidate_id}_thumb")


@router.post("/candidates/{candidate_id}/download", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def download_candidate_original(
    candidate_id: int,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not candidate.image_url:
        raise HTTPException(status_code=422, detail="Candidate has no image_url")

    key_hash = candidate.image_url_hash or str(candidate.id)
    job = await get_or_create_active_job(
        db,
        job_type="download_candidate",
        payload={"candidate_id": candidate_id},
        idempotency_key=f"download_candidate:{candidate_id}:{key_hash}",
    )
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/candidates/{candidate_id}/review", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def review_candidate(
    candidate_id: int,
    review_in: ReviewRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    """
    Принимает решение по кандидату.
    В MVP создаем Job для асинхронного переноса картинки в FinalAsset.
    """
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
        
    action = review_in.normalized_action
    if action == "approve_final" and not candidate.may_use_directly:
        raise HTTPException(
            status_code=403,
            detail="Candidate rights must be confirmed before selecting it as final",
        )

    reason = review_in.effective_reason
    payload = {
        "candidate_id": candidate_id,
        "action": action,
        "reason": reason,
        "reject_reason": reason,
        "comment": review_in.comment,
    }
    idempotency_key = f"review_candidate:{candidate_id}:{action}:{reason or ''}:{review_in.comment or ''}"
    job = await get_or_create_active_job(
        db,
        job_type="review_candidate",
        payload=payload,
        idempotency_key=idempotency_key,
    )
    await db.commit()
    await db.refresh(job)

    return job


@router.post("/candidates/{candidate_id}/use-as-reference", response_model=ImageCandidateResponse)
async def use_candidate_as_reference(
    candidate_id: int,
    reference_in: CandidateReferenceRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await mark_candidate_as_reference(
            candidate_id=candidate_id,
            db=db,
            mark_high_value=reference_in.mark_high_value,
            comment=reference_in.comment,
            actor=reference_in.actor,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/candidates/{candidate_id}/reference-brief", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_reference_brief_job(
    candidate_id: int,
    prompt_version: str = "mock-v1",
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        await get_reference_candidate(candidate_id, db)
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail)

    idempotency_key = f"reference_brief:{candidate_id}:{prompt_version}"
    existing_result = await db.execute(
        select(Job).where(
            Job.idempotency_key == idempotency_key,
            Job.status.in_(["pending", "processing", "running", "completed", "succeeded"]),
        )
    )
    job = existing_result.scalars().first()
    if not job:
        job = await get_or_create_active_job(
            db,
            job_type="create_reference_brief",
            payload={"candidate_id": candidate_id, "prompt_version": prompt_version},
            idempotency_key=idempotency_key,
        )
    await db.commit()
    await db.refresh(job)
    return job


@router.get("/candidates/{candidate_id}/reference-brief", response_model=ReferenceBriefResponse)
async def read_reference_brief(candidate_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await get_reference_brief(candidate_id, db)
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail)


@router.patch("/candidates/{candidate_id}/reference-brief", response_model=ReferenceBriefResponse)
async def update_reference_brief(
    candidate_id: int,
    brief_in: ReferenceBriefUpdate,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await update_reference_brief_manual(
            candidate_id,
            db,
            updates=brief_in.model_dump(exclude_unset=True),
        )
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail)


@router.post("/candidates/{candidate_id}/block-domain")
async def block_domain_for_candidate(
    candidate_id: int,
    block_in: CandidateBlockDomainRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        blocked = await block_candidate_domain(
            candidate_id=candidate_id,
            db=db,
            reason=block_in.reason,
            actor=block_in.actor,
        )
        return {"id": blocked.id, "domain": blocked.domain, "reason": blocked.reason}
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail)


@router.post("/candidates/{candidate_id}/confirm-rights", response_model=ImageCandidateResponse)
async def confirm_rights_for_candidate(
    candidate_id: int,
    rights_in: CandidateRightsConfirmRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await confirm_candidate_rights(
            candidate_id=candidate_id,
            db=db,
            rights_status=rights_in.rights_status,
            source_url=rights_in.source_url,
            license_note=rights_in.license_note,
            license_document_ref=rights_in.license_document_ref,
            author_name=rights_in.author_name,
            comment=rights_in.comment,
            actor=rights_in.actor,
        )
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail)


@router.get("/candidates/{candidate_id}/reviews", response_model=List[CandidateReviewResponse])
async def list_candidate_reviews(candidate_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    if not result.scalars().first():
        raise HTTPException(status_code=404, detail="Candidate not found")

    result = await db.execute(
        select(CandidateReview)
        .where(CandidateReview.candidate_id == candidate_id)
        .order_by(CandidateReview.reviewer_name)
    )
    return result.scalars().all()


@router.get("/candidates/{candidate_id}/reviews/aggregate", response_model=CandidateReviewAggregate)
async def get_candidate_review_aggregate(candidate_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await load_candidate_review_aggregate(candidate_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/candidates/{candidate_id}/reviews/{reviewer_name}", response_model=CandidateReviewResponse)
async def put_candidate_review(
    candidate_id: int,
    reviewer_name: str,
    review_in: CandidateReviewCreate,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    if review_in.reviewer_name.strip().lower() != reviewer_name.strip().lower():
        raise HTTPException(status_code=422, detail="reviewer_name in path and body must match")

    try:
        review, _aggregate = await upsert_candidate_review(
            candidate_id=candidate_id,
            reviewer_name=reviewer_name,
            reviewer_version=review_in.reviewer_version,
            score=review_in.score,
            verdict=review_in.verdict,
            reason=review_in.reason,
            flags=review_in.flags,
            response_time_ms=review_in.response_time_ms,
            db=db,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return review

@router.get("/candidates/{candidate_id}/review-payload")
async def get_candidate_review_payload(
    candidate_id: int,
    prompt_version: str | None = None,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await build_candidate_reviewer_payload(candidate_id, db, prompt_version)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/reviewers/status", response_model=List[ReviewerCliStatus])
async def get_reviewer_status(_admin: None = Depends(require_admin_api_token)):
    return list(get_reviewer_cli_readiness().values())


@router.post("/candidates/{candidate_id}/reviews/run", response_model=List[JobResponse], status_code=status.HTTP_202_ACCEPTED)
async def run_candidate_reviewers(
    candidate_id: int,
    run_in: CandidateReviewRunRequest | None = None,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise HTTPException(status_code=404, detail="Candidate not found")
    if not candidate.storage_key_original:
        raise HTTPException(status_code=409, detail="Candidate must be downloaded before AI review")

    run_in = run_in or CandidateReviewRunRequest()
    reviewers = [reviewer.strip().lower() for reviewer in run_in.reviewers]
    if not reviewers:
        raise HTTPException(status_code=422, detail="reviewers must not be empty")
    if len(reviewers) != len(set(reviewers)):
        raise HTTPException(status_code=422, detail="reviewers must be unique")
    invalid = [reviewer for reviewer in reviewers if reviewer not in EXPECTED_REVIEWERS]
    if invalid:
        raise HTTPException(status_code=422, detail=f"unknown reviewers: {', '.join(invalid)}")

    prompt_version = run_in.prompt_version or settings.reviewer_prompt_version
    jobs = []
    for reviewer in reviewers:
        jobs.append(
            await _get_or_create_reviewer_job(
                candidate_id=candidate_id,
                reviewer_name=reviewer,
                prompt_version=prompt_version,
                force=run_in.force,
                db=db,
            )
        )

    await db.commit()
    for job in jobs:
        await db.refresh(job)
    return jobs
