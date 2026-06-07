from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import os
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import settings
from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.candidate import ImageCandidate

logger = logging.getLogger(__name__)

ALLOWED_IMAGE_MIME_TYPES = {
    "image/jpeg",
    "image/png",
    "image/webp",
    "image/gif",
}
IMAGE_FORMAT_EXTENSIONS = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
}
MAX_REDIRECTS = 3


class StorageDownloadError(ValueError):
    pass


def _max_download_bytes() -> int:
    return settings.max_download_mb * 1024 * 1024


def _validate_url_scheme(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise StorageDownloadError("Only http/https image URLs are allowed")
    if not parsed.hostname:
        raise StorageDownloadError("Image URL must include a hostname")
    return parsed.hostname.rstrip(".").lower()


def _configured_allowed_image_domains() -> set[str]:
    raw_domains = settings.allowed_image_domains or ""
    domains = set()
    for item in raw_domains.split(","):
        domain = item.strip().rstrip(".").lower()
        if domain:
            domains.add(domain)
    return domains


def _host_matches_domain(hostname: str, domain: str) -> bool:
    return hostname == domain or hostname.endswith(f".{domain}")


def _validate_allowed_image_domain(hostname: str) -> None:
    allowed_domains = _configured_allowed_image_domains()
    if not allowed_domains:
        if settings.app_env.lower() != "local":
            raise StorageDownloadError("ALLOWED_IMAGE_DOMAINS must be configured outside local environment")
        return
    if any(_host_matches_domain(hostname, domain) for domain in allowed_domains):
        return
    raise StorageDownloadError(f"Image host is not in allowed image domains: {hostname}")


def _is_blocked_ip(raw_ip: str) -> bool:
    ip = ipaddress.ip_address(raw_ip)
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
        or not ip.is_global
    )


async def _validate_public_url(url: str) -> None:
    hostname = _validate_url_scheme(url)
    _validate_allowed_image_domain(hostname)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(hostname, None, type=0, proto=0)
    except OSError as e:
        raise StorageDownloadError(f"Could not resolve image host: {hostname}") from e

    ips = {info[4][0] for info in infos}
    if not ips:
        raise StorageDownloadError(f"Image host resolved to no addresses: {hostname}")
    blocked = [ip for ip in ips if _is_blocked_ip(ip)]
    if blocked:
        raise StorageDownloadError(f"Image host resolves to a blocked address: {hostname}")


def _check_content_length(headers: httpx.Headers) -> None:
    content_length = headers.get("content-length")
    if not content_length:
        return
    try:
        size = int(content_length)
    except ValueError as e:
        raise StorageDownloadError("Invalid Content-Length header") from e
    if size > _max_download_bytes():
        raise StorageDownloadError("Image download exceeds configured size limit")


def _check_content_type(headers: httpx.Headers) -> str:
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type not in ALLOWED_IMAGE_MIME_TYPES:
        raise StorageDownloadError(f"Unsupported image content type: {content_type or 'missing'}")
    return content_type


def _verify_image(data: bytes) -> str:
    try:
        with Image.open(BytesIO(data)) as img:
            image_format = img.format
            width, height = img.size
            if width * height > settings.max_image_pixels:
                raise StorageDownloadError("Image exceeds configured pixel limit")
            img.verify()
    except (UnidentifiedImageError, OSError) as e:
        raise StorageDownloadError("Downloaded file is not a valid raster image") from e

    extension = IMAGE_FORMAT_EXTENSIONS.get(image_format or "")
    if not extension:
        raise StorageDownloadError(f"Unsupported image format: {image_format or 'unknown'}")
    return extension


async def _fetch_image_bytes(url: str) -> bytes:
    current_url = url
    timeout = httpx.Timeout(15.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout, trust_env=False, follow_redirects=False) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await _validate_public_url(current_url)
            try:
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= MAX_REDIRECTS:
                            raise StorageDownloadError("Too many image redirects")
                        location = response.headers.get("location")
                        if not location:
                            raise StorageDownloadError("Redirect response missing Location header")
                        current_url = urljoin(current_url, location)
                        continue

                    response.raise_for_status()
                    _check_content_length(response.headers)
                    _check_content_type(response.headers)

                    chunks: list[bytes] = []
                    downloaded = 0
                    async for chunk in response.aiter_bytes():
                        downloaded += len(chunk)
                        if downloaded > _max_download_bytes():
                            raise StorageDownloadError("Image download exceeds configured size limit")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as e:
                raise StorageDownloadError(f"Image download failed: {e}") from e

    raise StorageDownloadError("Image download failed")


