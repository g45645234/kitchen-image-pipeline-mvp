from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import settings
from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.candidate import ImageCandidate
from app.models.mistake import Mistake
from app.models.video import Video
from app.services.candidate_status import APPROVED_FINAL, is_approved_final_status
from app.services.image_processing_limiter import image_processing_slot
from app.services.job_service import get_or_create_active_job
from app.services.storage_service import StorageDownloadError, _normalize_storage_key, _path_for_storage_key, store_final_asset_derivative_files, store_final_asset_original, store_uploaded_final_asset_original_file


STORAGE_KEY_FIELDS = (
    "storage_key_original",
    "storage_key_thumbnail",
    "storage_key_processed",
    "metadata_storage_key",
)


EXPORTABLE_RIGHTS_STATUSES = {"manual_licensed", "own", "free_to_use"}


def final_asset_health(asset: FinalAsset) -> dict:
    warnings: list[str] = []
    missing_storage_fields: list[str] = []
    invalid_storage_fields: list[str] = []
    missing_files: list[dict[str, str]] = []
    empty_files: list[dict[str, str]] = []
    available_storage_fields: list[str] = []

    if asset.rights_status not in EXPORTABLE_RIGHTS_STATUSES or not asset.may_use_directly:
        warnings.append("rights_not_exportable")
    if asset.storage_status not in {"ok", "exported"}:
        warnings.append("storage_status_not_ok")

    required_fields = ("storage_key_original", "storage_key_thumbnail", "storage_key_processed")
    for field in required_fields:
        raw_key = getattr(asset, field, None)
        if not raw_key:
            missing_storage_fields.append(field)
            continue
        storage_key = _normalize_storage_key(raw_key)
        if not storage_key:
            invalid_storage_fields.append(field)
            continue
        try:
            file_path = _path_for_storage_key(settings.storage_root, storage_key)
        except ValueError:
            invalid_storage_fields.append(field)
            continue
        if not file_path.exists() or not file_path.is_file():
            missing_files.append({"field": field, "storage_key": storage_key})
            continue
        if file_path.stat().st_size <= 0:
            empty_files.append({"field": field, "storage_key": storage_key})
            continue
        available_storage_fields.append(field)

    if missing_storage_fields:
        warnings.append("missing_storage_keys")
    if invalid_storage_fields:
        warnings.append("invalid_storage_keys")
    if missing_files:
        warnings.append("missing_storage_files")
    if empty_files:
        warnings.append("empty_storage_files")
    if not asset.storage_key_processed:
        warnings.append("missing_processed_asset")

    return {
        "ok": not warnings,
        "warnings": sorted(set(warnings)),
        "missing_storage_fields": missing_storage_fields,
        "invalid_storage_fields": invalid_storage_fields,
        "missing_files": missing_files,
        "empty_files": empty_files,
        "available_storage_fields": available_storage_fields,
    }


def _storage_keys(asset: FinalAsset | None) -> set[str]:
    if not asset:
        return set()
    return {value for field in STORAGE_KEY_FIELDS if (value := getattr(asset, field, None))}


async def _enqueue_cleanup_for_replaced_keys(
    db: AsyncSession,
    *,
    asset: FinalAsset,
    previous_keys: set[str],
    reason: str,
):
    old_storage_keys = sorted(previous_keys - _storage_keys(asset))
    if not old_storage_keys:
        return None
    return await get_or_create_active_job(
        db,
        job_type="cleanup_storage",
        payload={
            "dry_run": False,
            "mode": "targeted",
            "reason": reason,
            "asset_id": asset.id,
            "old_storage_keys": old_storage_keys,
        },
        idempotency_key=f"cleanup_storage:{reason}:{asset.id}",
    )


def copy_candidate_to_asset(asset: FinalAsset, candidate: ImageCandidate, mistake: Mistake) -> None:
    asset.video_id = mistake.video_id
    asset.mistake_id = mistake.id
    asset.candidate_id = candidate.id
    asset.side = candidate.side
    asset.source_type = candidate.source_type
    asset.source_url = candidate.image_url
    asset.license_label = candidate.license_label
    asset.author_name = candidate.author_name
    asset.rights_status = candidate.rights_status
    asset.may_use_directly = candidate.may_use_directly
    asset.license_note = None
    asset.license_document_ref = None
    asset.rights_confirmed_by = None
    asset.rights_confirmed_at = None
    asset.storage_key_original = candidate.storage_key_original
    asset.storage_key_thumbnail = candidate.storage_key_thumbnail
    asset.storage_key_processed = candidate.storage_key_processed
    asset.metadata_storage_key = None
    asset.original_exif_preserved = False
    asset.processed_exif_stripped = False
    asset.storage_status = candidate.storage_status
    asset.status = "approved"
    if not asset.storage_key_original:
        asset.storage_status = "pending"


