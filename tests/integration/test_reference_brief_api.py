import pytest
from sqlalchemy import select

from app.models.candidate import ReferenceBrief
from app.services.job_runner import fetch_and_run_one_job


@pytest.mark.asyncio
async def test_reference_brief_job_creates_draft_and_is_idempotent(client, db_session, seed_mistake, seed_candidate, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "worker_job_types", "create_reference_brief")
    mistake = await seed_mistake(
        title="Too dark cabinets",
        explanation="The kitchen looks visually heavy.",
        wrong_visual_prompt="dark matte kitchen cabinets",
        right_visual_prompt="bright balanced kitchen cabinets",
        negative_criteria=["people", "text overlays"],
    )
    candidate = await seed_candidate(
        mistake=mistake,
        status="approved_reference",
        usage_role="reference_only",
        side="wrong",
        domain="example.com",
        original_width=1600,
        original_height=900,
    )

    candidate_id = candidate.id
    mistake_id = mistake.id

    first = await client.post(f"/api/candidates/{candidate_id}/reference-brief")
    second = await client.post(f"/api/candidates/{candidate_id}/reference-brief")

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["type"] == "create_reference_brief"
    assert first.json()["idempotency_key"] == f"reference_brief:{candidate_id}:mock-v1"

    job = await fetch_and_run_one_job(db_session)
    assert job.status == "completed"
    assert job.result["candidate_id"] == candidate_id

    repeat_after_completed = await client.post(f"/api/candidates/{candidate_id}/reference-brief")
    assert repeat_after_completed.status_code == 202
    assert repeat_after_completed.json()["id"] == first.json()["id"]

    response = await client.get(f"/api/candidates/{candidate_id}/reference-brief")
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_id"] == candidate_id
    assert body["mistake_id"] == mistake_id
    assert body["side"] == "wrong"
    assert body["status"] == "draft"
    assert "Too dark cabinets" in body["visual_problem"]
    assert "dark matte kitchen cabinets" in body["important_visual_signs"]
    assert "people" in body["do_not_copy"]
    assert isinstance(body["important_visual_signs"], list)
    assert isinstance(body["do_not_copy"], list)

    stored = await db_session.scalar(select(ReferenceBrief).where(ReferenceBrief.candidate_id == candidate_id))
    assert stored.important_visual_signs == body["important_visual_signs"]
    assert stored.error_message is None


@pytest.mark.asyncio
async def test_reference_brief_job_persists_error_message_on_failure(client, db_session, seed_candidate, monkeypatch):
    from app.config import settings
    import app.services.reference_brief_service as reference_brief_service

    monkeypatch.setattr(settings, "worker_job_types", "create_reference_brief")
    candidate = await seed_candidate(status="approved_reference", usage_role="reference_only")
    candidate_id = candidate.id

    def fail_build_reference_brief_draft(_candidate):
        raise RuntimeError("brief generation unavailable")

    monkeypatch.setattr(reference_brief_service, "build_reference_brief_draft", fail_build_reference_brief_draft)

    response = await client.post(f"/api/candidates/{candidate_id}/reference-brief")
    assert response.status_code == 202

    job = await fetch_and_run_one_job(db_session)
    assert job.status == "pending"
    assert "brief generation unavailable" in job.error_message

    brief_response = await client.get(f"/api/candidates/{candidate_id}/reference-brief")
    assert brief_response.status_code == 200
    body = brief_response.json()
    assert body["status"] == "failed"
    assert "brief generation unavailable" in body["error_message"]

    stored = await db_session.scalar(select(ReferenceBrief).where(ReferenceBrief.candidate_id == candidate_id))
    assert stored.status == "failed"
    assert "brief generation unavailable" in stored.error_message


@pytest.mark.asyncio
async def test_reference_brief_requires_reference_candidate(client, seed_candidate):
    candidate = await seed_candidate(status="review", usage_role="candidate")
    candidate_id = candidate.id

    response = await client.post(f"/api/candidates/{candidate_id}/reference-brief")

    assert response.status_code == 422
    assert "approved_reference" in response.text


@pytest.mark.asyncio
async def test_reference_brief_manual_patch(client, db_session, seed_candidate, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "worker_job_types", "create_reference_brief")
    candidate = await seed_candidate(status="approved_reference", usage_role="reference_only")
    candidate_id = candidate.id
    created = await client.post(f"/api/candidates/{candidate_id}/reference-brief")
    assert created.status_code == 202
    await fetch_and_run_one_job(db_session)

    response = await client.patch(
        f"/api/candidates/{candidate_id}/reference-brief",
        json={
            "visual_problem": "manual problem",
            "important_visual_signs": ["manual sign"],
            "do_not_copy": ["manual no-copy"],
            "clean_generation_brief": "manual clean brief",
            "negative_prompt": "manual negative",
            "status": "approved",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["visual_problem"] == "manual problem"
    assert body["important_visual_signs"] == ["manual sign"]
    assert body["do_not_copy"] == ["manual no-copy"]
    assert body["status"] == "approved"


@pytest.mark.asyncio
async def test_reference_brief_manual_patch_validates_status(client, db_session, seed_candidate, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "worker_job_types", "create_reference_brief")
    candidate = await seed_candidate(status="approved_reference", usage_role="reference_only")
    candidate_id = candidate.id
    await client.post(f"/api/candidates/{candidate_id}/reference-brief")
    await fetch_and_run_one_job(db_session)

    response = await client.patch(
        f"/api/candidates/{candidate_id}/reference-brief",
        json={"status": "published"},
    )

    assert response.status_code == 422