def _final_asset_storage_key(asset: FinalAsset, extension: str) -> str:
    candidate_part = asset.candidate_id if asset.candidate_id is not None else "manual"
    url_hash = hashlib.sha256((asset.source_url or str(asset.id)).encode("utf-8")).hexdigest()[:12]
    return (
        f"final-assets/video_{asset.video_id}/mistake_{asset.mistake_id}/"
        f"{asset.side}/asset_{asset.id}_candidate_{candidate_part}_{url_hash}.{extension}"
    )


def _write_storage_key(storage_key: str, data: bytes) -> None:
    normalized_key = _normalize_storage_key(storage_key)
    if not normalized_key:
        raise StorageDownloadError("Invalid storage key")
    destination = _path_for_storage_key(settings.storage_root, normalized_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    with open(temp_path, "wb") as f:
        f.write(data)
    os.replace(temp_path, destination)


def _max_upload_bytes() -> int:
    return settings.max_upload_mb * 1024 * 1024


def _check_upload_size(data: bytes) -> None:
    if len(data) > _max_upload_bytes():
        raise StorageDownloadError("Uploaded image exceeds configured size limit")


def _contained_jpeg_bytes(data: bytes, size: tuple[int, int]) -> bytes:
    with Image.open(BytesIO(data)) as source:
        image = ImageOps.exif_transpose(source)
        if image.mode not in {"RGB", "RGBA"}:
            image = image.convert("RGBA")
        fitted = ImageOps.contain(image, size, method=Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", size, (255, 255, 255))
        left = (size[0] - fitted.width) // 2
        top = (size[1] - fitted.height) // 2
        if fitted.mode == "RGBA":
            canvas.paste(fitted, (left, top), fitted)
        else:
            canvas.paste(fitted.convert("RGB"), (left, top))
        output = BytesIO()
        canvas.save(output, format="JPEG", quality=92, optimize=True)
        return output.getvalue()


def _uploaded_final_asset_prefix(asset: FinalAsset) -> str:
    return f"projects/{asset.video_id}/final_assets/{asset.id}"


def store_uploaded_final_asset_original_file(asset: FinalAsset, data: bytes) -> str:
    _check_upload_size(data)
    extension = _verify_image(data)
    original_key = f"{_uploaded_final_asset_prefix(asset)}/original.{extension}"
    _write_storage_key(original_key, data)
    return original_key


def store_uploaded_final_asset_files(asset: FinalAsset, data: bytes, original_filename: str | None = None) -> dict[str, str]:
    original_key = store_uploaded_final_asset_original_file(asset, data)
    asset.storage_key_original = original_key
    keys = store_final_asset_derivative_files(asset, original_filename=original_filename)
    return {"storage_key_original": original_key, **keys}


def store_final_asset_derivative_files(asset: FinalAsset, allow_missing_original: bool = False, original_filename: str | None = None) -> dict[str, str]:
    if not asset.storage_key_original:
        if allow_missing_original:
            return {}
        raise StorageDownloadError(f"FinalAsset {asset.id} has no original storage file")

    storage_key = _normalize_storage_key(asset.storage_key_original)
    if not storage_key:
        raise StorageDownloadError(f"FinalAsset {asset.id} has invalid original storage key")
    original_path = _path_for_storage_key(settings.storage_root, storage_key)
    if not original_path.exists() or not original_path.is_file():
        if allow_missing_original:
            return {}
        raise StorageDownloadError(f"FinalAsset {asset.id} original file is missing")

    data = original_path.read_bytes()
    _verify_image(data)
    prefix = _uploaded_final_asset_prefix(asset)
    thumbnail_key = asset.storage_key_thumbnail or f"{prefix}/thumb.jpg"
    processed_key = asset.storage_key_processed or f"{prefix}/processed_1920x1080.jpg"
    metadata_key = asset.metadata_storage_key or f"{prefix}/metadata.json"

    if not asset.storage_key_thumbnail:
        _write_storage_key(thumbnail_key, _contained_jpeg_bytes(data, (400, 225)))
    if not asset.storage_key_processed:
        _write_storage_key(processed_key, _contained_jpeg_bytes(data, (settings.default_target_width, settings.default_target_height)))

    metadata = {
        "asset_id": asset.id,
        "source_type": asset.source_type,
        "rights_status": asset.rights_status,
        "source_url": asset.source_url,
        "license_note": asset.license_note,
        "license_document_ref": asset.license_document_ref,
        "author_name": asset.author_name,
        "original_filename": original_filename,
        "original_exif_preserved": True,
        "processed_exif_stripped": True,
    }
    _write_storage_key(metadata_key, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))

    return {
        "storage_key_thumbnail": thumbnail_key,
        "storage_key_processed": processed_key,
        "metadata_storage_key": metadata_key,
    }


async def store_final_asset_original(asset: FinalAsset) -> str:
    if not asset.source_url:
        raise StorageDownloadError(f"FinalAsset {asset.id} has no source_url")
    data = await _fetch_image_bytes(asset.source_url)
    extension = _verify_image(data)
    storage_key = _final_asset_storage_key(asset, extension)
    _write_storage_key(storage_key, data)
    return storage_key


async def download_final_asset_original(asset_id: int, db: AsyncSession) -> FinalAsset:
    result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id))
    asset = result.scalars().first()
    if not asset:
        raise StorageDownloadError(f"FinalAsset {asset_id} not found")
    if not asset.source_url:
        raise StorageDownloadError(f"FinalAsset {asset_id} has no source_url")

    try:
        storage_key = await store_final_asset_original(asset)
    except StorageDownloadError:
        result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id).with_for_update())
        failed_asset = result.scalars().first()
        if failed_asset:
            failed_asset.storage_status = "failed"
            if failed_asset.candidate_id:
                result = await db.execute(
                    select(ImageCandidate).where(ImageCandidate.id == failed_asset.candidate_id).with_for_update()
                )
                candidate = result.scalars().first()
                if candidate:
                    candidate.storage_status = "failed"
            await db.commit()
        raise

    result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id).with_for_update())
    asset = result.scalars().first()
    if not asset:
        raise StorageDownloadError(f"FinalAsset {asset_id} disappeared during download")
    asset.storage_key_original = storage_key
    asset.storage_status = "ok"

    if asset.candidate_id:
        result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == asset.candidate_id).with_for_update())
        candidate = result.scalars().first()
        if candidate:
            candidate.storage_key_original = storage_key
            candidate.storage_status = "ok"

    await db.commit()
    await db.refresh(asset)
    logger.info("Downloaded final asset %s to %s", asset_id, storage_key)
    return asset


