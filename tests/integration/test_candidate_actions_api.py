import pytest
from sqlalchemy import func, select

import app.services.storage_service as storage_service

from app.models.audit import AuditEvent, BlockedDomain
from app.models.asset import FinalAsset
from app.services.storage_service import StorageDownloadError


@pytest.mark.asyncio
async def test_get_candidate_by_id(client, seed_candidate):
    candidate = await seed_candidate(source_provider="mock_search", rights_status="unknown")

    response = await client.get(f"/api/candidates/{candidate.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == candidate.id
    assert body["mistake_id"] == candidate.mistake_id
    assert body["source_provider"] == "mock_search"


@pytest.mark.asyncio
async def test_candidate_original_serves_downloaded_local_file(client, seed_candidate, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    storage_key = "candidates/full.jpg"
    file_path = tmp_path / storage_key
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"full-image-bytes")
    candidate = await seed_candidate(storage_key_original=storage_key, storage_status="ok")

    response = await client.get(f"/api/candidates/{candidate.id}/original")

    assert response.status_code == 200
    assert response.content == b"full-image-bytes"
    assert "candidate_" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_candidate_original_missing_file_returns_404(client, seed_candidate, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "storage_root", tmp_path)
    candidate = await seed_candidate(storage_key_original="candidates/missing.jpg", storage_status="ok")

    response = await client.get(f"/api/candidates/{candidate.id}/original")

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_review_approve_final_denies_unlicensed_candidate(client, db_session, seed_candidate):
    candidate = await seed_candidate(may_use_directly=False, rights_status="unknown")

    response = await client.post(
        f"/api/candidates/{candidate.id}/review",
        json={"action": "approve_final", "comment": "try final"},
    )

    assert response.status_code == 403
    job_count = await db_session.scalar(select(func.count()).select_from(FinalAsset))
    assert job_count == 0


@pytest.mark.asyncio
async def test_review_approve_reference_matches_spec_and_worker(client, db_session, seed_candidate):
    from app.services.job_runner import fetch_and_run_one_job

    candidate = await seed_candidate(status="review")

    response = await client.post(
        f"/api/candidates/{candidate.id}/review",
        json={"action": "approve_reference", "comment": "useful reference"},
    )

    assert response.status_code == 202
    assert response.json()["type"] == "review_candidate"
    assert response.json()["payload"]["action"] == "approve_reference"

    job = await fetch_and_run_one_job(db_session)
    assert job.status == "completed"

    await db_session.refresh(candidate)
    assert candidate.status == "approved_reference"
    assert candidate.usage_role == "reference_only"
    assert float(candidate.reference_priority_score) == 1.0
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "candidate.approved_reference", AuditEvent.entity_id == candidate.id)
    )
    assert audit is not None
    assert audit.after["status"] == "approved_reference"


@pytest.mark.asyncio
async def test_review_reject_uses_reject_reason_and_is_idempotent(client, db_session, seed_candidate):
    from app.services.job_runner import fetch_and_run_one_job

    candidate = await seed_candidate(status="review")
    payload = {"action": "reject", "reject_reason": "bad_quality", "comment": "blurred"}

    first = await client.post(f"/api/candidates/{candidate.id}/review", json=payload)
    second = await client.post(f"/api/candidates/{candidate.id}/review", json=payload)

    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["id"] == first.json()["id"]
    assert first.json()["payload"]["reject_reason"] == "bad_quality"
    assert first.json()["idempotency_key"].startswith(f"review_candidate:{candidate.id}:reject:")

    job = await fetch_and_run_one_job(db_session)
    assert job.status == "completed"
    audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "candidate.rejected", AuditEvent.entity_id == candidate.id)
    )
    assert audit is not None
    assert audit.after["reject_reason"] == "bad_quality"


