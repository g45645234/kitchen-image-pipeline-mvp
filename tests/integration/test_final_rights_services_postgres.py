from datetime import datetime, timezone
from io import BytesIO
import pytest
from PIL import Image
from sqlalchemy import func, select

from app.config import settings
from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.job import Job
from app.services.final_asset_service import select_candidate_as_final
from app.services.job_runner import process_single_job
from app.services.rights_service import confirm_candidate_rights


def _jpeg_bytes(size=(640, 360), color=(80, 120, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_final_select_requires_confirmed_rights(db_session, seed_candidate):
    candidate = await seed_candidate(may_use_directly=False, rights_status="unknown")

    with pytest.raises(PermissionError):
        await select_candidate_as_final(candidate.id, db_session)


@pytest.mark.asyncio
async def test_confirm_rights_writes_candidate_and_audit(db_session, seed_candidate):
    candidate = await seed_candidate(may_use_directly=False, rights_status="unknown")

    updated = await confirm_candidate_rights(
        candidate_id=candidate.id,
        db=db_session,
        rights_status="manual_licensed",
        license_note="licensed by owner",
        license_document_ref="invoice-1",
        author_name="Owner",
        comment="confirmed with owner",
        actor="tester",
    )

    assert updated.rights_status == "manual_licensed"
    assert updated.may_use_directly is True
    assert updated.author_name == "Owner"

    event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "rights_confirmed"))
    assert event.actor == "tester"
    assert event.before["rights_status"] == "unknown"
    assert event.after["rights_status"] == "manual_licensed"
    assert event.after["license_note"] == "licensed by owner"
    assert event.comment == "confirmed with owner"

    spec_event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "candidate.rights_confirmed"))
    assert spec_event is not None
    assert spec_event.actor == "tester"
    assert spec_event.after["rights_status"] == "manual_licensed"
    assert spec_event.after["license_note"] == "licensed by owner"