async def download_candidate_image(candidate_id: int, image_url: str, db: AsyncSession):
    """Legacy helper retained for older code paths; stores a validated candidate image."""
    data = await _fetch_image_bytes(image_url)
    extension = _verify_image(data)
    url_hash = hashlib.sha256(image_url.encode("utf-8")).hexdigest()[:12]
    storage_key = f"candidates/{candidate_id}_{url_hash}.{extension}"
    _write_storage_key(storage_key, data)

    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if candidate:
        candidate.storage_key_original = storage_key
        candidate.storage_status = "ok"
        await db.commit()

    logger.info("Downloaded candidate %s to %s", candidate_id, storage_key)
    return storage_key


def _normalize_storage_key(raw_key: str | None) -> str | None:
    if not raw_key:
        return None
    storage_key = str(raw_key).replace("\\", "/").strip()
    if "//" in storage_key:
        return None
    path = Path(storage_key)
    if path.is_absolute() or any(part in {"..", ""} for part in path.parts):
        return None
    return path.as_posix()


def _path_for_storage_key(storage_root: Path, storage_key: str) -> Path:
    root_path = storage_root.resolve()
    candidate_path = (root_path / storage_key).resolve()
    if os.path.commonpath([str(root_path), str(candidate_path)]) != str(root_path):
        raise ValueError(f"storage key escapes storage root: {storage_key}")
    return candidate_path


def _relative_storage_key(storage_root: Path, file_path: Path) -> str:
    return file_path.resolve().relative_to(storage_root.resolve()).as_posix()


