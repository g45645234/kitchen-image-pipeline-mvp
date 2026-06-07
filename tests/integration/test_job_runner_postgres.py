from datetime import datetime, timedelta, timezone
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.candidate import SearchQuery
from app.models.job import Job
from app.services.job_runner import fetch_and_run_one_job, requeue_stale_processing_jobs


def _jpeg_bytes(size=(640, 360), color=(80, 120, 160)) -> bytes:
    image = Image.new("RGB", size, color)
    output = BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_requeue_stale_processing_jobs_returns_old_locks_to_pending(db_session, monkeypatch):
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 30)
    stale = Job(
        type="cleanup_storage",
        status="processing",
        payload={"dry_run": True},
        attempts=1,
        max_attempts=3,
        locked_by="dead-worker",
        locked_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    fresh = Job(
        type="cleanup_storage",
        status="processing",
        payload={"dry_run": True},
        attempts=1,
        max_attempts=3,
        locked_by="live-worker",
        locked_at=datetime.now(timezone.utc),
    )
    exhausted = Job(
        type="cleanup_storage",
        status="processing",
        payload={"dry_run": True},
        attempts=3,
        max_attempts=3,
        locked_by="dead-worker",
        locked_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    db_session.add_all([stale, fresh, exhausted])
    await db_session.commit()

    count = await requeue_stale_processing_jobs(db_session)

    assert count == 2
    refreshed_stale = await db_session.get(Job, stale.id)
    refreshed_fresh = await db_session.get(Job, fresh.id)
    refreshed_exhausted = await db_session.get(Job, exhausted.id)

    assert refreshed_stale.status == "pending"
    assert refreshed_stale.locked_by is None
    assert refreshed_stale.locked_at is None
    assert refreshed_stale.error_message == "Requeued stale processing job"

    assert refreshed_fresh.status == "processing"
    assert refreshed_fresh.locked_by == "live-worker"

    assert refreshed_exhausted.status == "failed"
    assert refreshed_exhausted.locked_by is None
    assert refreshed_exhausted.finished_at is not None


@pytest.mark.asyncio
async def test_fetch_and_run_one_job_respects_max_running_jobs(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "max_running_jobs", 1)
    monkeypatch.setattr(settings, "worker_job_types", "cleanup_storage")
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    processing = Job(
        type="cleanup_storage",
        status="processing",
        payload={"dry_run": True},
        attempts=1,
        locked_by="live-worker",
        locked_at=datetime.now(timezone.utc),
    )
    pending = Job(type="cleanup_storage", status="pending", payload={"dry_run": True})
    db_session.add_all([processing, pending])
    await db_session.commit()

    result = await fetch_and_run_one_job(db_session)

    assert result is None
    refreshed_pending = await db_session.get(Job, pending.id)
    assert refreshed_pending.status == "pending"
    assert refreshed_pending.locked_by is None


@pytest.mark.asyncio
async def test_fetch_and_run_one_job_skips_image_jobs_when_image_limit_reached(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "max_running_jobs", 5)
    monkeypatch.setattr(settings, "max_image_processing_jobs", 1)
    monkeypatch.setattr(settings, "worker_job_types", "export_final_assets,cleanup_storage")
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    processing_image = Job(
        type="export_final_assets",
        status="processing",
        payload={"video_id": 1},
        attempts=1,
        locked_by="live-worker",
        locked_at=datetime.now(timezone.utc),
    )
    pending_image = Job(type="export_final_assets", status="pending", payload={"video_id": 2})
    cleanup = Job(type="cleanup_storage", status="pending", payload={"dry_run": True})
    db_session.add_all([processing_image, pending_image, cleanup])
    await db_session.commit()

    result = await fetch_and_run_one_job(db_session)

    assert result is not None
    assert result.type == "cleanup_storage"
    assert result.status == "completed"
    refreshed_pending_image = await db_session.get(Job, pending_image.id)
    assert refreshed_pending_image.status == "pending"


@pytest.mark.asyncio
async def test_fetch_and_run_one_job_returns_none_when_only_image_jobs_are_limited(db_session, monkeypatch):
    monkeypatch.setattr(settings, "max_running_jobs", 5)
    monkeypatch.setattr(settings, "max_image_processing_jobs", 1)
    monkeypatch.setattr(settings, "worker_job_types", "export_final_assets")
    processing_image = Job(
        type="export_final_assets",
        status="processing",
        payload={"video_id": 1},
        attempts=1,
        locked_by="live-worker",
        locked_at=datetime.now(timezone.utc),
    )
    pending_image = Job(type="export_final_assets", status="pending", payload={"video_id": 2})
    db_session.add_all([processing_image, pending_image])
    await db_session.commit()

    result = await fetch_and_run_one_job(db_session)

    assert result is None
    refreshed_pending_image = await db_session.get(Job, pending_image.id)
    assert refreshed_pending_image.status == "pending"


@pytest.mark.asyncio
async def test_fetch_and_run_one_job_requeues_stale_before_max_running_check(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "max_running_jobs", 1)
    monkeypatch.setattr(settings, "job_lock_timeout_minutes", 30)
    monkeypatch.setattr(settings, "worker_job_types", "cleanup_storage")
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    stale = Job(
        type="cleanup_storage",
        status="processing",
        payload={"dry_run": True},
        attempts=1,
        locked_by="dead-worker",
        locked_at=datetime.now(timezone.utc) - timedelta(minutes=45),
    )
    pending = Job(type="cleanup_storage", status="pending", payload={"dry_run": True})
    db_session.add_all([stale, pending])
    await db_session.commit()

    result = await fetch_and_run_one_job(db_session)

    assert result is not None
    assert result.status == "completed"
    refreshed_stale = await db_session.get(Job, stale.id)
    refreshed_pending = await db_session.get(Job, pending.id)
    assert refreshed_stale.status in {"pending", "completed"}
    assert refreshed_stale.locked_by is None
    assert {refreshed_stale.status, refreshed_pending.status} == {"pending", "completed"}


@pytest.mark.asyncio
async def test_fetch_and_run_jobs_respects_worker_job_type_filter(db_session, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "worker_job_types", "cleanup_storage")
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    reviewer_job = Job(type="run_candidate_reviewer", status="pending", payload={"candidate_id": 1, "reviewer_name": "codex"})
    cleanup_job = Job(type="cleanup_storage", status="pending", payload={"dry_run": True})
    db_session.add_all([reviewer_job, cleanup_job])
    await db_session.commit()
    reviewer_job_id = reviewer_job.id
    cleanup_job_id = cleanup_job.id

    await fetch_and_run_one_job(db_session)

    refreshed_reviewer = await db_session.get(Job, reviewer_job_id)
    refreshed_cleanup = await db_session.get(Job, cleanup_job_id)
    assert refreshed_reviewer.status == "pending"
    assert refreshed_cleanup.status == "completed"
    assert refreshed_cleanup.result["deleted_files_count"] == 0
    assert refreshed_cleanup.result["freed_bytes"] == 0
    assert refreshed_cleanup.result["orphan_files_count"] == 0



@pytest.mark.asyncio
async def test_fetch_and_run_one_job_writes_job_failed_audit(db_session, monkeypatch):
    monkeypatch.setattr(settings, "worker_job_types", "unknown_job_type")
    job = Job(type="unknown_job_type", status="pending", payload={"example": True}, max_attempts=1)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    await fetch_and_run_one_job(db_session)

    refreshed = await db_session.get(Job, job_id)
    assert refreshed.status == "failed"
    event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "job.failed"))
    assert event is not None
    assert event.entity_type == "job"
    assert event.entity_id == job_id
    assert event.after["status"] == "failed"
    assert "Unknown job type" in event.after["error_message"]


