import pytest
from sqlalchemy import func, select

from app.config import settings
from app.models.audit import BlockedDomain
from app.models.candidate import ImageCandidate, SearchQuery
from app.models.job import Job
from app.models.feedback import MistakeSideFeedback
from app.services.job_runner import fetch_and_run_one_job, process_single_job


@pytest.mark.asyncio
async def test_generate_search_queries_is_idempotent(client, db_session, seed_mistake):
    mistake = await seed_mistake(wrong_visual_prompt="Dark cramped kitchen", right_visual_prompt="Bright clean kitchen")

    first = await client.post(
        f"/api/mistakes/{mistake.id}/generate-search-queries",
        json={"providers": ["mock_search"], "limit_per_query": 7},
    )
    second = await client.post(
        f"/api/mistakes/{mistake.id}/generate-search-queries",
        json={"providers": ["mock_search"], "limit_per_query": 7},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert [item["id"] for item in second.json()] == [item["id"] for item in first.json()]
    assert {item["side"] for item in first.json()} == {"wrong", "right"}
    assert {item["source_provider"] for item in first.json()} == {"mock_search"}
    assert {item["results_count"] for item in first.json()} == {7}

    count = await db_session.scalar(select(func.count()).select_from(SearchQuery))
    assert count == 2


@pytest.mark.asyncio
async def test_list_search_queries_supports_side_limit_and_offset(client, seed_mistake):
    mistake = await seed_mistake()
    generated = await client.post(
        f"/api/mistakes/{mistake.id}/generate-search-queries",
        json={"providers": ["mock_search"], "limit_per_query": 3},
    )
    wrong_id = next(item["id"] for item in generated.json() if item["side"] == "wrong")

    response = await client.get(f"/api/mistakes/{mistake.id}/search-queries?side=wrong&limit=1&offset=0")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [wrong_id]
    assert response.json()[0]["side"] == "wrong"


@pytest.mark.asyncio
async def test_search_alias_creates_spec_payload_job(client, seed_mistake):
    mistake = await seed_mistake()

    response = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["mock"], "limit_per_query": 1},
    )

    assert response.status_code == 202
    body = response.json()
    assert len(body) == 1
    assert body[0]["type"] == "run_search"
    assert body[0]["idempotency_key"].startswith("run_search:")
    assert body[0]["payload"]["limit_per_query"] == 1
    assert "query_id" in body[0]["payload"]


@pytest.mark.asyncio
async def test_search_query_requests_enforce_max_limit_per_query(client, seed_mistake):
    mistake = await seed_mistake()

    generated = await client.post(
        f"/api/mistakes/{mistake.id}/generate-search-queries",
        json={"providers": ["mock_search"], "limit_per_query": 51},
    )
    manual = await client.post(
        f"/api/mistakes/{mistake.id}/search-queries",
        json={"side": "wrong", "source_provider": "mock_search", "query_text": "kitchen", "results_count": 51},
    )
    search = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"providers": ["mock_search"], "limit_per_query": 51},
    )

    assert generated.status_code == 422
    assert manual.status_code == 422
    assert search.status_code == 422


@pytest.mark.asyncio
async def test_search_creates_one_active_job_per_mistake_side_provider(client, db_session, seed_mistake):
    mistake = await seed_mistake(wrong_visual_prompt="Generated wrong prompt")

    first = await client.post(
        f"/api/mistakes/{mistake.id}/search-queries",
        json={"side": "wrong", "source_provider": "mock", "query_text": "older query", "results_count": 4},
    )
    second = await client.post(
        f"/api/mistakes/{mistake.id}/search-queries",
        json={"side": "wrong", "source_provider": "mock", "query_text": "newer query", "results_count": 4},
    )
    assert first.status_code == 201
    assert second.status_code == 201

    search = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["mock_search"], "limit_per_query": 4},
    )
    repeat = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["mock_search"], "limit_per_query": 2},
    )

    assert search.status_code == 202
    assert repeat.status_code == 202
    assert len(search.json()) == 1
    assert len(repeat.json()) == 1
    assert search.json()[0]["id"] == repeat.json()[0]["id"]
    assert search.json()[0]["idempotency_key"] == f"run_search:{mistake.id}:wrong:mock_search"
    assert search.json()[0]["payload"]["query_id"] == second.json()["id"]

    count = await db_session.scalar(select(func.count()).select_from(Job).where(Job.type == "run_search"))
    assert count == 1


