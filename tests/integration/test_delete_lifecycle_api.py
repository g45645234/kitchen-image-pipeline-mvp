from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.candidate import ImageCandidate
from app.models.job import Job
from app.models.mistake import Mistake
from app.models.video import Video
from app.services.job_runner import process_single_job
from app.services.storage_service import cleanup_storage


def _write(path: Path, data: bytes = b"x") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path.relative_to(settings.storage_root).as_posix()


@pytest.mark.asyncio
async def test_delete_video_soft_deletes_children_and_creates_cleanup_job(
    client,
    db_session,
    seed_video,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    video = await seed_video(title="Delete lifecycle", slug="delete-lifecycle")
    mistake = await seed_mistake(video=video, title="Delete me")
    final_key = _write(storage_root / "projects" / str(video.id) / "final.jpg", b"final")
    candidate_key = _write(storage_root / "candidates" / "candidate.jpg", b"candidate")
    unrelated_orphan = storage_root / "unrelated" / "video-orphan.tmp"
    unrelated_orphan.parent.mkdir(parents=True)
    unrelated_orphan.write_bytes(b"keep-me")

    db_session.add(
        FinalAsset(
            video_id=video.id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="own_upload",
            rights_status="own",
            may_use_directly=True,
            storage_key_original=final_key,
            storage_status="ok",
            status="approved",
        )
    )
    db_session.add(
        ImageCandidate(
            mistake_id=mistake.id,
            side="wrong",
            source_type="search",
            source_provider="test",
            source_page_url="https://example.com/source",
            image_url="https://example.com/candidate.jpg",
            image_url_hash="delete-video-candidate",
            original_width=100,
            original_height=100,
            domain="example.com",
            rights_status="unknown",
            usage_role="candidate",
            may_use_directly=False,
            storage_key_original=candidate_key,
            storage_status="ok",
            quality_flags={},
        )
    )
    await db_session.commit()

    response = await client.delete(f"/api/videos/{video.id}?actor=pytest")
    assert response.status_code == 202
    job = response.json()
    assert job["type"] == "cleanup_storage"
    assert job["payload"]["reason"] == "delete_video"
    assert job["payload"]["video_id"] == video.id
    assert job["payload"]["targets"]["final_assets"][0]["storage_key_original"] == final_key
    assert job["payload"]["targets"]["candidates"][0]["storage_key_original"] == candidate_key

    assert (await client.get(f"/api/videos/{video.id}")).status_code == 404
    assert (await client.get(f"/api/videos/{video.id}?include_deleted=true")).status_code == 200
    assert (await client.get(f"/api/videos/{video.id}/mistakes")).status_code == 404

    refreshed_video = await db_session.get(Video, video.id)
    refreshed_mistake = await db_session.get(Mistake, mistake.id)
    assert refreshed_video.deleted_at is not None
    assert refreshed_video.status == "deleted"
    assert refreshed_mistake.deleted_at is not None

    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "video.deleted"))
    assert audit is not None
    assert audit.before["final_assets"][0]["storage_key_original"] == final_key

    report = await cleanup_storage(dry_run=True, db=db_session)
    orphan_keys = {item["storage_key"] for item in report["orphan_files"]}
    assert {final_key, candidate_key}.issubset(orphan_keys)

    cleanup_job_model = await db_session.get(Job, job["id"])
    cleanup_result = await process_single_job(cleanup_job_model, db_session)
    assert cleanup_result["mode"] == "targeted"
    assert cleanup_result["deleted_files_count"] == 2
    assert not (storage_root / final_key).exists()
    assert not (storage_root / candidate_key).exists()
    assert unrelated_orphan.exists()


@pytest.mark.asyncio
async def test_delete_mistake_soft_deletes_mistake_only_and_creates_cleanup_job(
    client,
    db_session,
    seed_video,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    video = await seed_video(title="Keep video", slug="keep-video")
    deleted_mistake = await seed_mistake(video=video, order_index=1, title="Delete mistake")
    kept_mistake = await seed_mistake(video=video, order_index=2, title="Keep mistake")
    final_key = _write(storage_root / "projects" / str(video.id) / "mistake-final.jpg", b"final")
    unrelated_orphan = storage_root / "unrelated" / "mistake-orphan.tmp"
    unrelated_orphan.parent.mkdir(parents=True)
    unrelated_orphan.write_bytes(b"keep-me")

    db_session.add(
        FinalAsset(
            video_id=video.id,
            mistake_id=deleted_mistake.id,
            side="right",
            source_type="own_upload",
            rights_status="own",
            may_use_directly=True,
            storage_key_original=final_key,
            storage_status="ok",
            status="approved",
        )
    )
    await db_session.commit()

    response = await client.delete(f"/api/mistakes/{deleted_mistake.id}?actor=pytest")
    assert response.status_code == 202
    job = response.json()
    assert job["payload"]["reason"] == "delete_mistake"
    assert job["payload"]["mistake_id"] == deleted_mistake.id
    assert job["payload"]["targets"]["final_assets"][0]["storage_key_original"] == final_key

    assert (await client.get(f"/api/videos/{video.id}")).status_code == 200
    visible = await client.get(f"/api/videos/{video.id}/mistakes")
    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()] == [kept_mistake.id]
    assert (await client.get(f"/api/mistakes/{deleted_mistake.id}")).status_code == 404
    assert (await client.post(f"/api/mistakes/{deleted_mistake.id}/candidates/search")).status_code == 404

    deleted = await db_session.get(Mistake, deleted_mistake.id)
    assert deleted.deleted_at is not None
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "mistake.deleted"))
    assert audit is not None

    cleanup_job_model = await db_session.get(Job, job["id"])
    cleanup_result = await process_single_job(cleanup_job_model, db_session)
    assert cleanup_result["mode"] == "targeted"
    assert cleanup_result["deleted_files_count"] == 1
    assert not (storage_root / final_key).exists()
    assert unrelated_orphan.exists()
