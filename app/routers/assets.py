from datetime import datetime, timezone
import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from typing import List

from app.auth import require_admin_api_token
from app.config import settings
from app.db import get_db
from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.video import Video
from app.services.final_asset_service import select_candidate_as_final, upload_own_final_asset
from app.services.rights_service import confirm_final_asset_rights
from app.services.storage_service import StorageDownloadError
from app.models.job import Job
from app.services.job_service import get_or_create_active_job
from app.services.export_service import build_video_export_readiness, safe_video_export_slug
from app.schemas.asset import FinalAssetResponse, FinalAssetRightsConfirmRequest
from app.schemas.job import JobResponse

router = APIRouter(prefix="", tags=["assets"], dependencies=[Depends(require_admin_api_token)])


async def _get_video_or_404(video_id: int, db: AsyncSession) -> Video:
    result = await db.execute(select(Video).where(Video.id == video_id))
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    return video


def _latest_export_file(video: Video, filename: str) -> Path | None:
    if not settings.export_root.exists():
        return None
    safe_slug = safe_video_export_slug(video)
    matches = sorted(settings.export_root.glob(f"{safe_slug}_*/{filename}"), key=lambda path: path.parent.name, reverse=True)
    return matches[0] if matches else None


async def _read_upload_file_limited(file: UploadFile) -> bytes:
    max_bytes = settings.max_upload_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(status_code=422, detail="Uploaded image exceeds configured size limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _storage_path(storage_key: str) -> Path:
    path = (settings.storage_root / storage_key).resolve()
    root = settings.storage_root.resolve()
    if root not in path.parents and path != root:
        raise HTTPException(status_code=400, detail="Invalid storage key")
    return path


def _storage_file_response(asset: FinalAsset, storage_key: str | None, filename: str) -> FileResponse:
    if not storage_key:
        raise HTTPException(status_code=404, detail="Asset file is not available")
    path = _storage_path(storage_key)
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="Asset file is missing")
    media_type = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else None
    return FileResponse(path, media_type=media_type, filename=filename)


@router.get("/videos/{video_id}/assets", response_model=List[FinalAssetResponse])
async def list_assets_for_video(video_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FinalAsset).where(FinalAsset.video_id == video_id).order_by(FinalAsset.sort_order))
    assets = result.scalars().all()
    return assets


@router.get("/videos/{video_id}/final-assets", response_model=List[FinalAssetResponse])
async def list_final_assets_for_video(
    video_id: int,
    limit: int = 100,
    offset: int = 0,
    include_rejected: bool = False,
    db: AsyncSession = Depends(get_db),
):
    await _get_video_or_404(video_id, db)
    query = select(FinalAsset).where(FinalAsset.video_id == video_id)
    if not include_rejected:
        query = query.where(FinalAsset.status.in_(["approved", "exported"]))
    result = await db.execute(query.order_by(FinalAsset.sort_order, FinalAsset.id).offset(offset).limit(limit))
    return result.scalars().all()


async def _get_final_asset_or_404(asset_id: int, db: AsyncSession) -> FinalAsset:
    result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id))
    asset = result.scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Final asset not found")
    return asset


@router.get("/final-assets/{asset_id}/thumbnail")
async def get_final_asset_thumbnail(asset_id: int, db: AsyncSession = Depends(get_db)):
    asset = await _get_final_asset_or_404(asset_id, db)
    return _storage_file_response(asset, asset.storage_key_thumbnail, f"final_asset_{asset_id}_thumb.jpg")


@router.get("/final-assets/{asset_id}/processed")
async def get_final_asset_processed(asset_id: int, db: AsyncSession = Depends(get_db)):
    asset = await _get_final_asset_or_404(asset_id, db)
    return _storage_file_response(asset, asset.storage_key_processed, f"final_asset_{asset_id}_1920x1080.jpg")


@router.get("/final-assets/{asset_id}/original")
async def get_final_asset_original(asset_id: int, db: AsyncSession = Depends(get_db)):
    asset = await _get_final_asset_or_404(asset_id, db)
    return _storage_file_response(asset, asset.storage_key_original, f"final_asset_{asset_id}_original")


