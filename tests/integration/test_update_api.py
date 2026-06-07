import pytest
from sqlalchemy import select

from app.models.audit import AuditEvent
from app.models.mistake import Mistake
from app.models.video import Video


@pytest.mark.asyncio
async def test_patch_video_updates_fields_and_writes_audit(client, db_session, seed_video):
    video = await seed_video(title="Original title", slug="original-title", transcript="old transcript")

    response = await client.patch(
        f"/api/videos/{video.id}?actor=pytest",
        json={"title": "Updated title", "slug": "updated-title", "transcript": None, "status": "draft"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["title"] == "Updated title"
    assert body["slug"] == "updated-title"
    assert body["transcript"] is None

    refreshed = await db_session.get(Video, video.id)
    assert refreshed.title == "Updated title"
    assert refreshed.slug == "updated-title"

    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "video.updated", AuditEvent.entity_id == video.id)
    )
    assert audit is not None
    assert audit.actor == "pytest"
    assert audit.before["title"] == "Original title"
    assert audit.after["title"] == "Updated title"
    assert audit.before["slug"] == "original-title"
    assert audit.after["slug"] == "updated-title"


@pytest.mark.asyncio
async def test_patch_video_rejects_invalid_or_duplicate_slug(client, seed_video):
    first = await seed_video(title="First", slug="first-video")
    second = await seed_video(title="Second", slug="second-video")

    invalid = await client.patch(f"/api/videos/{first.id}", json={"slug": "Bad Slug"})
    assert invalid.status_code == 422

    deleted_status = await client.patch(f"/api/videos/{first.id}", json={"status": "deleted"})
    assert deleted_status.status_code == 422

    duplicate = await client.patch(f"/api/videos/{first.id}", json={"slug": second.slug})
    assert duplicate.status_code == 400


@pytest.mark.asyncio
async def test_patch_video_rejects_deleted_video(client, seed_video):
    video = await seed_video(title="Deleted", slug="deleted-update")
    delete_response = await client.delete(f"/api/videos/{video.id}?actor=pytest")
    assert delete_response.status_code == 202

    response = await client.patch(f"/api/videos/{video.id}", json={"title": "Should not update"})
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_patch_mistake_updates_prompts_order_and_writes_audit(client, db_session, seed_video, seed_mistake):
    video = await seed_video(title="Mistake update video", slug="mistake-update-video")
    mistake = await seed_mistake(
        video=video,
        order_index=1,
        title="Original mistake",
        short_title="Original",
        wrong_visual_prompt="old wrong",
        right_visual_prompt="old right",
        negative_criteria=["old"],
    )

    response = await client.patch(
        f"/api/mistakes/{mistake.id}?actor=pytest",
        json={
            "order_index": 3,
            "title": "Updated mistake",
            "short_title": "Updated",
            "time_start": "00:00:10",
            "time_end": "00:00:20",
            "explanation": "Updated explanation",
            "wrong_visual_prompt": "new wrong",
            "right_visual_prompt": "new right",
            "negative_criteria": ["avoid text", "avoid watermark"],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["order_index"] == 3
    assert body["title"] == "Updated mistake"
    assert body["wrong_visual_prompt"] == "new wrong"
    assert body["right_visual_prompt"] == "new right"
    assert body["negative_criteria"] == ["avoid text", "avoid watermark"]

    refreshed = await db_session.get(Mistake, mistake.id)
    assert refreshed.order_index == 3
    assert refreshed.title == "Updated mistake"

    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "mistake.updated", AuditEvent.entity_id == mistake.id)
    )
    assert audit is not None
    assert audit.actor == "pytest"
    assert audit.before["order_index"] == 1
    assert audit.after["order_index"] == 3
    assert audit.before["wrong_visual_prompt"] == "old wrong"
    assert audit.after["wrong_visual_prompt"] == "new wrong"


@pytest.mark.asyncio
async def test_patch_mistake_rejects_duplicate_order_index(client, seed_video, seed_mistake):
    video = await seed_video(title="Order conflict", slug="order-conflict")
    first = await seed_mistake(video=video, order_index=1, title="First")
    await seed_mistake(video=video, order_index=2, title="Second")

    response = await client.patch(f"/api/mistakes/{first.id}", json={"order_index": 2})

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_patch_mistake_rejects_deleted_mistake_or_deleted_video(client, seed_video, seed_mistake):
    video = await seed_video(title="Deleted mistake video", slug="deleted-mistake-video")
    deleted_mistake = await seed_mistake(video=video, order_index=1, title="Deleted mistake")
    kept_mistake = await seed_mistake(video=video, order_index=2, title="Kept until video delete")

    delete_mistake_response = await client.delete(f"/api/mistakes/{deleted_mistake.id}?actor=pytest")
    assert delete_mistake_response.status_code == 202
    deleted_patch = await client.patch(f"/api/mistakes/{deleted_mistake.id}", json={"title": "Nope"})
    assert deleted_patch.status_code == 404

    delete_video_response = await client.delete(f"/api/videos/{video.id}?actor=pytest")
    assert delete_video_response.status_code == 202
    child_patch = await client.patch(f"/api/mistakes/{kept_mistake.id}", json={"title": "Nope"})
    assert child_patch.status_code == 404


