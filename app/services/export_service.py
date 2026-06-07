import asyncio
import csv
import json
import os
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.mistake import Mistake
from app.models.video import Video
from app.services.final_asset_service import final_asset_health
from app.services.image_processing_limiter import image_processing_slot
from app.services.storage_service import (
    StorageDownloadError,
    _normalize_storage_key,
    _path_for_storage_key,
    download_final_asset_original,
    store_final_asset_derivative_files,
)


SCHEMA_VERSION = "1.0"
EXPORTABLE_RIGHTS_STATUSES = {"manual_licensed", "own", "free_to_use"}
CSV_FIELDS = [
    "schema_version",
    "video_id",
    "mistake_id",
    "order_index",
    "side",
    "asset_id",
    "file",
    "time_start",
    "time_end",
    "title",
    "source_type",
    "rights_status",
    "source_url",
    "license_note",
    "license_document_ref",
    "author_name",
    "original_exif_preserved",
    "processed_exif_stripped",
]


def _slugify(value: str, fallback: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")
    return slug or fallback


def safe_video_export_slug(video: Video) -> str:
    return _slugify(video.slug or "", f"video-{video.id}")


def _export_path_for_name(name: str) -> Path:
    root = settings.export_root.resolve()
    path = (root / name).resolve()
    if os.path.commonpath([str(root), str(path)]) != str(root):
        raise ValueError(f"export path escapes export root: {name}")
    return path


def _source_path(storage_key: str) -> Path:
    normalized_key = _normalize_storage_key(storage_key)
    if not normalized_key:
        raise ValueError(f"Invalid storage key for export: {storage_key}")
    return _path_for_storage_key(settings.storage_root, normalized_key)


def _build_export_warnings(mistakes: list[Mistake], assets: list[FinalAsset]) -> list[dict]:
    sides_by_mistake: dict[int, set[str]] = {mistake.id: set() for mistake in mistakes}
    for asset in assets:
        if asset.mistake_id in sides_by_mistake:
            sides_by_mistake[asset.mistake_id].add(asset.side)

    warnings: list[dict] = []
    mistakes_by_id = {mistake.id: mistake for mistake in mistakes}
    for mistake_id, sides in sides_by_mistake.items():
        mistake = mistakes_by_id[mistake_id]
        if not sides:
            warnings.append({
                "code": "missing_final_assets",
                "mistake_id": mistake.id,
                "order_index": mistake.order_index,
                "message": "Mistake has no exportable final assets",
            })
            continue
        for side in ("wrong", "right"):
            if side not in sides:
                warnings.append({
                    "code": "missing_side_final_asset",
                    "mistake_id": mistake.id,
                    "order_index": mistake.order_index,
                    "side": side,
                    "message": f"Mistake is missing an exportable {side} final asset",
                })
    return warnings


def _asset_is_exportable(asset: FinalAsset) -> bool:
    return (
        asset.status in {"approved", "exported"}
        and asset.may_use_directly is True
        and asset.rights_status in EXPORTABLE_RIGHTS_STATUSES
    )


def _storage_key_file_available(storage_key: str | None) -> bool:
    if not storage_key:
        return False
    normalized_key = _normalize_storage_key(storage_key)
    if not normalized_key:
        return False
    try:
        file_path = _path_for_storage_key(settings.storage_root, normalized_key)
    except ValueError:
        return False
    return file_path.exists() and file_path.is_file() and file_path.stat().st_size > 0


def _final_asset_export_storage_state(asset: FinalAsset) -> dict:
    warnings: list[str] = []
    if asset.storage_status not in {"ok", "exported"}:
        warnings.append("storage_status_not_ok")
        return {"ready": False, "autoheal_derivatives": False, "warnings": warnings}

    if asset.storage_key_processed:
        if _storage_key_file_available(asset.storage_key_processed):
            return {"ready": True, "autoheal_derivatives": False, "warnings": warnings}
        warnings.append("processed_file_missing")
        return {"ready": False, "autoheal_derivatives": False, "warnings": warnings}

    warnings.append("missing_processed_asset")
    if _storage_key_file_available(asset.storage_key_original):
        return {"ready": True, "autoheal_derivatives": True, "warnings": warnings}

    warnings.append("original_file_missing")
    return {"ready": False, "autoheal_derivatives": False, "warnings": warnings}


async def build_video_export_readiness(video_id: int, db: AsyncSession) -> dict:
    result_video = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)))
    video = result_video.scalars().first()
    if not video:
        raise ValueError(f"Video {video_id} not found")

    result_mistakes = await db.execute(
        select(Mistake)
        .where(Mistake.video_id == video_id, Mistake.deleted_at.is_(None))
        .order_by(Mistake.order_index, Mistake.id)
    )
    mistakes = result_mistakes.scalars().all()
    warnings: list[dict] = []
    if not mistakes:
        warnings.append({"code": "no_active_mistakes", "message": "Video has no active mistakes"})
        return {
            "video_id": video.id,
            "can_export": False,
            "complete": False,
            "active_mistake_count": 0,
            "ready_mistake_count": 0,
            "exportable_asset_count": 0,
            "ready_asset_count": 0,
            "warnings": warnings,
        }

    mistake_ids = [mistake.id for mistake in mistakes]
    result_assets = await db.execute(
        select(FinalAsset)
        .where(FinalAsset.video_id == video_id)
        .where(FinalAsset.mistake_id.in_(mistake_ids))
        .where(FinalAsset.status.in_(["approved", "exported"]))
        .order_by(FinalAsset.mistake_id, FinalAsset.side, FinalAsset.id)
    )
    assets = result_assets.scalars().all()
    assets_by_mistake_side: dict[tuple[int, str], list[FinalAsset]] = {}
    exportable_assets: list[FinalAsset] = []
    ready_assets: list[FinalAsset] = []

    for asset in assets:
        assets_by_mistake_side.setdefault((asset.mistake_id, asset.side), []).append(asset)
        health = final_asset_health(asset)
        if not _asset_is_exportable(asset):
            warnings.append({
                "code": "final_asset_not_exportable",
                "asset_id": asset.id,
                "mistake_id": asset.mistake_id,
                "side": asset.side,
                "rights_status": asset.rights_status,
                "may_use_directly": asset.may_use_directly,
                "message": "Final asset is selected but is not exportable by rights/status policy",
            })
            continue
        exportable_assets.append(asset)
        export_state = _final_asset_export_storage_state(asset)
        if export_state["ready"]:
            ready_assets.append(asset)
            if export_state["autoheal_derivatives"]:
                warnings.append({
                    "code": "final_asset_needs_derivatives",
                    "asset_id": asset.id,
                    "mistake_id": asset.mistake_id,
                    "side": asset.side,
                    "health_warnings": export_state["warnings"],
                    "message": "Final asset is exportable; export will generate missing processed/thumbnail derivatives from original",
                })
            elif health["warnings"]:
                warnings.append({
                    "code": "final_asset_health_warning",
                    "asset_id": asset.id,
                    "mistake_id": asset.mistake_id,
                    "side": asset.side,
                    "health_warnings": health["warnings"],
                    "message": "Final asset can export but has non-blocking storage/UI health warnings",
                })
        else:
            warnings.append({
                "code": "final_asset_not_ready",
                "asset_id": asset.id,
                "mistake_id": asset.mistake_id,
                "side": asset.side,
                "health_warnings": sorted(set(health["warnings"] + export_state["warnings"])),
                "message": "Final asset is exportable by rights but storage is not ready",
            })

    ready_sides_by_mistake: dict[int, set[str]] = {mistake.id: set() for mistake in mistakes}
    for asset in ready_assets:
        if asset.mistake_id in ready_sides_by_mistake:
            ready_sides_by_mistake[asset.mistake_id].add(asset.side)

    for mistake in mistakes:
        sides = ready_sides_by_mistake[mistake.id]
        if not sides:
            warnings.append({
                "code": "missing_final_assets",
                "mistake_id": mistake.id,
                "order_index": mistake.order_index,
                "message": "Mistake has no export-ready final assets",
            })
            continue
        for side in ("wrong", "right"):
            if side not in sides:
                warnings.append({
                    "code": "missing_side_final_asset",
                    "mistake_id": mistake.id,
                    "order_index": mistake.order_index,
                    "side": side,
                    "message": f"Mistake is missing an export-ready {side} final asset",
                })

    if not ready_assets:
        warnings.append({"code": "no_export_ready_assets", "message": "Video has no export-ready final assets"})

    required_side_count = len(mistakes) * 2
    ready_side_count = sum(len(sides & {"wrong", "right"}) for sides in ready_sides_by_mistake.values())
    ready_mistake_count = sum(1 for sides in ready_sides_by_mistake.values() if {"wrong", "right"}.issubset(sides))
    return {
        "video_id": video.id,
        "can_export": bool(ready_assets),
        "complete": ready_side_count == required_side_count,
        "active_mistake_count": len(mistakes),
        "ready_mistake_count": ready_mistake_count,
        "exportable_asset_count": len(exportable_assets),
        "ready_asset_count": len(ready_assets),
        "warnings": warnings,
    }