async def _collect_storage_references(db: AsyncSession) -> tuple[dict[str, list[dict]], list[dict]]:
    references: dict[str, list[dict]] = {}
    invalid_references: list[dict] = []

    def add_reference(entity_type: str, entity_id: int, field: str, raw_key: str | None) -> None:
        if not raw_key:
            return
        storage_key = _normalize_storage_key(raw_key)
        ref = {"entity_type": entity_type, "entity_id": entity_id, "field": field, "storage_key": raw_key}
        if not storage_key:
            invalid_references.append(ref)
            return
        references.setdefault(storage_key, []).append({**ref, "storage_key": storage_key})

    result = await db.execute(select(ImageCandidate))
    for candidate in result.scalars().all():
        add_reference("image_candidate", candidate.id, "storage_key_thumbnail", candidate.storage_key_thumbnail)
        add_reference("image_candidate", candidate.id, "storage_key_original", candidate.storage_key_original)
        add_reference("image_candidate", candidate.id, "storage_key_processed", candidate.storage_key_processed)

    result = await db.execute(select(FinalAsset))
    for asset in result.scalars().all():
        add_reference("final_asset", asset.id, "storage_key_original", asset.storage_key_original)
        add_reference("final_asset", asset.id, "storage_key_thumbnail", asset.storage_key_thumbnail)
        add_reference("final_asset", asset.id, "storage_key_processed", asset.storage_key_processed)
        add_reference("final_asset", asset.id, "metadata_storage_key", asset.metadata_storage_key)

    return references, invalid_references


STORAGE_KEY_FIELDS = (
    "storage_key_original",
    "storage_key_thumbnail",
    "storage_key_processed",
    "metadata_storage_key",
)


def _parse_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"Invalid boolean value: {value!r}")


def _extract_target_storage_keys(payload: dict | None) -> tuple[list[str], list[dict]]:
    payload = payload or {}
    raw_keys: list[str] = []
    for key in payload.get("old_storage_keys") or []:
        raw_keys.append(key)
    targets = payload.get("targets") or {}
    for items in targets.values():
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            for field in STORAGE_KEY_FIELDS:
                value = item.get(field)
                if value:
                    raw_keys.append(value)

    seen: set[str] = set()
    normalized: list[str] = []
    invalid_target_keys: list[dict] = []
    for raw_key in raw_keys:
        storage_key = _normalize_storage_key(raw_key)
        if not storage_key:
            invalid_target_keys.append({"storage_key": raw_key})
            continue
        if storage_key in seen:
            continue
        seen.add(storage_key)
        normalized.append(storage_key)
    return normalized, invalid_target_keys


async def cleanup_storage_targets(payload: dict, db: AsyncSession):
    """Delete only explicitly captured orphan storage keys from a cleanup job payload."""
    dry_run = _parse_bool(payload.get("dry_run", True))
    target_keys, invalid_target_keys = _extract_target_storage_keys(payload)
    logger.info("Running targeted storage cleanup (dry_run=%s, targets=%s)", dry_run, len(target_keys))
    storage_root = settings.storage_root
    storage_root.mkdir(parents=True, exist_ok=True)

    db.add(
        AuditEvent(
            actor="system",
            entity_type="storage",
            entity_id=0,
            action="storage.cleanup_started",
            before=None,
            after={
                "dry_run": dry_run,
                "storage_root": str(storage_root),
                "mode": "targeted",
                "target_storage_keys": target_keys,
                "invalid_target_keys": invalid_target_keys,
                "reason": payload.get("reason"),
            },
            comment="Targeted storage cleanup started",
        )
    )
    await db.commit()

    references, invalid_references = await _collect_storage_references(db)
    deleted_files_count = 0
    freed_bytes = 0
    target_files = []
    skipped_referenced_files = []
    missing_target_files = []

    for storage_key in target_keys:
        if storage_key in references:
            skipped_referenced_files.append({"storage_key": storage_key, "references": references[storage_key]})
            continue
        try:
            file_path = _path_for_storage_key(storage_root, storage_key)
        except ValueError:
            continue
        if not file_path.exists() or not file_path.is_file():
            missing_target_files.append({"storage_key": storage_key})
            continue
        size = file_path.stat().st_size
        target_files.append({"storage_key": storage_key, "bytes": size})
        if not dry_run:
            file_path.unlink(missing_ok=True)
            deleted_files_count += 1
            freed_bytes += size

    missing_files = []
    for storage_key in sorted(references):
        try:
            expected_path = _path_for_storage_key(storage_root, storage_key)
        except ValueError:
            continue
        if not expected_path.exists():
            for ref in references[storage_key]:
                missing_files.append(ref)

    result = {
        "dry_run": dry_run,
        "storage_root": str(storage_root),
        "mode": "targeted",
        "target_storage_keys_count": len(target_keys),
        "target_storage_keys": target_keys,
        "invalid_target_keys_count": len(invalid_target_keys),
        "invalid_target_keys": invalid_target_keys,
        "orphan_files_count": len(target_files),
        "orphan_bytes": sum(item["bytes"] for item in target_files),
        "orphan_files": target_files,
        "missing_target_files_count": len(missing_target_files),
        "missing_target_files": missing_target_files,
        "skipped_referenced_files_count": len(skipped_referenced_files),
        "skipped_referenced_files": skipped_referenced_files,
        "missing_files_count": len(missing_files),
        "missing_files": missing_files,
        "invalid_references_count": len(invalid_references),
        "invalid_references": invalid_references,
        "deleted_files_count": deleted_files_count,
        "freed_bytes": freed_bytes,
    }

    if missing_files:
        db.add(
            AuditEvent(
                actor="system",
                entity_type="storage",
                entity_id=0,
                action="storage.missing_file_detected",
                before=None,
                after={"missing_files": missing_files, "missing_files_count": len(missing_files)},
                comment="Targeted storage cleanup detected missing referenced files",
            )
        )
    db.add(
        AuditEvent(
            actor="system",
            entity_type="storage",
            entity_id=0,
            action="storage.cleanup_finished",
            before=None,
            after=result,
            comment="Targeted storage cleanup finished",
        )
    )
    await db.commit()
    return result