@pytest.mark.asyncio
async def test_select_candidate_as_final_creates_and_replaces_per_side(db_session, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    first = await seed_candidate(
        mistake=mistake,
        side="right",
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="final/first.jpg",
        storage_status="ok",
    )
    second = await seed_candidate(
        mistake=mistake,
        side="right",
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="final/second.jpg",
        storage_status="ok",
    )

    first_asset = await select_candidate_as_final(first.id, db_session)
    second_asset = await select_candidate_as_final(second.id, db_session)

    assert second_asset.id == first_asset.id
    assert second_asset.candidate_id == second.id
    assert second_asset.storage_key_original == "final/second.jpg"

    count = await db_session.scalar(select(func.count()).select_from(FinalAsset))
    assert count == 1

    refreshed_first = await db_session.get(type(first), first.id)
    refreshed_second = await db_session.get(type(second), second.id)
    assert refreshed_first.status == "new"
    assert refreshed_second.status == "approved_final"


@pytest.mark.asyncio
async def test_select_candidates_on_different_sides_create_separate_assets(db_session, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    wrong = await seed_candidate(mistake=mistake, side="wrong", may_use_directly=True, rights_status="manual_licensed", storage_key_original="wrong.jpg", storage_status="ok")
    right = await seed_candidate(mistake=mistake, side="right", may_use_directly=True, rights_status="manual_licensed", storage_key_original="right.jpg", storage_status="ok")

    await select_candidate_as_final(wrong.id, db_session)
    await select_candidate_as_final(right.id, db_session)

    count = await db_session.scalar(select(func.count()).select_from(FinalAsset))
    assert count == 2

@pytest.mark.asyncio
async def test_replacing_final_candidate_clears_previous_license_metadata(db_session, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    first = await seed_candidate(
        mistake=mistake,
        side="right",
        may_use_directly=False,
        rights_status="unknown",
        storage_key_original="first.jpg",
        storage_status="ok",
    )
    second = await seed_candidate(
        mistake=mistake,
        side="right",
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="second.jpg",
        storage_status="ok",
    )

    await confirm_candidate_rights(
        candidate_id=first.id,
        db=db_session,
        rights_status="manual_licensed",
        license_note="paid stock license",
        license_document_ref="invoice-99",
        comment="license checked",
        actor="tester",
    )
    first_asset = await select_candidate_as_final(first.id, db_session)
    assert first_asset.license_note == "paid stock license"
    assert first_asset.license_document_ref == "invoice-99"
    assert first_asset.rights_confirmed_by == "tester"

    second_asset = await select_candidate_as_final(second.id, db_session)

    assert second_asset.id == first_asset.id
    assert second_asset.candidate_id == second.id
    assert second_asset.storage_key_original == "second.jpg"
    assert second_asset.license_note is None
    assert second_asset.license_document_ref is None
    assert second_asset.rights_confirmed_by is None
    assert second_asset.rights_confirmed_at is None



@pytest.mark.asyncio
async def test_select_candidate_as_final_rejects_deleted_mistake_or_video(
    db_session, seed_video, seed_mistake, seed_candidate
):
    deleted_at = datetime.now(timezone.utc)

    deleted_mistake = await seed_mistake()
    candidate_for_deleted_mistake = await seed_candidate(
        mistake=deleted_mistake,
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="deleted/mistake.jpg",
        storage_status="ok",
    )
    deleted_mistake.deleted_at = deleted_at
    await db_session.commit()

    with pytest.raises(ValueError, match="Mistake"):
        await select_candidate_as_final(candidate_for_deleted_mistake.id, db_session)

    deleted_video = await seed_video()
    live_mistake_deleted_video = await seed_mistake(video=deleted_video)
    candidate_for_deleted_video = await seed_candidate(
        mistake=live_mistake_deleted_video,
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="deleted/video.jpg",
        storage_status="ok",
    )
    deleted_video.deleted_at = deleted_at
    await db_session.commit()

    with pytest.raises(ValueError, match="Video"):
        await select_candidate_as_final(candidate_for_deleted_video.id, db_session)


@pytest.mark.asyncio
async def test_select_final_replacement_enqueues_cleanup_for_old_derivatives(
    db_session,
    seed_mistake,
    seed_candidate,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    mistake = await seed_mistake()
    first_original = storage_root / "candidate" / "first.jpg"
    first_original.parent.mkdir(parents=True)
    first_original.write_bytes(_jpeg_bytes(color=(10, 20, 30)))
    second_processed = storage_root / "candidate" / "second_processed.jpg"
    second_processed.write_bytes(_jpeg_bytes(size=(1920, 1080), color=(50, 60, 70)))

    first = await seed_candidate(
        mistake=mistake,
        side="wrong",
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="candidate/first.jpg",
        storage_status="ok",
    )
    second = await seed_candidate(
        mistake=mistake,
        side="wrong",
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="candidate/second_original.jpg",
        storage_key_processed="candidate/second_processed.jpg",
        storage_status="ok",
    )

    first_asset = await select_candidate_as_final(first.id, db_session)
    old_derivative_keys = {
        first_asset.storage_key_thumbnail,
        first_asset.storage_key_processed,
        first_asset.metadata_storage_key,
    }
    assert all((storage_root / key).exists() for key in old_derivative_keys)

    second_asset = await select_candidate_as_final(second.id, db_session)
    assert second_asset.id == first_asset.id
    assert second_asset.candidate_id == second.id
    assert second_asset.storage_key_processed == "candidate/second_processed.jpg"

    result = await db_session.execute(
        select(Job).where(Job.idempotency_key == f"cleanup_storage:replace_final_asset:{second_asset.id}")
    )
    cleanup_job = result.scalars().first()
    assert cleanup_job is not None
    for key in old_derivative_keys:
        assert key in cleanup_job.payload["old_storage_keys"]

    unrelated_orphan = storage_root / "unrelated" / "orphan.tmp"
    unrelated_orphan.parent.mkdir(parents=True)
    unrelated_orphan.write_bytes(b"keep-me")

    cleanup_result = await process_single_job(cleanup_job, db_session)
    assert cleanup_result["mode"] == "targeted"
    for key in old_derivative_keys:
        assert not (storage_root / key).exists()
    assert first_original.exists()
    assert second_processed.exists()
    assert unrelated_orphan.exists()

@pytest.mark.asyncio
async def test_confirm_final_asset_rights_api_updates_rights_and_audit_without_touching_storage(
    client, db_session, seed_mistake
):
    mistake = await seed_mistake()
    asset = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="wrong",
        source_type="search",
        rights_status="unknown",
        may_use_directly=False,
        storage_status="ok",
        status="approved",
        storage_key_original="legacy/original.jpg",
        storage_key_processed=None,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)

    empty_comment = await client.post(
        f"/api/final-assets/{asset.id}/confirm-rights",
        json={"rights_status": "manual_licensed", "comment": ""},
    )
    assert empty_comment.status_code == 422

    response = await client.post(
        f"/api/final-assets/{asset.id}/confirm-rights",
        json={
            "rights_status": "manual_licensed",
            "source_url": "https://license.example/source",
            "license_note": "licensed final asset",
            "license_document_ref": "invoice-legacy-1",
            "author_name": "Kitchen Owner",
            "comment": "license checked",
            "actor": "api-test",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["rights_status"] == "manual_licensed"
    assert body["may_use_directly"] is True
    assert body["source_url"] == "https://license.example/source"
    assert body["license_note"] == "licensed final asset"
    assert body["license_document_ref"] == "invoice-legacy-1"
    assert body["author_name"] == "Kitchen Owner"
    assert body["rights_confirmed_by"] == "api-test"
    assert body["storage_key_original"] == "legacy/original.jpg"
    assert body["storage_key_processed"] is None

    event = await db_session.scalar(select(AuditEvent).where(AuditEvent.action == "final_asset.rights_confirmed"))
    assert event is not None
    assert event.actor == "api-test"
    assert event.entity_type == "final_asset"
    assert event.entity_id == asset.id
    assert event.before["rights_status"] == "unknown"
    assert event.after["rights_status"] == "manual_licensed"
    assert event.comment == "license checked"


@pytest.mark.asyncio
async def test_confirm_final_asset_rights_api_rejects_rejected_asset(client, db_session, seed_mistake):
    mistake = await seed_mistake()
    asset = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="right",
        source_type="search",
        rights_status="unknown",
        may_use_directly=False,
        storage_status="cleanup_pending",
        status="rejected",
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)

    response = await client.post(
        f"/api/final-assets/{asset.id}/confirm-rights",
        json={"rights_status": "manual_licensed", "comment": "license checked"},
    )

    assert response.status_code == 422
