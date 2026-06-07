import json
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

import app.services.candidate_review_runner as candidate_review_runner
from app.config import settings
from app.models.asset import FinalAsset
from app.models.candidate import CandidateReview, ImageCandidate
from app.models.job import Job
from app.services.job_runner import process_single_job


def ensure_review_image(storage_key: str = "review/candidate.jpg") -> str:
    path = settings.storage_root / storage_key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake image bytes for reviewer payload tests")
    return storage_key


@pytest.mark.asyncio
async def test_run_reviewers_endpoint_creates_default_jobs_idempotently(client, db_session, seed_candidate):
    candidate = await seed_candidate(storage_key_original="review/downloaded.jpg", storage_status="ok")

    response = await client.post(f"/api/candidates/{candidate.id}/reviews/run", json={})
    assert response.status_code == 202
    body = response.json()
    assert len(body) == 3
    assert {job["type"] for job in body} == {"run_candidate_reviewer"}
    assert {job["payload"]["reviewer_name"] for job in body} == {"codex", "antigravity", "claude_cli"}
    assert all(job["payload"]["candidate_id"] == candidate.id for job in body)
    assert all(job["idempotency_key"].startswith(f"candidate_review:{candidate.id}:") for job in body)

    first_ids = [job["id"] for job in body]
    repeated = await client.post(f"/api/candidates/{candidate.id}/reviews/run", json={})
    assert repeated.status_code == 202
    assert [job["id"] for job in repeated.json()] == first_ids

    count = await db_session.scalar(select(func.count()).select_from(Job))
    assert count == 3


@pytest.mark.asyncio
async def test_run_reviewers_endpoint_validates_reviewer_list(client, seed_candidate):
    candidate = await seed_candidate(storage_key_original="review/downloaded.jpg", storage_status="ok")

    duplicate = await client.post(
        f"/api/candidates/{candidate.id}/reviews/run",
        json={"reviewers": ["codex", "codex"]},
    )
    assert duplicate.status_code == 422

    unknown = await client.post(
        f"/api/candidates/{candidate.id}/reviews/run",
        json={"reviewers": ["unknown"]},
    )
    assert unknown.status_code == 422

    missing = await client.post("/api/candidates/999999/reviews/run", json={})
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_force_run_reviewers_creates_new_jobs(client, db_session, seed_candidate):
    candidate = await seed_candidate(storage_key_original="review/downloaded.jpg", storage_status="ok")

    first = await client.post(
        f"/api/candidates/{candidate.id}/reviews/run",
        json={"reviewers": ["codex"], "prompt_version": "v1"},
    )
    forced = await client.post(
        f"/api/candidates/{candidate.id}/reviews/run",
        json={"reviewers": ["codex"], "prompt_version": "v1", "force": True},
    )

    assert first.status_code == 202
    assert forced.status_code == 202
    assert first.json()[0]["id"] != forced.json()[0]["id"]

    count = await db_session.scalar(select(func.count()).select_from(Job))
    assert count == 2


@pytest.mark.asyncio
async def test_run_reviewers_endpoint_requires_downloaded_candidate(client, seed_candidate):
    candidate = await seed_candidate(storage_key_original=None, storage_status="pending")

    response = await client.post(f"/api/candidates/{candidate.id}/reviews/run", json={"reviewers": ["antigravity"]})

    assert response.status_code == 409
    assert "downloaded" in response.json()["detail"]