@pytest.mark.asyncio
async def test_search_auto_rejects_blocked_domain_candidates(
    client, db_session, seed_mistake, monkeypatch
):
    from app.services import search_service

    monkeypatch.setattr(settings, "google_api_key", "test-key")
    monkeypatch.setattr(settings, "google_cse_id", "test-cse")
    db_session.add(BlockedDomain(domain="blocked.example", reason="bad source"))
    await db_session.commit()

    async def fake_google(query, limit, api_key, cse_id):
        return [
            {
                "image_url": "https://blocked.example/image.jpg",
                "page_url": "https://blocked.example/page",
                "thumb_url": "https://blocked.example/thumb.jpg",
                "width": 1920,
                "height": 1080,
                "domain": "BLOCKED.EXAMPLE.",
            }
        ]

    monkeypatch.setattr(search_service, "_search_google", fake_google)
    mistake = await seed_mistake(wrong_visual_prompt="blocked domain kitchen")
    response = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["google"], "limit_per_query": 1},
    )
    job = await db_session.get(Job, response.json()[0]["id"])

    await process_single_job(job, db_session)

    candidate = (await db_session.execute(select(ImageCandidate))).scalars().one()
    assert candidate.domain == "blocked.example"
    assert candidate.status == "auto_rejected"
    assert candidate.reject_reason == "blocked_domain"
    assert candidate.score_quality is not None


@pytest.mark.asyncio
async def test_search_keeps_low_resolution_candidates_as_low_quality(
    client, db_session, seed_mistake, monkeypatch
):
    from app.services import search_service

    monkeypatch.setattr(settings, "pixabay_api_key", "test-key")

    async def fake_pixabay(query, limit, api_key):
        return [
            {
                "largeImageURL": "https://cdn.example.com/small.jpg",
                "pageURL": "https://example.com/small",
                "previewURL": "https://cdn.example.com/small-thumb.jpg",
                "imageWidth": 320,
                "imageHeight": 200,
                "user": "Photographer",
            }
        ]

    monkeypatch.setattr(search_service, "_search_pixabay", fake_pixabay)
    mistake = await seed_mistake(wrong_visual_prompt="tiny kitchen")
    response = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["pixabay"], "limit_per_query": 1},
    )
    job = await db_session.get(Job, response.json()[0]["id"])

    await process_single_job(job, db_session)

    candidate = (await db_session.execute(select(ImageCandidate))).scalars().one()
    assert candidate.original_width == 320
    assert candidate.original_height == 200
    assert candidate.is_low_quality is True
    assert candidate.quality_flags["low_resolution"] is True
    assert candidate.status == "new"
    assert candidate.rights_status == "free_to_use"
    assert candidate.may_use_directly is True


@pytest.mark.asyncio
async def test_yandex_search_filters_small_candidates(db_session, seed_mistake, monkeypatch):
    from app.services import search_service

    monkeypatch.setattr(settings, "yandex_api_key", "test-key")
    monkeypatch.setattr(settings, "yandex_folder_id", "test-folder")

    async def fake_yandex(query, limit, api_key, folder_id):
        return [
            {
                "image_url": "https://example.com/small.jpg",
                "page_url": "https://example.com/small",
                "thumb_url": "https://example.com/small-thumb.jpg",
                "width": 399,
                "height": 300,
                "domain": "example.com",
            },
            {
                "image_url": "https://example.com/large.jpg",
                "page_url": "https://example.com/large",
                "thumb_url": "https://example.com/large-thumb.jpg",
                "width": 400,
                "height": 300,
                "domain": "example.com",
            },
        ]

    monkeypatch.setattr(search_service, "_search_yandex_search_api", fake_yandex)
    mistake = await seed_mistake(wrong_visual_prompt="kitchen")
    query = SearchQuery(
        mistake_id=mistake.id,
        side="wrong",
        source_provider="yandex_search_api",
        query_text="кухня тест",
        results_count=2,
        status="pending",
    )
    db_session.add(query)
    await db_session.commit()
    await db_session.refresh(query)

    job = Job(type="run_search", status="pending", payload={"query_id": query.id, "limit_per_query": 2})

    await process_single_job(job, db_session)

    candidates = (await db_session.execute(select(ImageCandidate).order_by(ImageCandidate.id))).scalars().all()
    assert len(candidates) == 1
    assert candidates[0].image_url == "https://example.com/large.jpg"
    assert candidates[0].source_provider == "yandex_search_api"