async def process_final_asset_files(asset_id: int, db: AsyncSession, original_filename: str | None = None) -> FinalAsset:
    result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id).with_for_update())
    asset = result.scalars().first()
    if not asset:
        raise ValueError(f"FinalAsset {asset_id} not found")

    if not asset.storage_key_original and not asset.source_url:
        raise StorageDownloadError(f"FinalAsset {asset.id} has no original storage file or source_url")

    async with image_processing_slot(db):
        if not asset.storage_key_original:
            asset.storage_key_original = await store_final_asset_original(asset)

        derivative_keys = await asyncio.to_thread(store_final_asset_derivative_files, asset, original_filename=original_filename)
        asset.storage_key_thumbnail = derivative_keys["storage_key_thumbnail"]
        asset.storage_key_processed = derivative_keys["storage_key_processed"]
        asset.metadata_storage_key = derivative_keys["metadata_storage_key"]
        asset.original_exif_preserved = True
        asset.processed_exif_stripped = True
        asset.storage_status = "ok"

    await db.commit()
    await db.refresh(asset)
    return asset


async def select_candidate_as_final(
    candidate_id: int,
    db: AsyncSession,
    reviewed_by: str = "manual-final",
) -> FinalAsset:
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    if not candidate.may_use_directly:
        raise PermissionError("Candidate rights must be confirmed before selecting it as final")

    result = await db.execute(
        select(Mistake)
        .where(Mistake.id == candidate.mistake_id, Mistake.deleted_at.is_(None))
        .with_for_update()
    )
    mistake = result.scalars().first()
    if not mistake:
        raise ValueError(f"Mistake {candidate.mistake_id} not found")

    result = await db.execute(select(Video).where(Video.id == mistake.video_id, Video.deleted_at.is_(None)))
    video = result.scalars().first()
    if not video:
        raise ValueError(f"Video {mistake.video_id} not found")

    result = await db.execute(
        select(FinalAsset)
        .where(FinalAsset.mistake_id == candidate.mistake_id, FinalAsset.side == candidate.side)
        .order_by(FinalAsset.id)
        .with_for_update()
    )
    asset = result.scalars().first()
    previous_candidate_id = asset.candidate_id if asset else None
    previous_storage_keys = _storage_keys(asset)
    if not asset:
        asset = FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side=candidate.side,
            source_type=candidate.source_type,
            rights_status=candidate.rights_status,
            may_use_directly=candidate.may_use_directly,
            storage_status=candidate.storage_status,
            status="approved",
        )
        db.add(asset)

    copy_candidate_to_asset(asset, candidate, mistake)

    rights_result = await db.execute(
        select(AuditEvent)
        .where(
            AuditEvent.entity_type == "candidate",
            AuditEvent.entity_id == candidate.id,
            AuditEvent.action == "rights_confirmed",
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    rights_event = rights_result.scalars().first()
    if rights_event:
        after = rights_event.after or {}
        asset.license_note = after.get("license_note") or rights_event.comment
        asset.license_document_ref = after.get("license_document_ref")
        asset.rights_confirmed_by = rights_event.actor
        asset.rights_confirmed_at = rights_event.created_at

    if previous_candidate_id and previous_candidate_id != candidate.id:
        result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == previous_candidate_id).with_for_update())
        previous_candidate = result.scalars().first()
        if previous_candidate and is_approved_final_status(previous_candidate.status):
            previous_candidate.status = "auto_reviewed" if previous_candidate.review_score is not None else "new"
            if previous_candidate.reviewed_by in {"manual-final", "legacy-review"}:
                previous_candidate.reviewed_by = None
                previous_candidate.reviewed_at = None

    candidate_before = {
        "status": candidate.status,
        "usage_role": candidate.usage_role,
        "reviewed_by": candidate.reviewed_by,
        "reviewed_at": candidate.reviewed_at.isoformat() if candidate.reviewed_at else None,
    }
    candidate.status = APPROVED_FINAL
    candidate.reviewed_by = reviewed_by
    candidate.reviewed_at = datetime.now(timezone.utc)

    await db.flush()

    db.add(
        AuditEvent(
            actor=reviewed_by,
            entity_type="candidate",
            entity_id=candidate.id,
            action="candidate.approved_final",
            before=candidate_before,
            after={
                "status": candidate.status,
                "usage_role": candidate.usage_role,
                "reviewed_by": candidate.reviewed_by,
                "reviewed_at": candidate.reviewed_at.isoformat() if candidate.reviewed_at else None,
                "final_asset_id": asset.id,
            },
            comment="Candidate selected as final asset",
            created_at=datetime.now(timezone.utc),
        )
    )

    needs_original = asset.source_url and not asset.storage_key_original
    needs_derivatives = asset.storage_key_original and (not asset.storage_key_processed or not asset.storage_key_thumbnail)
    if needs_original or needs_derivatives:
        try:
            async with image_processing_slot(db):
                if needs_original:
                    storage_key = await store_final_asset_original(asset)
                    asset.storage_key_original = storage_key
                    asset.storage_status = "ok"
                    candidate.storage_key_original = storage_key
                    candidate.storage_status = "ok"

                if asset.storage_key_original and (not asset.storage_key_processed or not asset.storage_key_thumbnail):
                    derivative_keys = await asyncio.to_thread(store_final_asset_derivative_files, asset, allow_missing_original=True)
                    if derivative_keys:
                        asset.storage_key_thumbnail = derivative_keys["storage_key_thumbnail"]
                        asset.storage_key_processed = derivative_keys["storage_key_processed"]
                        asset.metadata_storage_key = derivative_keys["metadata_storage_key"]
                        asset.original_exif_preserved = True
                        asset.processed_exif_stripped = True
                        asset.storage_status = "ok"
        except StorageDownloadError:
            await db.rollback()
            raise

    await _enqueue_cleanup_for_replaced_keys(
        db,
        asset=asset,
        previous_keys=previous_storage_keys,
        reason="replace_final_asset",
    )

    await db.commit()
    await db.refresh(asset)
    return asset