@pytest.mark.asyncio
async def test_confirm_rights_then_select_final_api(client, db_session, seed_candidate):
    candidate = await seed_candidate(
        may_use_directly=False,
        rights_status="unknown",
        storage_key_original="ready.jpg",
        storage_status="ok",
    )

    denied = await client.post(f"/api/candidates/{candidate.id}/select-final")
    assert denied.status_code == 403

    empty_comment = await client.post(
        f"/api/candidates/{candidate.id}/confirm-rights",
        json={"rights_status": "manual_licensed", "comment": ""},
    )
    assert empty_comment.status_code == 422

    confirmed = await client.post(
        f"/api/candidates/{candidate.id}/confirm-rights",
        json={
            "rights_status": "manual_licensed",
            "license_note": "licensed",
            "license_document_ref": "doc-1",
            "comment": "confirmed",
            "actor": "api-test",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["may_use_directly"] is True

    selected = await client.post(f"/api/candidates/{candidate.id}/select-final")
    assert selected.status_code == 200
    asset = selected.json()
    approved_audit = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "candidate.approved_final", AuditEvent.entity_id == candidate.id)
    )
    assert approved_audit is not None
    assert approved_audit.after["status"] == "approved_final"
    assert asset["candidate_id"] == candidate.id
    assert asset["license_note"] == "licensed"
    assert asset["license_document_ref"] == "doc-1"
    assert asset["rights_confirmed_by"] == "api-test"

    event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "rights_confirmed"))
    assert event.comment == "confirmed"
    spec_event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "candidate.rights_confirmed"))
    assert spec_event is not None
    assert spec_event.after["rights_status"] == "manual_licensed"


@pytest.mark.asyncio
async def test_reference_and_block_domain_api(client, db_session, seed_candidate):
    reference_candidate = await seed_candidate(domain="reference.example")
    ref = await client.post(
        f"/api/candidates/{reference_candidate.id}/use-as-reference",
        json={"mark_high_value": True, "comment": "useful reference", "actor": "api-test"},
    )
    assert ref.status_code == 200
    assert ref.json()["status"] == "approved_reference"
    assert ref.json()["usage_role"] == "reference_only"

    block_candidate = await seed_candidate(domain="Blocked.Example.")
    sibling_candidate = await seed_candidate(domain="blocked.example", image_url_hash="blocked-sibling")
    approved_candidate = await seed_candidate(domain="blocked.example", status="approved_final", image_url_hash="blocked-approved")
    blocked = await client.post(
        f"/api/candidates/{block_candidate.id}/block-domain",
        json={"reason": "bad source", "actor": "api-test"},
    )
    assert blocked.status_code == 200
    assert blocked.json()["domain"] == "blocked.example"

    domain = await db_session.scalar(select(BlockedDomain).where(BlockedDomain.domain == "blocked.example"))
    assert domain.reason == "bad source"

    await db_session.refresh(block_candidate)
    await db_session.refresh(sibling_candidate)
    await db_session.refresh(approved_candidate)
    assert block_candidate.status == "rejected"
    assert block_candidate.reject_reason == "blocked_domain"
    assert block_candidate.domain == "blocked.example"
    assert sibling_candidate.status == "auto_rejected"
    assert sibling_candidate.reject_reason == "blocked_domain"
    assert approved_candidate.status == "approved_final"

    audit_actions = [row[0] for row in (await db_session.execute(select(AuditEvent.action).order_by(AuditEvent.id))).all()]
    assert "reference_marked" in audit_actions
    assert "candidate.approved_reference" in audit_actions
    assert "domain_blocked" in audit_actions
    assert "candidate.domain_blocked" in audit_actions


@pytest.mark.asyncio
async def test_legacy_review_action_validation(client, seed_candidate):
    candidate = await seed_candidate()

    invalid = await client.post(
        f"/api/candidates/{candidate.id}/review",
        json={"action": "reference_only", "reason": "bad"},
    )
    assert invalid.status_code == 422

    valid = await client.post(
        f"/api/candidates/{candidate.id}/review",
        json={"action": "reject", "reason": "bad_quality"},
    )
    assert valid.status_code == 202
    assert valid.json()["type"] == "review_candidate"