@pytest.mark.asyncio
async def test_run_candidate_reviewer_job_upserts_review_without_auto_final(db_session, seed_candidate, monkeypatch):
    candidate = await seed_candidate(storage_key_original=ensure_review_image(), storage_status="ok")
    scores = {"codex": 0.81, "antigravity": 0.77, "claude_cli": 0.22}
    seen_payloads = []

    async def fake_run_reviewer_cli(reviewer_name, payload):
        seen_payloads.append(payload)
        return {
            "reviewer_name": reviewer_name,
            "reviewer_version": f"{reviewer_name}-test",
            "score": scores[reviewer_name],
            "verdict": "pass" if scores[reviewer_name] >= 0.7 else "fail",
            "reason": f"{reviewer_name} reason",
            "flags": {"mocked": True},
        }

    monkeypatch.setattr(candidate_review_runner, "run_reviewer_cli", fake_run_reviewer_cli)

    results = []
    for reviewer in ["codex", "antigravity", "claude_cli"]:
        results.append(
            await process_single_job(
                Job(
                    type="run_candidate_reviewer",
                    payload={"candidate_id": candidate.id, "reviewer_name": reviewer, "prompt_version": "runner-test-v1"},
                ),
                db_session,
            )
        )

    assert results[-1]["review_score"] == 0.77
    assert results[-1]["pass_count"] == 2
    assert results[-1]["approved_by_consensus"] is True
    assert seen_payloads[0]["prompt_version"] == "runner-test-v1"
    assert seen_payloads[0]["candidate"]["id"] == candidate.id
    assert seen_payloads[0]["candidate"]["review_image_source"] == "local_file"
    assert seen_payloads[0]["candidate"]["image_file_available"] is True
    assert seen_payloads[0]["candidate"]["image_file_path"].endswith("review/candidate.jpg")
    assert seen_payloads[0]["mistake"]["id"] == candidate.mistake_id

    reviews = (await db_session.execute(select(CandidateReview).order_by(CandidateReview.reviewer_name))).scalars().all()
    assert [review.reviewer_name for review in reviews] == ["antigravity", "claude_cli", "codex"]
    assert {review.reviewer_version for review in reviews} == {"codex-test", "antigravity-test", "claude_cli-test"}
    assert all(review.response_time_ms is not None and review.response_time_ms >= 0 for review in reviews)
    assert results[-1]["response_time_ms"] is not None

    refreshed = await db_session.get(ImageCandidate, candidate.id)
    assert refreshed.status == "auto_reviewed"
    assert float(refreshed.review_score) == 0.77

    final_count = await db_session.scalar(select(func.count()).select_from(FinalAsset))
    assert final_count == 0


@pytest.mark.asyncio
async def test_reviewer_status_api_reports_host_bridge_contract(client, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "host_reviewer_status_path", tmp_path / "missing-host-reviewer-status.json")
    monkeypatch.setattr(settings, "codex_cli_command", "python3 --version")
    monkeypatch.setattr(settings, "antigravity_cli_command", None)
    monkeypatch.setattr(settings, "claude_cli_command", "/definitely/missing/claude")

    response = await client.get("/api/reviewers/status")

    assert response.status_code == 200
    body = {item["reviewer_name"]: item for item in response.json()}
    assert body["codex"]["execution_environment"] == "host_bridge"
    assert body["codex"]["ready"] is False
    assert body["codex"]["executable"] is False
    assert body["codex"]["web_process_executable"] is True
    assert "command" not in body["codex"]
    assert "host_reviewer_bridge" in body["codex"]["message"]
    assert body["codex"]["error"] == "host bridge heartbeat file not found"
    assert body["antigravity"]["configured"] is False
    assert body["antigravity"]["error"] == "command is not configured for host bridge"
    assert body["claude_cli"]["ready"] is False
    assert body["claude_cli"]["web_process_executable"] is False