@router.post("/final-assets/{asset_id}/process", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def process_final_asset_job(
    asset_id: int,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    asset = await _get_final_asset_or_404(asset_id, db)
    source_ref = asset.storage_key_original or asset.source_url or str(asset.id)
    source_hash = hashlib.sha256(source_ref.encode("utf-8")).hexdigest()[:12]
    target_size = f"{settings.default_target_width}x{settings.default_target_height}"
    job = await get_or_create_active_job(
        db,
        job_type="process_final_asset",
        payload={"asset_id": asset_id},
        idempotency_key=f"process_final_asset:{asset_id}:{source_hash}:{target_size}",
    )
    await db.commit()
    await db.refresh(job)
    return job


@router.post("/final-assets/{asset_id}/confirm-rights", response_model=FinalAssetResponse)
async def confirm_rights_for_final_asset(
    asset_id: int,
    rights_in: FinalAssetRightsConfirmRequest,
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await confirm_final_asset_rights(
            asset_id=asset_id,
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


@router.delete("/final-assets/{asset_id}", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_final_asset(asset_id: int, actor: str = "admin-ui", _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id).with_for_update())
    asset = result.scalars().first()
    if not asset:
        raise HTTPException(status_code=404, detail="Final asset not found")

    cleanup_idempotency_key = f"cleanup_storage:final_asset:{asset.id}"
    storage_keys = [
        asset.storage_key_original,
        asset.storage_key_thumbnail,
        asset.storage_key_processed,
        asset.metadata_storage_key,
    ]
    old_storage_keys = [key for key in storage_keys if key]
    if asset.status == "rejected" and not any(storage_keys):
        result = await db.execute(
            select(Job)
            .where(Job.type == "cleanup_storage", Job.idempotency_key == cleanup_idempotency_key)
            .order_by(Job.created_at.desc(), Job.id.desc())
            .limit(1)
        )
        existing_cleanup_job = result.scalars().first()
        if existing_cleanup_job:
            return existing_cleanup_job

    before = {
        "status": asset.status,
        "storage_status": asset.storage_status,
        "storage_key_original": asset.storage_key_original,
        "storage_key_thumbnail": asset.storage_key_thumbnail,
        "storage_key_processed": asset.storage_key_processed,
        "metadata_storage_key": asset.metadata_storage_key,
    }

    asset.status = "rejected"
    asset.storage_status = "cleanup_pending"
    asset.storage_key_original = None
    asset.storage_key_thumbnail = None
    asset.storage_key_processed = None
    asset.metadata_storage_key = None
    asset.updated_at = datetime.now(timezone.utc)

    db.add(
        AuditEvent(
            actor=actor,
            entity_type="final_asset",
            entity_id=asset.id,
            action="final_asset.deleted",
            before=before,
            after={"status": asset.status, "storage_status": asset.storage_status},
            comment="Final asset deleted; files scheduled for cleanup",
        )
    )

    cleanup_job = await get_or_create_active_job(
        db,
        job_type="cleanup_storage",
        payload={
            "dry_run": False,
            "mode": "targeted",
            "reason": "delete_final_asset",
            "asset_id": asset.id,
            "old_storage_keys": old_storage_keys,
        },
        idempotency_key=cleanup_idempotency_key,
    )
    await db.commit()
    await db.refresh(cleanup_job)
    return cleanup_job


@router.post("/candidates/{candidate_id}/select-final", response_model=FinalAssetResponse)
async def select_final_candidate(candidate_id: int, _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    """Manually promotes a candidate to the final asset for its mistake side."""
    try:
        return await select_candidate_as_final(candidate_id, db)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    except StorageDownloadError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/mistakes/{mistake_id}/upload-final-asset", response_model=FinalAssetResponse, status_code=status.HTTP_201_CREATED)
async def upload_final_asset_for_mistake(
    mistake_id: int,
    side: str = Form(...),
    license_note: str = Form(...),
    file: UploadFile = File(...),
    actor: str = Form("admin-ui"),
    license_document_ref: str | None = Form(None),
    author_name: str | None = Form(None),
    caption: str | None = Form(None),
    _admin: None = Depends(require_admin_api_token),
    db: AsyncSession = Depends(get_db),
):
    data = await _read_upload_file_limited(file)
    if not data:
        raise HTTPException(status_code=422, detail="Uploaded file is empty")
    try:
        return await upload_own_final_asset(
            mistake_id=mistake_id,
            side=side,
            file_bytes=data,
            original_filename=file.filename,
            license_note=license_note,
            license_document_ref=license_document_ref,
            author_name=author_name,
            caption=caption,
            actor=actor,
            db=db,
        )
    except StorageDownloadError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except ValueError as e:
        detail = str(e)
        status_code = 404 if "not found" in detail.lower() else 422
        raise HTTPException(status_code=status_code, detail=detail)


@router.post("/videos/{video_id}/export", response_model=JobResponse, status_code=status.HTTP_202_ACCEPTED)
async def export_video_assets(video_id: int, _admin: None = Depends(require_admin_api_token), db: AsyncSession = Depends(get_db)):
    """
    Запускает сборку финального манифеста и копирование файлов в папку exports.
    """
    try:
        readiness = await build_video_export_readiness(video_id, db)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not readiness["can_export"]:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Video is not export-ready",
                "warnings": readiness["warnings"],
                "ready_asset_count": readiness["ready_asset_count"],
                "active_mistake_count": readiness["active_mistake_count"],
            },
        )

    active_legacy = await db.execute(
        select(Job).where(
            Job.idempotency_key == f"export_final_assets:{video_id}",
            Job.status.in_(["pending", "processing", "running"]),
        )
    )
    new_job = active_legacy.scalars().first()
    if not new_job:
        new_job = await get_or_create_active_job(
            db,
            job_type="export_video",
            payload={"video_id": video_id},
            idempotency_key=f"export_video:{video_id}",
        )
    await db.commit()
    await db.refresh(new_job)

    return new_job

@router.get("/videos/{video_id}/manifest")
async def get_video_manifest(video_id: int, db: AsyncSession = Depends(get_db)):
    video = await _get_video_or_404(video_id, db)
    manifest_path = _latest_export_file(video, "manifest.json")
    if not manifest_path:
        raise HTTPException(status_code=404, detail="Manifest not found")
    return FileResponse(manifest_path, media_type="application/json", filename="manifest.json")


@router.get("/videos/{video_id}/assets-csv")
async def get_video_assets_csv(video_id: int, db: AsyncSession = Depends(get_db)):
    video = await _get_video_or_404(video_id, db)
    csv_path = _latest_export_file(video, "assets.csv")
    if not csv_path:
        raise HTTPException(status_code=404, detail="Assets CSV not found")
    return FileResponse(csv_path, media_type="text/csv", filename="assets.csv")
