import asyncio
from datetime import datetime, timezone
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import settings
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.job import Job
from app.services.export_service import export_video_manifest
from app.services.image_processing_limiter import image_processing_slot
from app.services.job_runner import process_single_job
from app.services.storage_service import cleanup_storage


def _png_bytes(size=(640, 480), color=(120, 80, 40)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="PNG")
    return output.getvalue()


def _jpeg_bytes(size=(640, 480), color=(120, 80, 40)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_upload_final_asset_enqueues_processing_without_waiting_for_image_slot(
    client,
    engine,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "max_image_processing_jobs", 1)
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "worker_job_types", "process_final_asset")
    mistake = await seed_mistake(order_index=2, title="Limiter", short_title="Limiter")
    session_maker = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with session_maker() as holder:
        async with image_processing_slot(holder):
            response = await client.post(
                f"/api/mistakes/{mistake.id}/upload-final-asset",
                data={"side": "wrong", "license_note": "own file", "actor": "pytest"},
                files={"file": ("limited.png", _png_bytes(), "image/png")},
            )
            assert response.status_code == 201
            body = response.json()
            assert body["storage_status"] == "processing"
            assert body["storage_key_original"].endswith("original.png")
            assert body["storage_key_processed"] is None

            async with session_maker() as worker_session:
                process_job = await worker_session.scalar(
                    select(Job).where(Job.type == "process_final_asset", Job.payload["asset_id"].as_integer() == body["id"])
                )
                assert process_job is not None
                process_task = asyncio.create_task(process_single_job(process_job, worker_session))
                await asyncio.sleep(0.2)
                assert not process_task.done()
                await holder.commit()
                result = await asyncio.wait_for(process_task, timeout=5)
                assert result["asset_id"] == body["id"]