async def cleanup_storage(dry_run: bool, db: AsyncSession):
    """Compare local storage files with DB storage keys and optionally delete orphans."""
    logger.info("Running storage cleanup (dry_run=%s)", dry_run)
    storage_root = settings.storage_root
    storage_root.mkdir(parents=True, exist_ok=True)

    db.add(
        AuditEvent(
            actor="system",
            entity_type="storage",
            entity_id=0,
            action="storage.cleanup_started",
            before=None,
            after={"dry_run": dry_run, "storage_root": str(storage_root)},
            comment="Storage cleanup started",
        )
    )
    await db.commit()

    references, invalid_references = await _collect_storage_references(db)
    referenced_keys = set(references)

    files_on_disk: dict[str, Path] = {}
    for file_path in storage_root.rglob("*"):
        if file_path.is_file():
            files_on_disk[_relative_storage_key(storage_root, file_path)] = file_path

    orphan_files = []
    orphan_bytes = 0
    deleted_files_count = 0
    freed_bytes = 0
    for storage_key, file_path in sorted(files_on_disk.items()):
        if storage_key in referenced_keys:
            continue
        size = file_path.stat().st_size
        orphan_files.append({"storage_key": storage_key, "bytes": size})
        orphan_bytes += size
        if not dry_run:
            file_path.unlink(missing_ok=True)
            deleted_files_count += 1
            freed_bytes += size

    missing_files = []
    for storage_key in sorted(referenced_keys):
        try:
            expected_path = _path_for_storage_key(storage_root, storage_key)
        except ValueError:
            continue
        if not expected_path.exists():
            for ref in references[storage_key]:
                missing_files.append(ref)

    result = {
        "dry_run": dry_run,
        "storage_root": str(storage_root),
        "referenced_files_count": len(referenced_keys),
        "files_on_disk_count": len(files_on_disk),
        "orphan_files_count": len(orphan_files),
        "orphan_bytes": orphan_bytes,
        "orphan_files": orphan_files,
        "missing_files_count": len(missing_files),
        "missing_files": missing_files,
        "invalid_references_count": len(invalid_references),
        "invalid_references": invalid_references,
        "deleted_files_count": deleted_files_count,
        "freed_bytes": freed_bytes,
    }

    if missing_files:
        db.add(
            AuditEvent(
                actor="system",
                entity_type="storage",
                entity_id=0,
                action="storage.missing_file_detected",
                before=None,
                after={"missing_files": missing_files, "missing_files_count": len(missing_files)},
                comment="Storage cleanup detected missing referenced files",
            )
        )
    db.add(
        AuditEvent(
            actor="system",
            entity_type="storage",
            entity_id=0,
            action="storage.cleanup_finished",
            before=None,
            after=result,
            comment="Storage cleanup finished",
        )
    )
    await db.commit()
    return result