@pytest.mark.asyncio
async def test_search_job_scores_created_candidates(client, db_session, seed_mistake):
    mistake = await seed_mistake()
    response = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["mock_search"], "limit_per_query": 1},
    )
    job = await db_session.get(Job, response.json()[0]["id"])

    await process_single_job(job, db_session)

    candidate = (await db_session.execute(select(ImageCandidate))).scalars().one()
    assert candidate.score_quality is not None
    assert candidate.review_score == candidate.score_quality
    assert candidate.score_visual is None
    assert candidate.reference_priority_score is None
    assert candidate.is_low_quality is True
    assert candidate.status == "new"
    assert candidate.rights_status == "unknown"
    assert candidate.may_use_directly is False
    assert candidate.quality_flags["missing_dimensions"] is True
    assert candidate.quality_flags["source_metadata_score"] == 0.4


@pytest.mark.asyncio
async def test_search_job_payload_limits_sides_and_mock_count(client, db_session, seed_mistake):
    mistake = await seed_mistake()
    response = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["mock_search"], "limit_per_query": 1},
    )
    job = await db_session.get(Job, response.json()[0]["id"])

    await process_single_job(job, db_session)

    queries = (await db_session.execute(select(SearchQuery).order_by(SearchQuery.id))).scalars().all()
    candidates = (await db_session.execute(select(ImageCandidate).order_by(ImageCandidate.id))).scalars().all()
    assert [(query.side, query.source_provider, query.results_count, query.status) for query in queries] == [
        ("wrong", "mock_search", 1, "completed")
    ]
    assert [(candidate.side, candidate.source_provider) for candidate in candidates] == [("wrong", "mock_search")]


@pytest.mark.asyncio
async def test_run_search_uses_exact_query_id_without_regenerating_text(db_session, seed_mistake):
    mistake = await seed_mistake(wrong_visual_prompt="Generated wrong prompt")
    target_query = SearchQuery(
        mistake_id=mistake.id,
        side="wrong",
        source_provider="mock_search",
        query_text="manual exact transcript query",
        results_count=2,
        status="pending",
    )
    newer_query = SearchQuery(
        mistake_id=mistake.id,
        side="wrong",
        source_provider="mock_search",
        query_text="newer query must not run",
        results_count=2,
        status="pending",
    )
    db_session.add_all([target_query, newer_query])
    await db_session.commit()
    await db_session.refresh(target_query)
    await db_session.refresh(newer_query)

    job = Job(type="run_search", status="pending", payload={"query_id": target_query.id, "limit_per_query": 2})

    result = await process_single_job(job, db_session)

    await db_session.refresh(target_query)
    await db_session.refresh(newer_query)
    candidates = (
        await db_session.execute(select(ImageCandidate).order_by(ImageCandidate.id))
    ).scalars().all()

    assert result["queries"][0]["id"] == target_query.id
    assert target_query.query_text == "manual exact transcript query"
    assert target_query.status == "completed"
    assert newer_query.query_text == "newer query must not run"
    assert newer_query.status == "pending"
    assert {candidate.query_id for candidate in candidates} == {target_query.id}
    assert len(candidates) == 2


@pytest.mark.asyncio
async def test_multi_provider_search_job_records_partial_failure(
    db_session, seed_mistake, monkeypatch, tmp_path
):
    monkeypatch.setattr(settings, "pixabay_api_key", None)
    monkeypatch.setattr(settings, "worker_job_types", "search_all_queries")
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    mistake = await seed_mistake(wrong_visual_prompt="Dark kitchen")
    job = Job(
        type="search_all_queries",
        status="pending",
        payload={
            "mistake_id": mistake.id,
            "sides": ["wrong"],
            "providers": ["mock_search", "pixabay"],
            "limit_per_query": 1,
        },
    )
    db_session.add(job)
    await db_session.commit()

    result = await fetch_and_run_one_job(db_session)

    assert result.status == "partially_failed"
    assert result.result["status"] == "partially_failed"
    queries = (await db_session.execute(select(SearchQuery).order_by(SearchQuery.source_provider))).scalars().all()
    assert [(query.source_provider, query.status) for query in queries] == [
        ("mock_search", "completed"),
        ("pixabay", "failed"),
    ]
    assert "credentials_missing" in queries[1].error_message
    candidate_count = await db_session.scalar(select(func.count()).select_from(ImageCandidate))
    assert candidate_count == 1