@pytest.mark.asyncio
async def test_extract_mistakes_enqueues_idempotent_job_without_inline_creation(client, db_session, seed_video):
    video = await seed_video(transcript="Кухня слишком темная, рабочая зона плохо освещена.")

    response = await client.post(f"/api/videos/{video.id}/extract-mistakes")
    repeat = await client.post(f"/api/videos/{video.id}/extract-mistakes")

    assert response.status_code == 202
    assert repeat.status_code == 202
    body = response.json()
    assert body["id"] == repeat.json()["id"]
    assert body["type"] == "extract_mistakes"
    assert body["status"] == "pending"
    assert body["payload"]["video_id"] == video.id
    assert body["idempotency_key"].startswith(f"extract_mistakes:{video.id}:")

    mistakes = (await db_session.execute(select(Mistake).where(Mistake.video_id == video.id))).scalars().all()
    assert mistakes == []


@pytest.mark.asyncio
async def test_extract_mistakes_worker_creates_mock_draft_once(client, db_session, seed_video, monkeypatch):
    from app.config import settings
    from app.services.job_runner import fetch_and_run_one_job

    monkeypatch.setattr(settings, "anthropic_api_key", None)
    video = await seed_video(transcript="Ошибка: темные фасады и мало света на кухне.")

    response = await client.post(f"/api/videos/{video.id}/extract-mistakes")
    assert response.status_code == 202

    job = await fetch_and_run_one_job(db_session)
    assert job.status == "completed"
    assert job.result["provider"] == "mock"
    assert job.result["created_count"] == 1

    repeat = await client.post(f"/api/videos/{video.id}/extract-mistakes")
    assert repeat.status_code == 202
    assert repeat.json()["id"] == response.json()["id"]
    assert repeat.json()["status"] == "completed"

    mistakes = (
        await db_session.execute(select(Mistake).where(Mistake.video_id == video.id).order_by(Mistake.order_index))
    ).scalars().all()
    assert len(mistakes) == 1
    assert mistakes[0].title.startswith("Черновик ошибки:")
    assert mistakes[0].wrong_visual_prompt
    assert mistakes[0].right_visual_prompt


@pytest.mark.asyncio
async def test_extract_mistakes_defaults_to_mock_even_with_anthropic_key(client, db_session, seed_video, monkeypatch):
    from app.config import settings
    from app.services.job_runner import fetch_and_run_one_job

    monkeypatch.setattr(settings, "anthropic_api_key", "configured-but-not-opted-in")
    monkeypatch.setattr(settings, "mistake_extraction_provider", "mock")
    video = await seed_video(transcript="Ошибка: неудобный угол кухни.")

    response = await client.post(f"/api/videos/{video.id}/extract-mistakes")
    assert response.status_code == 202

    job = await fetch_and_run_one_job(db_session)
    assert job.status == "completed"
    assert job.result["provider"] == "mock"


@pytest.mark.asyncio
async def test_create_mistake_supports_video_scoped_spec_endpoint(client, db_session, seed_video):
    video = await seed_video()

    response = await client.post(
        f"/api/videos/{video.id}/mistakes",
        json={
            "order_index": 1,
            "title": "Path scoped mistake",
            "wrong_visual_prompt": "dark kitchen",
            "right_visual_prompt": "bright kitchen",
            "negative_criteria": ["watermark"],
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["video_id"] == video.id
    assert body["title"] == "Path scoped mistake"
    assert body["negative_criteria"] == ["watermark"]

    legacy = await client.post(
        "/api/mistakes",
        json={
            "video_id": video.id,
            "order_index": 2,
            "title": "Legacy mistake",
            "wrong_visual_prompt": "wrong",
            "right_visual_prompt": "right",
        },
    )
    assert legacy.status_code == 201

    audit_actions = [row[0] for row in (await db_session.execute(select(AuditEvent.action).order_by(AuditEvent.id))).all()]
    assert audit_actions.count("mistake.created") == 2


@pytest.mark.asyncio
async def test_create_mistake_video_scoped_rejects_missing_video(client):
    response = await client.post(
        "/api/videos/999999/mistakes",
        json={"order_index": 1, "title": "Missing video"},
    )

    assert response.status_code == 404