def _asset_payload(asset: FinalAsset, file_path: str) -> dict:
    return {
        "id": asset.id,
        "file": file_path,
        "source_type": asset.source_type,
        "rights_status": asset.rights_status,
        "source_url": asset.source_url,
        "license_note": asset.license_note,
        "license_document_ref": asset.license_document_ref,
        "author_name": asset.author_name,
        "original_exif_preserved": asset.original_exif_preserved,
        "processed_exif_stripped": asset.processed_exif_stripped,
    }


def _csv_row(video: Video, mistake: Mistake, asset: FinalAsset, file_path: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "video_id": video.id,
        "mistake_id": mistake.id,
        "order_index": mistake.order_index,
        "side": asset.side,
        "asset_id": asset.id,
        "file": file_path,
        "time_start": mistake.time_start,
        "time_end": mistake.time_end,
        "title": mistake.title,
        "source_type": asset.source_type,
        "rights_status": asset.rights_status,
        "source_url": asset.source_url,
        "license_note": asset.license_note,
        "license_document_ref": asset.license_document_ref,
        "author_name": asset.author_name,
        "original_exif_preserved": asset.original_exif_preserved,
        "processed_exif_stripped": asset.processed_exif_stripped,
    }


async def export_video_manifest(video_id: int, db: AsyncSession):
    """Build a human-readable export package for final video assets."""
    result_video = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)))
    video = result_video.scalars().first()
    if not video:
        raise ValueError(f"Video {video_id} not found")

    result_mistakes = await db.execute(
        select(Mistake)
        .where(Mistake.video_id == video_id, Mistake.deleted_at.is_(None))
        .order_by(Mistake.order_index, Mistake.id)
    )
    active_mistakes = result_mistakes.scalars().all()
    if not active_mistakes:
        raise ValueError(f"Video {video_id} has no active mistakes to export")

    active_mistake_ids = [mistake.id for mistake in active_mistakes]
    result_assets = await db.execute(
        select(FinalAsset)
        .where(FinalAsset.video_id == video_id)
        .where(FinalAsset.mistake_id.in_(active_mistake_ids))
        .where(FinalAsset.status.in_(["approved", "exported"]))
        .where(FinalAsset.may_use_directly.is_(True))
        .where(FinalAsset.rights_status.in_(EXPORTABLE_RIGHTS_STATUSES))
        .options(selectinload(FinalAsset.mistake))
        .order_by(FinalAsset.sort_order, FinalAsset.id)
    )
    assets = result_assets.scalars().all()
    if not assets:
        raise ValueError(f"Video {video_id} has no exportable final assets")

    warnings = _build_export_warnings(active_mistakes, assets)

    exported_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    export_stamp = exported_at.replace("-", "").replace(":", "").replace("T", "_").replace("Z", "")
    settings.export_root.mkdir(parents=True, exist_ok=True)
    safe_slug = safe_video_export_slug(video)
    export_dir = _export_path_for_name(f"{safe_slug}_{export_stamp}")
    suffix_counter = 1
    while export_dir.exists():
        export_dir = _export_path_for_name(f"{safe_slug}_{export_stamp}_{suffix_counter}")
        suffix_counter += 1
    temp_export_dir = _export_path_for_name(f".tmp_{export_dir.name}_{uuid4().hex[:8]}")
    temp_manifest_path = temp_export_dir / "manifest.json"
    temp_csv_path = temp_export_dir / "assets.csv"
    manifest_path = export_dir / "manifest.json"
    csv_path = export_dir / "assets.csv"
    finalized_export_dir = False

    try:
        mistakes_dir = temp_export_dir / "mistakes"
        os.makedirs(mistakes_dir, exist_ok=False)

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "exported_at": exported_at,
            "video": {
                "id": video.id,
                "slug": video.slug,
                "title": video.title,
            },
            "warnings": warnings,
            "mistakes": [],
        }
        rows = []
        exported_asset_ids = []
        mistakes_by_id = {}

        for asset in assets:
            if asset.storage_status != "ok":
                try:
                    async with image_processing_slot(db):
                        asset = await download_final_asset_original(asset.id, db)
                except StorageDownloadError as e:
                    raise ValueError(f"Final asset {asset.id} is not export-ready: {e}") from e

            if asset.storage_status != "ok":
                raise ValueError(f"Final asset {asset.id} is not export-ready")

            if not asset.storage_key_processed:
                if not asset.storage_key_original:
                    try:
                        async with image_processing_slot(db):
                            asset = await download_final_asset_original(asset.id, db)
                    except StorageDownloadError as e:
                        raise ValueError(f"Final asset {asset.id} processed file is not export-ready: {e}") from e
                try:
                    async with image_processing_slot(db):
                        derivative_keys = await asyncio.to_thread(store_final_asset_derivative_files, asset, allow_missing_original=False)
                except StorageDownloadError as e:
                    storage_key = asset.storage_key_original
                    path_value = None
                    if storage_key:
                        normalized_key = _normalize_storage_key(storage_key)
                        if normalized_key:
                            path_value = str(_path_for_storage_key(settings.storage_root, normalized_key))
                    db.add(
                        AuditEvent(
                            actor="system",
                            entity_type="final_asset",
                            entity_id=asset.id,
                            action="storage.missing_file_detected",
                            before=None,
                            after={
                                "video_id": video.id,
                                "asset_id": asset.id,
                                "storage_key": storage_key,
                                "path": path_value,
                                "during": "export",
                            },
                            comment="Export detected missing final asset file",
                        )
                    )
                    await db.commit()
                    raise ValueError(f"Final asset {asset.id} processed file is not export-ready: {e}") from e
                asset.storage_key_thumbnail = derivative_keys["storage_key_thumbnail"]
                asset.storage_key_processed = derivative_keys["storage_key_processed"]
                asset.metadata_storage_key = derivative_keys["metadata_storage_key"]
                asset.original_exif_preserved = True
                asset.processed_exif_stripped = True
                asset.storage_status = "ok"

            export_storage_key = asset.storage_key_processed
            if not export_storage_key:
                raise ValueError(f"Final asset {asset.id} processed file is not export-ready")

            mistake = asset.mistake
            if not mistake:
                result_mistake = await db.execute(select(Mistake).where(Mistake.id == asset.mistake_id))
                mistake = result_mistake.scalars().first()
            if not mistake:
                raise ValueError(f"Mistake {asset.mistake_id} not found for final asset {asset.id}")

            source_path = _source_path(export_storage_key)
            if not source_path.exists():
                db.add(
                    AuditEvent(
                        actor="system",
                        entity_type="final_asset",
                        entity_id=asset.id,
                        action="storage.missing_file_detected",
                        before=None,
                        after={
                            "video_id": video.id,
                            "asset_id": asset.id,
                            "storage_key": export_storage_key,
                            "path": str(source_path),
                            "during": "export",
                        },
                        comment="Export detected missing final asset file",
                    )
                )
                await db.commit()
                raise ValueError(f"Final asset {asset.id} file is missing: {source_path}")

            mistake_slug = _slugify(mistake.short_title or mistake.title, f"mistake-{mistake.id}")
            relative_dir = Path("mistakes") / f"{mistake.order_index:02d}_{mistake_slug}" / asset.side
            asset_dir = temp_export_dir / relative_dir
            os.makedirs(asset_dir, exist_ok=True)

            suffix = source_path.suffix or ".jpg"
            relative_file = relative_dir / f"{asset.sort_order + 1:03d}_1920x1080{suffix}"
            shutil.copy2(source_path, temp_export_dir / relative_file)

            entry = mistakes_by_id.get(mistake.id)
            if not entry:
                entry = {
                    "id": mistake.id,
                    "order_index": mistake.order_index,
                    "title": mistake.title,
                    "time_start": mistake.time_start,
                    "time_end": mistake.time_end,
                    "wrong_assets": [],
                    "right_assets": [],
                }
                mistakes_by_id[mistake.id] = entry
                manifest["mistakes"].append(entry)

            file_value = relative_file.as_posix()
            target_key = "right_assets" if asset.side == "right" else "wrong_assets"
            entry[target_key].append(_asset_payload(asset, file_value))
            rows.append(_csv_row(video, mistake, asset, file_value))
            exported_asset_ids.append(asset.id)

        manifest["mistakes"].sort(key=lambda item: item["order_index"])
        with open(temp_manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)

        with open(temp_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)

        temp_export_dir.rename(export_dir)
        finalized_export_dir = True

        if exported_asset_ids:
            result_exported_assets = await db.execute(select(FinalAsset).where(FinalAsset.id.in_(exported_asset_ids)).with_for_update())
            for exported_asset in result_exported_assets.scalars().all():
                exported_asset.status = "exported"

        db.add(
            AuditEvent(
                actor="system",
                entity_type="video",
                entity_id=video.id,
                action="video.exported",
                before=None,
                after={
                    "schema_version": SCHEMA_VERSION,
                    "exported_at": exported_at,
                    "export_dir": str(export_dir),
                    "manifest_path": str(manifest_path),
                    "assets_csv_path": str(csv_path),
                    "asset_ids": exported_asset_ids,
                    "asset_count": len(exported_asset_ids),
                    "warnings": warnings,
                    "warning_count": len(warnings),
                },
                comment="Video export package created",
            )
        )
        await db.commit()
    except Exception:
        if temp_export_dir.exists():
            shutil.rmtree(temp_export_dir, ignore_errors=True)
        if finalized_export_dir and export_dir.exists():
            shutil.rmtree(export_dir, ignore_errors=True)
        raise

    return str(manifest_path)