@pytest.mark.asyncio
async def test_process_single_job_supports_export_video_alias(db_session, seed_mistake, seed_candidate, monkeypatch, tmp_path):
    from pathlib import Path

    from app.services.final_asset_service import select_candidate_as_final
    from app.services.job_runner import process_single_job

    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    mistake = await seed_mistake(title="Export video alias")
    source_file = storage_root / "job" / "asset.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes())
    candidate = await seed_candidate(
        mistake=mistake,
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="job/asset.jpg",
        storage_status="ok",
    )
    await select_candidate_as_final(candidate.id, db_session)

    result = await process_single_job(Job(type="export_video", payload={"video_id": mistake.video_id}), db_session)

    assert Path(result["manifest_path"]).exists()
    assert Path(result["assets_csv_path"]).exists()


@pytest.mark.asyncio
async def test_process_final_asset_job_creates_derivatives(db_session, monkeypatch, tmp_path, seed_mistake):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "worker_job_types", "process_final_asset")
    mistake = await seed_mistake()
    original = settings.storage_root / "final" / "original.jpg"
    original.parent.mkdir(parents=True)
    original.write_bytes(_jpeg_bytes(size=(800, 600)))
    asset = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="wrong",
        source_type="own_upload",
        rights_status="own",
        may_use_directly=True,
        status="approved",
        storage_status="ok",
        storage_key_original="final/original.jpg",
    )
    db_session.add(asset)
    await db_session.commit()
    asset_id = asset.id
    db_session.add(Job(type="process_final_asset", status="pending", payload={"asset_id": asset_id}))
    await db_session.commit()

    job = await fetch_and_run_one_job(db_session)

    assert job.status == "completed"
    refreshed = await db_session.get(FinalAsset, asset_id)
    assert refreshed.storage_status == "ok"
    assert refreshed.storage_key_thumbnail.endswith("thumb.jpg")
    assert refreshed.storage_key_processed.endswith("processed_1920x1080.jpg")
    assert (settings.storage_root / refreshed.storage_key_processed).exists()


