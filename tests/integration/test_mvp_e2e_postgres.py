from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

import app.services.storage_service as storage_service
from app.config import settings
from app.models.asset import FinalAsset
from app.models.job import Job
from app.services.job_runner import process_single_job


def png_1x1() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (1, 1), color="white").save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_mvp_e2e_review_rights_select_download_export(
    client,
    db_session,
    seed_mistake,
    seed_candidate,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    async def fake_fetch_image_bytes(url: str) -> bytes:
        assert url == "https://images.example.com/kitchen.png"
        return png_1x1()

    monkeypatch.setattr(storage_service, "_fetch_image_bytes", fake_fetch_image_bytes)

    mistake = await seed_mistake(title="Bad Work Triangle", short_title="Bad Work Triangle")
    candidate = await seed_candidate(
        mistake=mistake,
        side="wrong",
        source_type="manual",
        image_url="https://images.example.com/kitchen.png",
        rights_status="unknown",
        may_use_directly=False,
        storage_key_original=None,
        storage_status="pending",
    )

    for reviewer, score in [("codex", 0.82), ("antigravity", 0.78), ("claude_cli", 0.31)]:
        response = await client.put(
            f"/api/candidates/{candidate.id}/reviews/{reviewer}",
            json={"reviewer_name": reviewer, "score": score, "verdict": "pass" if score >= 0.7 else "fail"},
        )
        assert response.status_code == 200

    aggregate = await client.get(f"/api/candidates/{candidate.id}/reviews/aggregate")
    assert aggregate.status_code == 200
    assert aggregate.json()["approved_by_consensus"] is True
    assert aggregate.json()["pass_count"] == 2
    assert aggregate.json()["review_score"] == 0.78

    denied = await client.post(f"/api/candidates/{candidate.id}/select-final")
    assert denied.status_code == 403

    confirmed = await client.post(
        f"/api/candidates/{candidate.id}/confirm-rights",
        json={
            "rights_status": "manual_licensed",
            "license_note": "permission from owner",
            "license_document_ref": "mail-123",
            "comment": "owner confirmed usage",
            "actor": "e2e-test",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["may_use_directly"] is True

    selected = await client.post(f"/api/candidates/{candidate.id}/select-final")
    assert selected.status_code == 200
    selected_asset = selected.json()
    assert selected_asset["storage_status"] == "ok"
    assert selected_asset["storage_key_original"].endswith(".png")
    assert selected_asset["storage_key_processed"].endswith("processed_1920x1080.jpg")
    assert selected_asset["storage_key_thumbnail"].endswith("thumb.jpg")
    assert (storage_root / selected_asset["storage_key_original"]).exists()
    with Image.open(storage_root / selected_asset["storage_key_processed"]) as processed:
        assert processed.size == (1920, 1080)
    assert selected_asset["license_note"] == "permission from owner"
    assert selected_asset["license_document_ref"] == "mail-123"

    asset = await db_session.scalar(select(FinalAsset).where(FinalAsset.id == selected_asset["id"]))
    assert asset.candidate_id == candidate.id

    result = await process_single_job(Job(type="export_final_assets", payload={"video_id": mistake.video_id}), db_session)
    manifest_path = Path(result["manifest_path"])
    csv_path = Path(result["assets_csv_path"])
    assert manifest_path.exists()
    assert csv_path.exists()

    manifest_response = await client.get(f"/api/videos/{mistake.video_id}/manifest")
    assert manifest_response.status_code == 200
    manifest = manifest_response.json()
    assert manifest["schema_version"] == "1.0"
    exported_asset = manifest["mistakes"][0]["wrong_assets"][0]
    assert exported_asset["id"] == selected_asset["id"]
    assert exported_asset["license_note"] == "permission from owner"
    exported_file = manifest_path.parent / exported_asset["file"]
    assert exported_file.exists()
    with Image.open(exported_file) as exported_image:
        assert exported_image.size == (1920, 1080)

    csv_response = await client.get(f"/api/videos/{mistake.video_id}/assets-csv")
    assert csv_response.status_code == 200
    assert "permission from owner" in csv_response.text
    assert "mail-123" in csv_response.text
