from app.config import settings
from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.job import Job
from sqlalchemy import func, select
import pytest


@pytest.mark.asyncio
async def test_create_video_rejects_invalid_slug(client):
    response = await client.post(
        "/api/videos",
        json={"title": "Bad slug", "slug": "../Bad Slug", "transcript": None},
    )

    assert response.status_code == 422
    assert "slug" in response.text


@pytest.mark.asyncio
async def test_create_video_accepts_conservative_slug(client, db_session):
    response = await client.post(
        "/api/videos",
        json={"title": "Good slug", "slug": "good-slug-123", "transcript": None},
    )

    assert response.status_code == 201
    assert response.json()["slug"] == "good-slug-123"
    audit = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "video.created"))
    assert audit is not None
    assert audit.entity_type == "video"
    assert audit.after["slug"] == "good-slug-123"


@pytest.mark.asyncio
async def test_create_video_rejects_deleted_status(client):
    response = await client.post(
        "/api/videos",
        json={"title": "Bad status", "slug": "bad-status", "status": "deleted"},
    )

    assert response.status_code == 422
    assert "status" in response.text


@pytest.mark.asyncio
async def test_list_videos_supports_status_filter_and_offset(client, seed_video):
    first_draft = await seed_video(status="draft")
    await seed_video(status="ready_to_export")
    second_draft = await seed_video(status="draft")

    draft_page = await client.get("/api/videos?status=draft&limit=10&offset=0")
    assert draft_page.status_code == 200
    draft_ids = [item["id"] for item in draft_page.json()]
    assert set(draft_ids) == {first_draft.id, second_draft.id}

    offset_page = await client.get("/api/videos?status=draft&limit=1&offset=1")
    assert offset_page.status_code == 200
    assert [item["id"] for item in offset_page.json()] == draft_ids[1:2]


@pytest.mark.asyncio
async def test_list_mistakes_for_video_supports_limit_and_offset(client, seed_video, seed_mistake):
    video = await seed_video()
    await seed_mistake(video=video, order_index=1, title="First")
    second = await seed_mistake(video=video, order_index=2, title="Second")
    third = await seed_mistake(video=video, order_index=3, title="Third")

    response = await client.get(f"/api/videos/{video.id}/mistakes?limit=2&offset=1")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [second.id, third.id]


@pytest.mark.asyncio
async def test_video_export_readiness_reports_blocked_without_final_assets(client, seed_mistake):
    mistake = await seed_mistake()

    response = await client.get(f"/api/videos/{mistake.video_id}/export-readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["video_id"] == mistake.video_id
    assert body["can_export"] is False
    assert body["complete"] is False
    assert body["active_mistake_count"] == 1
    assert body["ready_asset_count"] == 0
    assert {warning["code"] for warning in body["warnings"]} == {
        "missing_final_assets",
        "no_export_ready_assets",
    }


@pytest.mark.asyncio
async def test_export_api_rejects_video_without_ready_assets(client, db_session, seed_mistake):
    mistake = await seed_mistake()

    response = await client.post(f"/api/videos/{mistake.video_id}/export")

    assert response.status_code == 400
    body = response.json()
    assert body["detail"]["message"] == "Video is not export-ready"
    assert {warning["code"] for warning in body["detail"]["warnings"]} == {
        "missing_final_assets",
        "no_export_ready_assets",
    }
    count = await db_session.scalar(select(func.count()).select_from(Job))
    assert count == 0


@pytest.mark.asyncio
async def test_video_export_readiness_reports_complete_ready_assets(client, db_session, seed_mistake, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    mistake = await seed_mistake()

    def write_key(key: str):
        path = settings.storage_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"ready")

    for side in ["wrong", "right"]:
        prefix = f"projects/{mistake.video_id}/final_assets/{side}"
        keys = {
            "storage_key_original": f"{prefix}/original.jpg",
            "storage_key_thumbnail": f"{prefix}/thumb.jpg",
            "storage_key_processed": f"{prefix}/processed_1920x1080.jpg",
        }
        for key in keys.values():
            write_key(key)
        db_session.add(
            FinalAsset(
                video_id=mistake.video_id,
                mistake_id=mistake.id,
                side=side,
                source_type="own_upload",
                rights_status="own",
                may_use_directly=True,
                status="approved",
                storage_status="ok",
                **keys,
            )
        )
    await db_session.commit()

    response = await client.get(f"/api/videos/{mistake.video_id}/export-readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["can_export"] is True
    assert body["complete"] is True
    assert body["exportable_asset_count"] == 2
    assert body["ready_asset_count"] == 2
    assert body["warnings"] == []


@pytest.mark.asyncio
async def test_video_export_readiness_reports_broken_selected_asset(client, db_session, seed_mistake):
    mistake = await seed_mistake()
    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="own_upload",
            rights_status="own",
            may_use_directly=True,
            status="approved",
            storage_status="ok",
            storage_key_original="projects/1/final_assets/1/original.jpg",
            storage_key_thumbnail="projects/1/final_assets/1/thumb.jpg",
            storage_key_processed="projects/1/final_assets/1/processed_1920x1080.jpg",
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/videos/{mistake.video_id}/export-readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["can_export"] is False
    assert "final_asset_not_ready" in {warning["code"] for warning in body["warnings"]}
    not_ready = next(warning for warning in body["warnings"] if warning["code"] == "final_asset_not_ready")
    assert "missing_storage_files" in not_ready["health_warnings"]


@pytest.mark.asyncio
async def test_video_export_readiness_allows_export_autoheal_from_original(client, db_session, seed_mistake, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    mistake = await seed_mistake()
    original_key = f"projects/{mistake.video_id}/final_assets/wrong/original.jpg"
    original_path = settings.storage_root / original_key
    original_path.parent.mkdir(parents=True, exist_ok=True)
    original_path.write_bytes(b"original")
    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="own_upload",
            rights_status="own",
            may_use_directly=True,
            status="approved",
            storage_status="ok",
            storage_key_original=original_key,
            storage_key_thumbnail=None,
            storage_key_processed=None,
        )
    )
    await db_session.commit()

    response = await client.get(f"/api/videos/{mistake.video_id}/export-readiness")

    assert response.status_code == 200
    body = response.json()
    assert body["can_export"] is True
    assert body["complete"] is False
    assert body["exportable_asset_count"] == 1
    assert body["ready_asset_count"] == 1
    assert body["ready_mistake_count"] == 0
    warning_codes = {warning["code"] for warning in body["warnings"]}
    assert "final_asset_needs_derivatives" in warning_codes
    assert "missing_side_final_asset" in warning_codes
    needs_derivatives = next(
        warning for warning in body["warnings"] if warning["code"] == "final_asset_needs_derivatives"
    )
    assert needs_derivatives["health_warnings"] == ["missing_processed_asset"]
