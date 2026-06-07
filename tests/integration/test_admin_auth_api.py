import pytest

from app.config import settings


@pytest.mark.asyncio
async def test_local_without_admin_token_keeps_dev_workflow_open(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "admin_api_token", None)

    admin_response = await client.get("/admin/videos")
    assert admin_response.status_code == 200

    create_response = await client.post(
        "/api/videos",
        json={"title": "Local Auth Smoke", "slug": "local-auth-smoke", "status": "draft"},
    )
    assert create_response.status_code == 201


@pytest.mark.asyncio
async def test_admin_token_protects_admin_ui_jobs_and_write_endpoints(
    client, seed_mistake, seed_candidate, monkeypatch
):
    monkeypatch.setattr(settings, "app_env", "local")
    monkeypatch.setattr(settings, "admin_api_token", "secret-test-token")
    mistake = await seed_mistake()
    storage_key = "auth/review.jpg"
    image_path = settings.storage_root / storage_key
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(b"fake image bytes")
    candidate = await seed_candidate(mistake=mistake, storage_key_original=storage_key, storage_status="ok")

    assert (await client.get("/admin/videos")).status_code == 401
    assert (await client.get("/api/videos")).status_code == 401
    assert (await client.get(f"/api/mistakes/{mistake.id}/candidates")).status_code == 401
    assert (await client.get(f"/api/candidates/{candidate.id}/review-payload")).status_code == 401
    assert (await client.get("/api/jobs")).status_code == 401
    assert (await client.post("/api/jobs/cleanup?dry_run=true")).status_code == 401
    assert (await client.post(f"/api/mistakes/{mistake.id}/candidates/search")).status_code == 401
    assert (await client.post(f"/api/candidates/{candidate.id}/reviews/run", json={})).status_code == 401
    assert (await client.post(f"/api/candidates/{candidate.id}/select-final")).status_code == 401
    assert (await client.post(f"/api/videos/{mistake.video_id}/export")).status_code == 401
    assert (await client.delete(f"/api/mistakes/{mistake.id}")).status_code == 401

    header = {"X-Admin-Token": "secret-test-token"}
    allowed_create = await client.post(
        "/api/videos",
        json={"title": "Token Auth Smoke", "slug": "token-auth-smoke", "status": "draft"},
        headers=header,
    )
    assert allowed_create.status_code == 201

    allowed_videos = await client.get("/api/videos", headers=header)
    assert allowed_videos.status_code == 200

    allowed_payload = await client.get(f"/api/candidates/{candidate.id}/review-payload", headers=header)
    assert allowed_payload.status_code == 200

    allowed_jobs = await client.get("/api/jobs", headers=header)
    assert allowed_jobs.status_code == 200

    allowed_ui = await client.get("/admin/videos", headers={"Cookie": "admin_api_token=secret-test-token"})
    assert allowed_ui.status_code == 200


@pytest.mark.asyncio
async def test_non_local_without_admin_token_fails_closed(client, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "admin_api_token", None)

    admin_response = await client.get("/admin/videos")
    assert admin_response.status_code == 503

    write_response = await client.post(
        "/api/videos",
        json={"title": "Prod Auth Smoke", "slug": "prod-auth-smoke", "status": "draft"},
    )
    assert write_response.status_code == 503

    reviewer_response = await client.get("/api/reviewers/status")
    assert reviewer_response.status_code == 503

    read_response = await client.get("/api/videos")
    assert read_response.status_code == 503