async def upload_own_final_asset(
    mistake_id: int,
    side: str,
    file_bytes: bytes,
    original_filename: str | None,
    license_note: str,
    db: AsyncSession,
    actor: str = "admin-ui",
    license_document_ref: str | None = None,
    author_name: str | None = None,
    caption: str | None = None,
) -> FinalAsset:
    side = side.strip().lower()
    if side not in {"wrong", "right"}:
        raise ValueError("side must be wrong or right")
    if not license_note or not license_note.strip():
        raise ValueError("license_note is required for uploaded final assets")

    result = await db.execute(
        select(Mistake)
        .where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None))
        .with_for_update()
    )
    mistake = result.scalars().first()
    if not mistake:
        raise ValueError(f"Mistake {mistake_id} not found")

    result = await db.execute(select(Video).where(Video.id == mistake.video_id, Video.deleted_at.is_(None)))
    video = result.scalars().first()
    if not video:
        raise ValueError(f"Video {mistake.video_id} not found")

    result = await db.execute(
        select(FinalAsset)
        .where(FinalAsset.mistake_id == mistake_id, FinalAsset.side == side)
        .order_by(FinalAsset.id)
        .with_for_update()
    )
    asset = result.scalars().first()
    previous = None
    previous_storage_keys = _storage_keys(asset)
    if asset:
        previous = {
            "candidate_id": asset.candidate_id,
            "source_type": asset.source_type,
            "storage_key_original": asset.storage_key_original,
            "storage_key_thumbnail": asset.storage_key_thumbnail,
            "storage_key_processed": asset.storage_key_processed,
            "metadata_storage_key": asset.metadata_storage_key,
        }
    else:
        asset = FinalAsset(video_id=mistake.video_id, mistake_id=mistake.id, side=side, source_type="own_upload", rights_status="own")
        db.add(asset)

    asset.video_id = mistake.video_id
    asset.mistake_id = mistake.id
    asset.side = side
    asset.candidate_id = None
    asset.source_type = "own_upload"
    asset.source_url = None
    asset.license_label = "own_upload"
    asset.author_name = author_name
    asset.rights_status = "own"
    asset.may_use_directly = True
    asset.license_note = license_note.strip()
    asset.license_document_ref = license_document_ref
    asset.rights_confirmed_by = actor
    asset.rights_confirmed_at = datetime.now(timezone.utc)
    asset.storage_status = "processing"
    asset.original_exif_preserved = True
    asset.processed_exif_stripped = True
    asset.caption = caption
    asset.status = "approved"

    await db.flush()
    asset.storage_key_original = await asyncio.to_thread(store_uploaded_final_asset_original_file, asset, file_bytes)
    asset.storage_key_thumbnail = None
    asset.storage_key_processed = None
    asset.metadata_storage_key = None
    asset.processed_exif_stripped = False
    asset.storage_status = "processing"

    target_size = f"{settings.default_target_width}x{settings.default_target_height}"
    process_job = await get_or_create_active_job(
        db,
        job_type="process_final_asset",
        payload={"asset_id": asset.id, "original_filename": original_filename},
        idempotency_key=f"process_final_asset:{asset.id}:{asset.storage_key_original}:{target_size}",
    )

    db.add(
        AuditEvent(
            actor=actor,
            entity_type="final_asset",
            entity_id=asset.id,
            action="final_asset.uploaded",
            before=previous,
            after={
                "mistake_id": mistake.id,
                "side": side,
                "source_type": asset.source_type,
                "rights_status": asset.rights_status,
                "license_note": asset.license_note,
                "storage_key_original": asset.storage_key_original,
                "storage_key_thumbnail": asset.storage_key_thumbnail,
                "storage_key_processed": asset.storage_key_processed,
                "metadata_storage_key": asset.metadata_storage_key,
                "process_job_id": process_job.id,
            },
            comment=license_note.strip(),
        )
    )

    await _enqueue_cleanup_for_replaced_keys(
        db,
        asset=asset,
        previous_keys=previous_storage_keys,
        reason="replace_final_asset",
    )

    await db.commit()
    await db.refresh(asset)
    return asset