@pytest.mark.asyncio
async def test_download_candidate_job_stores_original(db_session, seed_candidate, monkeypatch, tmp_path):
    import app.services.storage_service as storage_service

    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "worker_job_types", "download_candidate")

    async def fake_fetch_image_bytes(url: str) -> bytes:
        return _jpeg_bytes()

    monkeypatch.setattr(storage_service, "_fetch_image_bytes", fake_fetch_image_bytes)
    candidate = await seed_candidate(image_url="https://images.example.com/download.jpg", storage_key_original=None, storage_status="pending")
    candidate_id = candidate.id
    db_session.add(Job(type="download_candidate", status="pending", payload={"candidate_id": candidate_id}))
    await db_session.commit()

    job = await fetch_and_run_one_job(db_session)

    assert job.status == "completed"
    refreshed = await db_session.get(type(candidate), candidate_id)
    assert refreshed.storage_status == "ok"
    assert refreshed.storage_key_original.startswith(f"candidates/{candidate_id}_")
    assert (settings.storage_root / refreshed.storage_key_original).exists()


@pytest.mark.asyncio
async def test_process_single_job_supports_spec_search_query_and_score_jobs(db_session, seed_mistake, seed_candidate):
    from app.services.job_runner import process_single_job

    mistake = await seed_mistake(
        title="Cabinet glare",
        wrong_visual_prompt="glossy kitchen cabinets with glare",
        right_visual_prompt="matte kitchen cabinets",
    )

    query_result = await process_single_job(
        Job(
            type="create_search_queries",
            payload={"mistake_id": mistake.id, "sides": ["wrong"], "providers": ["mock_search"], "limit_per_query": 5},
        ),
        db_session,
    )
    assert query_result["mistake_id"] == mistake.id
    assert query_result["query_count"] >= 1
    stored_queries = (await db_session.execute(select(SearchQuery).where(SearchQuery.mistake_id == mistake.id))).scalars().all()
    assert [query.id for query in stored_queries] == query_result["query_ids"]

    candidate = await seed_candidate(
        mistake=mistake,
        side="wrong",
        original_width=1920,
        original_height=1080,
        source_page_url="https://example.com/source",
        domain="example.com",
        score_quality=None,
        review_score=None,
    )
    score_result = await process_single_job(
        Job(type="score_candidates", payload={"mistake_id": mistake.id, "side": "wrong"}),
        db_session,
    )
    assert candidate.id in score_result["candidate_ids"]
    await db_session.refresh(candidate)
    assert float(candidate.score_quality) == 1.0
    assert float(candidate.review_score) == 1.0
    assert candidate.score_visual is None
    assert candidate.is_low_quality is False
