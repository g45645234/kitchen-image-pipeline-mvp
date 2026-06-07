from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.auth import require_admin_api_token
from app.db import get_db
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.candidate import ImageCandidate
from app.models.mistake import Mistake
from app.models.video import Video
from app.schemas.video import VideoCreate, VideoExportReadiness, VideoResponse, VideoUpdate
from app.schemas.job import JobResponse
from app.services.export_service import build_video_export_readiness
from app.services.job_service import get_or_create_active_job

router = APIRouter(prefix="/videos", tags=["videos"], dependencies=[Depends(require_admin_api_token)])


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


@router.post("", response_model=VideoResponse, status_code=status.HTTP_201_CREATED)
async def create_video(
    video_in: VideoCreate,
    actor: str = "admin-ui",
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    db_video = Video(**video_in.model_dump())
    db.add(db_video)
    try:
        await db.flush()
        db.add(
            AuditEvent(
                actor=actor,
                entity_type="video",
                entity_id=db_video.id,
                action="video.created",
                before=None,
                after=video_in.model_dump(),
                comment="Video created manually",
            )
        )
        await db.commit()
        await db.refresh(db_video)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Video with this slug might already exist.")
    return db_video


@router.get("", response_model=List[VideoResponse])
async def list_videos(
    skip: int = 0,
    offset: int | None = None,
    limit: int = Query(100, ge=1, le=500),
    status_filter: str | None = Query(None, alias="status"),
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
):
    page_offset = max(offset if offset is not None else skip, 0)
    query = select(Video)
    if not include_deleted:
        query = query.where(Video.deleted_at.is_(None))
    if status_filter:
        query = query.where(Video.status == status_filter)
    result = await db.execute(query.order_by(Video.created_at.desc(), Video.id.desc()).offset(page_offset).limit(limit))
    return result.scalars().all()


@router.get("/{video_id}", response_model=VideoResponse)
async def get_video(video_id: int, include_deleted: bool = False, db: AsyncSession = Depends(get_db)):
    query = select(Video).where(Video.id == video_id)
    if not include_deleted:
        query = query.where(Video.deleted_at.is_(None))
    result = await db.execute(query)
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


@router.get("/{video_id}/export-readiness", response_model=VideoExportReadiness)
async def get_video_export_readiness(video_id: int, db: AsyncSession = Depends(get_db)):
    try:
        return await build_video_export_readiness(video_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.patch("/{video_id}", response_model=VideoResponse)
async def update_video(
    video_id: int,
    video_in: VideoUpdate,
    actor: str = "admin-ui",
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)).with_for_update())
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    changes = video_in.model_dump(exclude_unset=True)
    if not changes:
        return video

    before = {field: getattr(video, field) for field in changes}
    for field, value in changes.items():
        setattr(video, field, value)
    video.updated_at = datetime.now(timezone.utc)

    db.add(
        AuditEvent(
            actor=actor,
            entity_type="video",
            entity_id=video.id,
            action="video.updated",
            before=before,
            after={field: getattr(video, field) for field in changes},
            comment="Video updated manually",
        )
    )
    try:
        await db.commit()
        await db.refresh(video)
    except Exception:
        await db.rollback()
        raise HTTPException(status_code=400, detail="Video update failed. Slug may already exist.")
    return video


@router.delete("/{video_id}", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_video(video_id: int, actor: str = "admin-ui", _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.id == video_id).with_for_update())
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    now = datetime.now(timezone.utc)
    before = {"deleted_at": video.deleted_at.isoformat() if video.deleted_at else None, "status": video.status}
    video.deleted_at = video.deleted_at or now
    video.status = "deleted"

    result = await db.execute(select(Mistake).where(Mistake.video_id == video_id).with_for_update())
    mistakes = result.scalars().all()
    mistake_ids = [mistake.id for mistake in mistakes]
    for mistake in mistakes:
        mistake.deleted_at = mistake.deleted_at or now

    cleared_final_assets = []
    cleared_candidates = []
    if mistake_ids:
        result = await db.execute(select(FinalAsset).where(FinalAsset.mistake_id.in_(mistake_ids)).with_for_update())
        for asset in result.scalars().all():
            previous = _storage_keys_for_cleanup(asset)
            cleared_final_assets.append({"id": asset.id, **previous})
            asset.status = "rejected"
            asset.storage_status = "cleanup_pending"
            _clear_storage_keys(asset)

        result = await db.execute(select(ImageCandidate).where(ImageCandidate.mistake_id.in_(mistake_ids)).with_for_update())
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
            entity_type="video",
            entity_id=video.id,
            action="video.deleted",
            before={**before, **cleanup_targets},
            after={"deleted_at": video.deleted_at.isoformat(), "status": video.status},
            comment="Video soft-deleted; related storage scheduled for cleanup",
        )
    )

    cleanup_job = await get_or_create_active_job(
        db,
        job_type="cleanup_storage",
        payload={"dry_run": False, "mode": "targeted", "reason": "delete_video", "video_id": video.id, "targets": cleanup_targets},
        idempotency_key=f"cleanup_storage:video:{video.id}",
    )
    await db.commit()
    await db.refresh(cleanup_job)
    return cleanup_job
