import pytest

from app.config import settings
from app.models.asset import FinalAsset
from sqlalchemy import func, select

from app.models.job import Job


@pytest.mark.asyncio
async def test_search_export_cleanup_return_active_existing_job(client, db_session, seed_mistake, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    mistake = await seed_mistake()
    video_id = mistake.video_id
    ready_path = settings.storage_root / "exports" / "ready.jpg"
    ready_path.parent.mkdir(parents=True, exist_ok=True)
    ready_path.write_bytes(b"ready")
    db_session.add(
        FinalAsset(
            video_id=video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="own_upload",
            rights_status="own",
            may_use_directly=True,
            status="approved",
            storage_status="ok",
            storage_key_original="exports/ready.jpg",
            storage_key_processed="exports/ready.jpg",
            storage_key_thumbnail="exports/ready.jpg",
        )
    )
    await db_session.commit()

    first_search = await client.post(f"/api/mistakes/{mistake.id}/candidates/search")
    second_search = await client.post(f"/api/mistakes/{mistake.id}/candidates/search")
    assert first_search.status_code == 202
    assert second_search.status_code == 202
    assert first_search.json()["id"] == second_search.json()["id"]
    assert first_search.json()["idempotency_key"] == f"search_all_queries:{mistake.id}"

    first_export = await client.post(f"/api/videos/{video_id}/export")
    second_export = await client.post(f"/api/videos/{video_id}/export")
    assert first_export.status_code == 202
    assert second_export.status_code == 202
    assert first_export.json()["id"] == second_export.json()["id"]
    assert first_export.json()["idempotency_key"] == f"export_video:{video_id}"
    assert first_export.json()["type"] == "export_video"

    first_cleanup = await client.post("/api/jobs/cleanup?dry_run=true")
    second_cleanup = await client.post("/api/jobs/cleanup?dry_run=true")
    assert first_cleanup.status_code == 202
    assert second_cleanup.status_code == 202
    assert first_cleanup.json()["id"] == second_cleanup.json()["id"]
    assert first_cleanup.json()["idempotency_key"] == "cleanup_storage:dry_run:true"

    delete_cleanup = await client.post("/api/jobs/cleanup?dry_run=false")
    assert delete_cleanup.status_code == 202
    assert delete_cleanup.json()["id"] != first_cleanup.json()["id"]
    assert delete_cleanup.json()["idempotency_key"] == "cleanup_storage:dry_run:false"

    count = await db_session.scalar(select(func.count()).select_from(Job))
    assert count == 4


@pytest.mark.asyncio
async def test_idempotent_job_rerun_after_completed_creates_new_job(client, db_session, seed_mistake):
    mistake = await seed_mistake()

    first = await client.post(f"/api/mistakes/{mistake.id}/candidates/search")
    assert first.status_code == 202
    first_job = await db_session.get(Job, first.json()["id"])
    first_job.status = "completed"
    await db_session.commit()

    rerun = await client.post(f"/api/mistakes/{mistake.id}/candidates/search")
    rerun_again = await client.post(f"/api/mistakes/{mistake.id}/candidates/search")

    assert rerun.status_code == 202
    assert rerun_again.status_code == 202
    assert rerun.json()["id"] != first.json()["id"]
    assert rerun_again.json()["id"] == rerun.json()["id"]
    assert rerun.json()["idempotency_key"].startswith(f"search_all_queries:{mistake.id}:rerun:")


@pytest.mark.asyncio
async def test_list_jobs_supports_spec_status_limit_and_offset(client, db_session):
    jobs = [
        Job(type="cleanup_storage", status="pending", payload={"n": 1}),
        Job(type="cleanup_storage", status="processing", payload={"n": 2}),
        Job(type="cleanup_storage", status="completed", payload={"n": 3}),
        Job(type="cleanup_storage", status="completed", payload={"n": 4}),
        Job(type="cleanup_storage", status="failed", payload={"n": 5}),
    ]
    db_session.add_all(jobs)
    await db_session.commit()

    running = await client.get("/api/jobs?status=running&limit=10&offset=0")
    succeeded_page_one = await client.get("/api/jobs?status=succeeded&limit=1&offset=0")
    succeeded_page_two = await client.get("/api/jobs?status=succeeded&limit=1&offset=1")
    completed_legacy_skip = await client.get("/api/jobs?status=completed&limit=1&skip=1")

    assert running.status_code == 200
    assert [job["id"] for job in running.json()] == [jobs[1].id]

    assert succeeded_page_one.status_code == 200
    assert succeeded_page_two.status_code == 200
    assert len(succeeded_page_one.json()) == 1
    assert len(succeeded_page_two.json()) == 1
    assert succeeded_page_one.json()[0]["status"] == "completed"
    assert succeeded_page_two.json()[0]["status"] == "completed"
    assert succeeded_page_one.json()[0]["id"] != succeeded_page_two.json()[0]["id"]

    assert completed_legacy_skip.status_code == 200
    assert completed_legacy_skip.json()[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_storage_cleanup_spec_endpoints_are_idempotent(client, db_session):
    dry_run = await client.post("/api/storage/cleanup-dry-run")
    dry_run_again = await client.post("/api/storage/cleanup?dry_run=true")
    assert dry_run.status_code == 202
    assert dry_run_again.status_code == 202
    assert dry_run.json()["id"] == dry_run_again.json()["id"]
    assert dry_run.json()["payload"] == {"dry_run": True}
    assert dry_run.json()["idempotency_key"] == "cleanup_storage:dry_run:true"

    delete_cleanup = await client.post("/api/storage/cleanup")
    assert delete_cleanup.status_code == 202
    assert delete_cleanup.json()["id"] != dry_run.json()["id"]
    assert delete_cleanup.json()["payload"] == {"dry_run": False}
    assert delete_cleanup.json()["idempotency_key"] == "cleanup_storage:dry_run:false"


@pytest.mark.asyncio
async def test_candidate_download_and_final_asset_process_jobs_are_idempotent(client, db_session, seed_candidate, seed_mistake):
    from app.models.asset import FinalAsset

    candidate = await seed_candidate(image_url_hash="download-hash", storage_key_original=None)
    first_download = await client.post(f"/api/candidates/{candidate.id}/download")
    second_download = await client.post(f"/api/candidates/{candidate.id}/download")

    assert first_download.status_code == 202
    assert second_download.status_code == 202
    assert second_download.json()["id"] == first_download.json()["id"]
    assert first_download.json()["type"] == "download_candidate"
    assert first_download.json()["idempotency_key"] == f"download_candidate:{candidate.id}:download-hash"

    mistake = await seed_mistake()
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

    first_process = await client.post(f"/api/final-assets/{asset_id}/process")
    second_process = await client.post(f"/api/final-assets/{asset_id}/process")

    assert first_process.status_code == 202
    assert second_process.status_code == 202
    assert second_process.json()["id"] == first_process.json()["id"]
    assert first_process.json()["type"] == "process_final_asset"
    assert first_process.json()["idempotency_key"].startswith(f"process_final_asset:{asset_id}:")