@pytest.mark.asyncio
async def test_selecting_second_final_reuses_asset_row(client, db_session, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    first = await seed_candidate(mistake=mistake, side="wrong", may_use_directly=True, rights_status="manual_licensed", storage_key_original="first.jpg", storage_status="ok")
    second = await seed_candidate(mistake=mistake, side="wrong", may_use_directly=True, rights_status="manual_licensed", storage_key_original="second.jpg", storage_status="ok")

    first_response = await client.post(f"/api/candidates/{first.id}/select-final")
    second_response = await client.post(f"/api/candidates/{second.id}/select-final")

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert second_response.json()["id"] == first_response.json()["id"]

    rows = (await db_session.execute(select(FinalAsset))).scalars().all()
    assert len(rows) == 1
    assert rows[0].candidate_id == second.id

@pytest.mark.asyncio
async def test_select_final_download_failure_does_not_persist_final_asset(client, db_session, seed_candidate, monkeypatch):
    async def fail_fetch_image_bytes(url: str) -> bytes:
        raise StorageDownloadError("download failed")

    monkeypatch.setattr(storage_service, "_fetch_image_bytes", fail_fetch_image_bytes)

    candidate = await seed_candidate(
        may_use_directly=False,
        rights_status="unknown",
        image_url="https://images.example.com/fail.png",
        storage_key_original=None,
        storage_status="pending",
    )
    candidate_id = candidate.id
    candidate_type = type(candidate)

    confirmed = await client.post(
        f"/api/candidates/{candidate_id}/confirm-rights",
        json={
            "rights_status": "manual_licensed",
            "license_note": "licensed",
            "comment": "confirmed",
            "actor": "api-test",
        },
    )
    assert confirmed.status_code == 200

    selected = await client.post(f"/api/candidates/{candidate_id}/select-final")
    assert selected.status_code == 502

    final_count = await db_session.scalar(select(func.count()).select_from(FinalAsset))
    assert final_count == 0

    refreshed = await db_session.get(candidate_type, candidate_id)
    assert refreshed.status == "new"
    assert refreshed.storage_status == "pending"
    assert refreshed.storage_key_original is None



@pytest.mark.asyncio
async def test_candidates_are_paginated(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    first = await seed_candidate(mistake=mistake, side="wrong")
    second = await seed_candidate(mistake=mistake, side="right")
    third = await seed_candidate(mistake=mistake, side="wrong")

    page_one = await client.get(f"/api/mistakes/{mistake.id}/candidates?limit=2&skip=0")
    assert page_one.status_code == 200
    assert [item["id"] for item in page_one.json()] == [first.id, second.id]

    page_two = await client.get(f"/api/mistakes/{mistake.id}/candidates?limit=2&skip=2")
    assert page_two.status_code == 200
    assert [item["id"] for item in page_two.json()] == [third.id]

@pytest.mark.asyncio
async def test_candidates_support_spec_filters_offset_and_sort(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    await seed_candidate(
        mistake=mistake,
        side="wrong",
        status="review",
        rights_status="unknown",
        source_provider="mock",
        review_score=0.2,
        original_width=800,
        original_height=600,
    )
    best = await seed_candidate(
        mistake=mistake,
        side="wrong",
        status="review",
        rights_status="manual_licensed",
        source_provider="mock",
        review_score=0.9,
        original_width=1920,
        original_height=1080,
    )
    filtered_out = await seed_candidate(
        mistake=mistake,
        side="right",
        status="rejected",
        rights_status="manual_licensed",
        source_provider="other",
        review_score=1.0,
    )

    response = await client.get(
        f"/api/mistakes/{mistake.id}/candidates"
        "?side=wrong&status=review&rights_status=manual_licensed&source_provider=mock"
        "&sort=-review_score&limit=10&offset=0"
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [best.id]
    assert filtered_out.id not in [item["id"] for item in response.json()]


@pytest.mark.asyncio
async def test_candidates_offset_alias_matches_legacy_skip(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    first = await seed_candidate(mistake=mistake, side="wrong")
    second = await seed_candidate(mistake=mistake, side="wrong")

    response = await client.get(f"/api/mistakes/{mistake.id}/candidates?limit=1&offset=1")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [second.id]


@pytest.mark.asyncio
async def test_candidates_status_filter_accepts_spec_values_and_legacy_aliases(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    final_candidate = await seed_candidate(mistake=mistake, status="approved_final", image_url_hash="spec-final")
    legacy_final = await seed_candidate(mistake=mistake, status="approved", image_url_hash="legacy-final")
    reference_candidate = await seed_candidate(mistake=mistake, status="approved_reference", image_url_hash="spec-reference")
    legacy_reference = await seed_candidate(mistake=mistake, status="reference_only", image_url_hash="legacy-reference")
    rejected = await seed_candidate(mistake=mistake, status="rejected", image_url_hash="rejected-status")

    final_response = await client.get(f"/api/mistakes/{mistake.id}/candidates?status=approved_final&sort=id")
    reference_response = await client.get(f"/api/mistakes/{mistake.id}/candidates?status=approved_reference&sort=id")

    assert final_response.status_code == 200
    assert [item["id"] for item in final_response.json()] == [final_candidate.id, legacy_final.id]
    assert rejected.id not in [item["id"] for item in final_response.json()]

    assert reference_response.status_code == 200
    assert [item["id"] for item in reference_response.json()] == [reference_candidate.id, legacy_reference.id]


@pytest.mark.asyncio
async def test_candidates_reject_unknown_sort(client, seed_mistake):
    mistake = await seed_mistake()

    response = await client.get(f"/api/mistakes/{mistake.id}/candidates?sort=unsupported")

    assert response.status_code == 422
