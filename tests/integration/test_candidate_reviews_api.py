import pytest


@pytest.mark.asyncio
async def test_candidate_reviews_api_contract(client, seed_candidate):
    candidate = await seed_candidate()

    missing = await client.get("/api/candidates/999999/reviews")
    assert missing.status_code == 404

    response = await client.put(
        f"/api/candidates/{candidate.id}/reviews/codex",
        json={
            "reviewer_name": "codex",
            "reviewer_version": "test",
            "score": 0.8,
            "verdict": "pass",
            "reason": "good",
            "flags": {"x": 1},
            "response_time_ms": 1234,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["candidate_id"] == candidate.id
    assert body["mistake_id"] == candidate.mistake_id
    assert body["side"] == candidate.side
    assert body["response_time_ms"] == 1234

    response = await client.put(
        f"/api/candidates/{candidate.id}/reviews/antigravity",
        json={"reviewer_name": "antigravity", "score": 0.9, "verdict": "pass"},
    )
    assert response.status_code == 200

    aggregate = await client.get(f"/api/candidates/{candidate.id}/reviews/aggregate")
    assert aggregate.status_code == 200
    assert aggregate.json()["approved_by_consensus"] is True
    assert aggregate.json()["pass_count"] == 2

    reviews = await client.get(f"/api/candidates/{candidate.id}/reviews")
    assert reviews.status_code == 200
    assert [r["reviewer_name"] for r in reviews.json()] == ["antigravity", "codex"]


@pytest.mark.asyncio
async def test_candidate_reviews_api_rejects_bad_payloads(client, seed_candidate):
    candidate = await seed_candidate()

    mismatch = await client.put(
        f"/api/candidates/{candidate.id}/reviews/codex",
        json={"reviewer_name": "claude_cli", "score": 0.5, "verdict": "maybe"},
    )
    assert mismatch.status_code == 422

    unknown = await client.put(
        f"/api/candidates/{candidate.id}/reviews/unknown",
        json={"reviewer_name": "unknown", "score": 0.5, "verdict": "maybe"},
    )
    assert unknown.status_code == 422

    bad_score = await client.put(
        f"/api/candidates/{candidate.id}/reviews/codex",
        json={"reviewer_name": "codex", "score": 2, "verdict": "pass"},
    )
    assert bad_score.status_code == 422