@pytest.mark.asyncio
async def test_reviewer_status_api_reports_fresh_host_bridge_heartbeat(client, monkeypatch, tmp_path):
    status_path = tmp_path / "host_reviewer_bridge_status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "service": "host_reviewer_bridge",
                "heartbeat_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "state": "idle",
                "pid": 4321,
                "locked_by": "bridge-api-test",
                "reviewers": {
                    "codex": {"configured": True},
                    "antigravity": {"configured": False},
                    "claude_cli": {"configured": True},
                },
            }
        )
    )
    monkeypatch.setattr(settings, "host_reviewer_status_path", status_path)
    monkeypatch.setattr(settings, "host_reviewer_status_ttl_seconds", 30)

    response = await client.get("/api/reviewers/status")

    assert response.status_code == 200
    body = {item["reviewer_name"]: item for item in response.json()}
    assert body["codex"]["ready"] is True
    assert body["codex"]["executable"] is True
    assert body["codex"]["host_bridge_state"] == "idle"
    assert body["codex"]["host_bridge_pid"] == 4321
    assert body["codex"]["host_bridge_locked_by"] == "bridge-api-test"
    assert body["codex"]["error"] is None
    assert body["antigravity"]["ready"] is False
    assert body["antigravity"]["error"] == "command is not configured on host bridge"
    assert body["claude_cli"]["ready"] is True


@pytest.mark.asyncio
async def test_candidate_review_payload_api_reuses_runner_contract(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake(title="Bad Lighting", wrong_visual_prompt="dark kitchen")
    candidate = await seed_candidate(
        mistake=mistake,
        side="wrong",
        image_url="https://example.com/kitchen.jpg",
        storage_key_original=ensure_review_image("review/payload.jpg"),
        storage_status="ok",
    )

    response = await client.get(f"/api/candidates/{candidate.id}/review-payload?prompt_version=test-v1")

    assert response.status_code == 200
    body = response.json()
    assert body["prompt_version"] == "test-v1"
    assert body["candidate"]["id"] == candidate.id
    assert body["candidate"]["image_url"] == "https://example.com/kitchen.jpg"
    assert body["candidate"]["review_image_source"] == "local_file"
    assert body["candidate"]["image_file_available"] is True
    assert body["candidate"]["image_file_path"].endswith("review/payload.jpg")
    assert body["mistake"]["id"] == mistake.id
    assert body["rubric"]["pass_threshold"] == 0.7



@pytest.mark.asyncio
async def test_reviewer_write_endpoints_require_admin_token_when_configured(client, seed_candidate, monkeypatch):
    candidate = await seed_candidate(storage_key_original="review/downloaded.jpg", storage_status="ok")
    monkeypatch.setattr(settings, "admin_api_token", "secret-test-token")

    denied = await client.post(f"/api/candidates/{candidate.id}/reviews/run", json={})
    assert denied.status_code == 401

    allowed = await client.post(
        f"/api/candidates/{candidate.id}/reviews/run",
        json={"reviewers": ["codex"]},
        headers={"X-Admin-Token": "secret-test-token"},
    )
    assert allowed.status_code == 202

    status_denied = await client.get("/api/reviewers/status")
    assert status_denied.status_code == 401

    status_allowed = await client.get("/api/reviewers/status", headers={"X-Admin-Token": "secret-test-token"})
    assert status_allowed.status_code == 200


@pytest.mark.asyncio
async def test_run_candidate_reviewer_skips_existing_review_without_cli(db_session, seed_candidate, monkeypatch):
    candidate = await seed_candidate(storage_key_original=ensure_review_image("review/skip.jpg"), storage_status="ok")

    async def first_review(_reviewer_name, _payload):
        return {
            "reviewer_version": "skip-test-reviewer",
            "score": 0.84,
            "verdict": "pass",
            "reason": "seed existing review",
            "flags": {},
        }

    monkeypatch.setattr(candidate_review_runner, "run_reviewer_cli", first_review)
    await candidate_review_runner.run_candidate_reviewer(candidate.id, "codex", db_session, prompt_version="skip-test", force=True)

    async def fail_if_called(_reviewer_name, _payload):
        raise AssertionError("reviewer CLI should not run when review already exists and force is false")

    monkeypatch.setattr(candidate_review_runner, "run_reviewer_cli", fail_if_called)

    result = await candidate_review_runner.run_candidate_reviewer(
        candidate.id,
        "codex",
        db_session,
        prompt_version="skip-test",
        force=False,
    )

    assert result["skipped_existing_review"] is True