@pytest.mark.asyncio
async def test_single_provider_run_search_failure_marks_query_failed(
    db_session, seed_mistake, monkeypatch
):
    monkeypatch.setattr(settings, "pixabay_api_key", None)
    mistake = await seed_mistake(wrong_visual_prompt="Dark kitchen")
    query = SearchQuery(
        mistake_id=mistake.id,
        side="wrong",
        source_provider="pixabay",
        query_text="dark kitchen",
        results_count=1,
        status="pending",
    )
    db_session.add(query)
    await db_session.commit()

    job = Job(type="run_search", status="pending", payload={"query_id": query.id, "limit_per_query": 1})

    with pytest.raises(ValueError, match="Search provider failed"):
        await process_single_job(job, db_session)

    refreshed = await db_session.get(SearchQuery, query.id)
    assert refreshed.status == "failed"
    assert "credentials_missing" in refreshed.error_message


@pytest.mark.asyncio
async def test_search_query_endpoints_reject_missing_or_invalid_inputs(client, seed_mistake):
    mistake = await seed_mistake()

    missing = await client.get("/api/mistakes/999999/search-queries")
    invalid_side = await client.post(
        f"/api/mistakes/{mistake.id}/generate-search-queries",
        json={"sides": ["left"], "providers": ["mock_search"]},
    )
    invalid_provider = await client.post(
        f"/api/mistakes/{mistake.id}/generate-search-queries",
        json={"providers": ["unknown_provider"]},
    )

    assert missing.status_code == 404
    assert invalid_side.status_code == 422
    assert invalid_provider.status_code == 422


@pytest.mark.asyncio
async def test_manual_search_query_can_be_added_and_searched_without_regeneration(client, db_session, seed_mistake):
    mistake = await seed_mistake(wrong_visual_prompt="Generated wrong prompt")

    created = await client.post(
        f"/api/mistakes/{mistake.id}/search-queries",
        json={
            "side": "wrong",
            "source_provider": "mock",
            "query_text": "manual black cabinet query",
            "language": "en",
            "results_count": 4,
        },
    )

    assert created.status_code == 201
    query = created.json()
    assert query["source_provider"] == "mock_search"
    assert query["query_text"] == "manual black cabinet query"
    assert query["results_count"] == 4

    search = await client.post(
        f"/api/mistakes/{mistake.id}/search",
        json={"sides": ["wrong"], "providers": ["mock_search"], "limit_per_query": 4},
    )

    assert search.status_code == 202
    assert search.json()[0]["payload"]["query_id"] == query["id"]
    refreshed = await db_session.get(SearchQuery, query["id"])
    assert refreshed.query_text == "manual black cabinet query"

    updated = await client.patch(
        f"/api/mistakes/{mistake.id}/search-queries/{query['id']}",
        json={"query_text": "updated manual query", "source_provider": "mock", "status": "pending"},
    )
    assert updated.status_code == 200
    assert updated.json()["query_text"] == "updated manual query"
    assert updated.json()["source_provider"] == "mock_search"

    deleted = await client.delete(f"/api/mistakes/{mistake.id}/search-queries/{query['id']}")
    assert deleted.status_code == 204
    missing = await client.patch(
        f"/api/mistakes/{mistake.id}/search-queries/{query['id']}",
        json={"query_text": "missing"},
    )
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_manual_search_query_rejects_invalid_provider_and_blank_query(client, seed_mistake):
    mistake = await seed_mistake()

    invalid_provider = await client.post(
        f"/api/mistakes/{mistake.id}/search-queries",
        json={"side": "wrong", "source_provider": "unknown", "query_text": "kitchen"},
    )
    blank_query = await client.post(
        f"/api/mistakes/{mistake.id}/search-queries",
        json={"side": "wrong", "source_provider": "mock_search", "query_text": "   "},
    )

    assert invalid_provider.status_code == 422
    assert blank_query.status_code == 422

@pytest.mark.asyncio
async def test_upsert_mistake_side_feedback(client, db_session, seed_mistake):
    mistake = await seed_mistake()

    first = await client.put(
        f"/api/mistakes/{mistake.id}/side-feedback/wrong",
        json={"feedback_text": "Reject product cards; need real kitchen context", "actor": "tester"},
    )
    second = await client.put(
        f"/api/mistakes/{mistake.id}/side-feedback/wrong",
        json={"feedback_text": "Need real kitchens without visible hood", "actor": "tester"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert second.json()["side"] == "wrong"
    assert second.json()["feedback_text"] == "Need real kitchens without visible hood"

    stored = await db_session.get(MistakeSideFeedback, second.json()["id"])
    assert stored.feedback_text == "Need real kitchens without visible hood"