@pytest.mark.asyncio
async def test_upload_own_final_asset_creates_processed_files_and_exports_processed_asset(
    client,
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    mistake = await seed_mistake(order_index=3, title="Own Upload Mistake", short_title="Own Upload")

    response = await client.post(
        f"/api/mistakes/{mistake.id}/upload-final-asset",
        data={
            "side": "wrong",
            "license_note": "own photo from local render",
            "license_document_ref": "local-note-1",
            "author_name": "Kitchen Team",
            "caption": "Uploaded final",
            "actor": "pytest",
        },
        files={"file": ("own-upload.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["candidate_id"] is None
    assert body["source_type"] == "own_upload"
    assert body["rights_status"] == "own"
    assert body["may_use_directly"] is True
    assert body["license_note"] == "own photo from local render"
    assert body["original_exif_preserved"] is True
    assert body["processed_exif_stripped"] is False
    assert body["storage_status"] == "processing"
    assert body["storage_key_thumbnail"] is None
    assert body["storage_key_processed"] is None

    process_job = await db_session.scalar(
        select(Job).where(Job.type == "process_final_asset", Job.payload["asset_id"].as_integer() == body["id"])
    )
    assert process_job is not None
    result = await process_single_job(process_job, db_session)
    assert result["asset_id"] == body["id"]

    refreshed = await db_session.get(FinalAsset, body["id"])
    assert refreshed.storage_status == "ok"
    assert refreshed.processed_exif_stripped is True
    body = {**body, **{
        "storage_key_thumbnail": refreshed.storage_key_thumbnail,
        "storage_key_processed": refreshed.storage_key_processed,
        "metadata_storage_key": refreshed.metadata_storage_key,
    }}

    original_path = storage_root / body["storage_key_original"]
    thumb_path = storage_root / body["storage_key_thumbnail"]
    processed_path = storage_root / body["storage_key_processed"]
    metadata_path = storage_root / body["metadata_storage_key"]
    assert original_path.exists()
    assert thumb_path.exists()
    assert processed_path.exists()
    assert metadata_path.exists()

    with Image.open(processed_path) as processed:
        assert processed.size == (1920, 1080)

    processed_response = await client.get(f"/api/final-assets/{body['id']}/processed")
    assert processed_response.status_code == 200
    thumbnail_response = await client.get(f"/api/final-assets/{body['id']}/thumbnail")
    assert thumbnail_response.status_code == 200

    candidates_page = await client.get(f"/ui/mistakes/{mistake.id}/candidates")
    assert candidates_page.status_code == 200
    assert f"asset #{body['id']} own_upload" in candidates_page.text
    assert f'href="/api/final-assets/{body["id"]}/thumbnail"' in candidates_page.text
    assert f'href="/api/final-assets/{body["id"]}/processed"' in candidates_page.text

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["asset_id"] == body["id"]
    assert metadata["original_filename"] == "own-upload.png"
    assert metadata["processed_exif_stripped"] is True

    audit = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.entity_type == "final_asset",
                AuditEvent.entity_id == body["id"],
                AuditEvent.action == "final_asset.uploaded",
            )
        )
    ).scalars().first()
    assert audit is not None
    assert audit.comment == "own photo from local render"

    manifest_path = Path(await export_video_manifest(mistake.video_id, db_session))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exported_asset = manifest["mistakes"][0]["wrong_assets"][0]
    assert exported_asset["source_type"] == "own_upload"
    assert exported_asset["file"].endswith("_1920x1080.jpg")
    exported_file = manifest_path.parent / exported_asset["file"]
    with Image.open(exported_file) as exported:
        assert exported.size == (1920, 1080)


@pytest.mark.asyncio
async def test_list_and_delete_final_asset_enqueues_cleanup_job(
    client,
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    mistake = await seed_mistake(order_index=4, title="Delete Final Asset", short_title="Delete Final")
    upload = await client.post(
        f"/api/mistakes/{mistake.id}/upload-final-asset",
        data={
            "side": "right",
            "license_note": "own file for delete test",
            "actor": "pytest",
        },
        files={"file": ("delete-me.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 201
    asset = upload.json()
    old_keys = [
        key for key in [
            asset["storage_key_original"],
            asset["storage_key_thumbnail"],
            asset["storage_key_processed"],
            asset["metadata_storage_key"],
        ]
        if key
    ]
    unrelated_orphan = storage_root / "unrelated" / "orphan.tmp"
    unrelated_orphan.parent.mkdir(parents=True)
    unrelated_orphan.write_bytes(b"keep-me")

    listed = await client.get(f"/api/videos/{mistake.video_id}/final-assets?limit=10&offset=0")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [asset["id"]]

    delete_response = await client.delete(f"/api/final-assets/{asset['id']}?actor=pytest")
    assert delete_response.status_code == 202
    cleanup_job = delete_response.json()
    assert cleanup_job["type"] == "cleanup_storage"
    assert cleanup_job["payload"] == {
        "dry_run": False,
        "mode": "targeted",
        "reason": "delete_final_asset",
        "asset_id": asset["id"],
        "old_storage_keys": old_keys,
    }
    assert cleanup_job["idempotency_key"] == f"cleanup_storage:final_asset:{asset['id']}"

    refreshed = await db_session.get(FinalAsset, asset["id"])
    assert refreshed.status == "rejected"
    assert refreshed.storage_status == "cleanup_pending"
    assert refreshed.storage_key_original is None
    assert refreshed.storage_key_thumbnail is None
    assert refreshed.storage_key_processed is None
    assert refreshed.metadata_storage_key is None

    active_list = await client.get(f"/api/videos/{mistake.video_id}/final-assets")
    assert active_list.status_code == 200
    assert active_list.json() == []

    rejected_list = await client.get(f"/api/videos/{mistake.video_id}/final-assets?include_rejected=true")
    assert rejected_list.status_code == 200
    assert [item["id"] for item in rejected_list.json()] == [asset["id"]]

    audit = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.entity_type == "final_asset",
                AuditEvent.entity_id == asset["id"],
                AuditEvent.action == "final_asset.deleted",
            )
        )
    ).scalars().first()
    assert audit is not None
    assert audit.before["storage_key_original"] == old_keys[0]

    cleanup_report = await cleanup_storage(dry_run=True, db=db_session)
    orphan_keys = {item["storage_key"] for item in cleanup_report["orphan_files"]}
    assert set(old_keys).issubset(orphan_keys)

    cleanup_job_model = await db_session.get(Job, cleanup_job["id"])
    cleanup_result = await process_single_job(cleanup_job_model, db_session)
    assert cleanup_result["mode"] == "targeted"
    assert cleanup_result["deleted_files_count"] == len(old_keys)
    for storage_key in old_keys:
        assert not (storage_root / storage_key).exists()
    assert unrelated_orphan.exists()


@pytest.mark.asyncio
async def test_upload_own_final_asset_rejects_deleted_mistake_or_video(client, db_session, seed_video, seed_mistake):
    deleted_at = datetime.now(timezone.utc)

    deleted_mistake = await seed_mistake()
    deleted_mistake.deleted_at = deleted_at
    await db_session.commit()

    response = await client.post(
        f"/api/mistakes/{deleted_mistake.id}/upload-final-asset",
        data={"side": "wrong", "license_note": "own file", "actor": "pytest"},
        files={"file": ("deleted-mistake.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 404

    deleted_video = await seed_video()
    live_mistake_deleted_video = await seed_mistake(video=deleted_video)
    deleted_video.deleted_at = deleted_at
    await db_session.commit()

    response = await client.post(
        f"/api/mistakes/{live_mistake_deleted_video.id}/upload-final-asset",
        data={"side": "right", "license_note": "own file", "actor": "pytest"},
        files={"file": ("deleted-video.png", _png_bytes(), "image/png")},
    )
    assert response.status_code == 404

    count = await db_session.scalar(select(FinalAsset).where(FinalAsset.mistake_id.in_([deleted_mistake.id, live_mistake_deleted_video.id])))
    assert count is None


@pytest.mark.asyncio
async def test_upload_final_asset_rejects_oversized_file_before_image_processing(
    client, seed_mistake, monkeypatch
):
    monkeypatch.setattr(settings, "max_upload_mb", 0)
    mistake = await seed_mistake()

    response = await client.post(
        f"/api/mistakes/{mistake.id}/upload-final-asset",
        data={"side": "wrong", "license_note": "own file", "actor": "pytest"},
        files={"file": ("too-large.png", _png_bytes(), "image/png")},
    )

    assert response.status_code == 422
    assert "exceeds configured size limit" in response.json()["detail"]


@pytest.mark.asyncio
async def test_reupload_final_asset_enqueues_cleanup_for_replaced_original(
    client,
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    mistake = await seed_mistake(order_index=5, title="Replace Upload", short_title="Replace Upload")
    first_upload = await client.post(
        f"/api/mistakes/{mistake.id}/upload-final-asset",
        data={"side": "wrong", "license_note": "first own file", "actor": "pytest"},
        files={"file": ("first.png", _png_bytes(color=(10, 20, 30)), "image/png")},
    )
    assert first_upload.status_code == 201
    first_asset = first_upload.json()
    old_original_key = first_asset["storage_key_original"]
    assert old_original_key.endswith("original.png")
    assert (storage_root / old_original_key).exists()

    second_upload = await client.post(
        f"/api/mistakes/{mistake.id}/upload-final-asset",
        data={"side": "wrong", "license_note": "replacement own file", "actor": "pytest"},
        files={"file": ("second.jpg", _jpeg_bytes(color=(40, 50, 60)), "image/jpeg")},
    )
    assert second_upload.status_code == 201
    second_asset = second_upload.json()
    assert second_asset["id"] == first_asset["id"]
    assert second_asset["storage_key_original"].endswith("original.jpg")
    assert second_asset["storage_key_original"] != old_original_key

    result = await db_session.execute(
        select(Job).where(Job.idempotency_key == f"cleanup_storage:replace_final_asset:{second_asset['id']}")
    )
    cleanup_job = result.scalars().first()
    assert cleanup_job is not None
    assert cleanup_job.payload == {
        "dry_run": False,
        "mode": "targeted",
        "reason": "replace_final_asset",
        "asset_id": second_asset["id"],
        "old_storage_keys": [old_original_key],
    }

    unrelated_orphan = storage_root / "unrelated" / "orphan.tmp"
    unrelated_orphan.parent.mkdir(parents=True)
    unrelated_orphan.write_bytes(b"keep-me")

    cleanup_result = await process_single_job(cleanup_job, db_session)
    assert cleanup_result["mode"] == "targeted"
    assert cleanup_result["deleted_files_count"] == 1
    assert not (storage_root / old_original_key).exists()
    assert (storage_root / second_asset["storage_key_original"]).exists()
    assert unrelated_orphan.exists()


@pytest.mark.asyncio
async def test_repeated_delete_final_asset_is_idempotent_after_cleanup_completed(
    client,
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    mistake = await seed_mistake(order_index=6, title="Repeated Delete", short_title="Repeated Delete")
    upload = await client.post(
        f"/api/mistakes/{mistake.id}/upload-final-asset",
        data={"side": "right", "license_note": "delete twice", "actor": "pytest"},
        files={"file": ("delete-twice.png", _png_bytes(), "image/png")},
    )
    assert upload.status_code == 201
    asset = upload.json()

    first_delete = await client.delete(f"/api/final-assets/{asset['id']}?actor=pytest")
    assert first_delete.status_code == 202
    first_job = first_delete.json()
    cleanup_job_model = await db_session.get(Job, first_job["id"])
    await process_single_job(cleanup_job_model, db_session)
    cleanup_job_model.status = "completed"
    await db_session.commit()

    second_delete = await client.delete(f"/api/final-assets/{asset['id']}?actor=pytest")
    assert second_delete.status_code == 202
    second_job = second_delete.json()
    assert second_job["id"] == first_job["id"]
    assert second_job["idempotency_key"] == first_job["idempotency_key"]

    audits = (
        await db_session.execute(
            select(AuditEvent).where(
                AuditEvent.entity_type == "final_asset",
                AuditEvent.entity_id == asset["id"],
                AuditEvent.action == "final_asset.deleted",
            )
        )
    ).scalars().all()
    assert len(audits) == 1

    cleanup_jobs = (
        await db_session.execute(
            select(Job).where(Job.idempotency_key == f"cleanup_storage:final_asset:{asset['id']}")
        )
    ).scalars().all()
    assert len(cleanup_jobs) == 1
