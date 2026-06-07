import csv
import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.config import settings
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.job import Job
from app.models.mistake import Mistake
from app.models.video import Video
from app.services.export_service import export_video_manifest
from app.services.job_runner import process_single_job
from app.services.final_asset_service import select_candidate_as_final
from app.services.rights_service import confirm_candidate_rights


def _jpeg_bytes(size=(640, 360), color=(80, 120, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, color).save(output, format="JPEG")
    return output.getvalue()


@pytest.mark.asyncio
async def test_export_manifest_and_assets_csv_include_final_assets_and_rights_metadata(
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

    mistake = await seed_mistake(
        order_index=2,
        title="Bad Lighting",
        short_title="Bad Lighting",
        time_start="00:01:00",
        time_end="00:01:20",
    )
    source_file = storage_root / "final" / "source.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes())

    candidate = await seed_candidate(
        mistake=mistake,
        side="right",
        source_type="manual",
        image_url="https://images.example.com/source.jpg",
        author_name="Owner",
        rights_status="unknown",
        may_use_directly=False,
        storage_key_original="final/source.jpg",
        storage_status="ok",
    )
    await confirm_candidate_rights(
        candidate_id=candidate.id,
        db=db_session,
        rights_status="manual_licensed",
        license_note="licensed by owner",
        license_document_ref="invoice-42",
        author_name="Owner",
        comment="checked license",
        actor="pytest",
    )
    final_asset = await select_candidate_as_final(candidate.id, db_session)
    final_asset.original_exif_preserved = True
    final_asset.processed_exif_stripped = True

    excluded = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="wrong",
        source_type="manual",
        source_url="https://images.example.com/rejected.jpg",
        rights_status="unknown",
        may_use_directly=False,
        storage_status="ok",
        storage_key_original="final/rejected.jpg",
        status="rejected",
    )
    db_session.add(excluded)
    await db_session.commit()

    manifest_path = Path(await export_video_manifest(mistake.video_id, db_session))
    export_dir = manifest_path.parent

    assert manifest_path.name == "manifest.json"
    assert manifest_path.exists()
    assert (export_dir / "assets.csv").exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert manifest["video"]["id"] == mistake.video_id
    assert len(manifest["mistakes"]) == 1
    assert manifest["warnings"] == [
        {
            "code": "missing_side_final_asset",
            "mistake_id": mistake.id,
            "order_index": 2,
            "side": "wrong",
            "message": "Mistake is missing an exportable wrong final asset",
        }
    ]

    exported_mistake = manifest["mistakes"][0]
    assert exported_mistake["order_index"] == 2
    assert exported_mistake["wrong_assets"] == []
    assert len(exported_mistake["right_assets"]) == 1

    exported_asset = exported_mistake["right_assets"][0]
    assert exported_asset["id"] == final_asset.id
    assert exported_asset["source_type"] == "manual"
    assert exported_asset["rights_status"] == "manual_licensed"
    assert exported_asset["license_note"] == "licensed by owner"
    assert exported_asset["license_document_ref"] == "invoice-42"
    assert exported_asset["author_name"] == "Owner"
    assert exported_asset["original_exif_preserved"] is True
    assert exported_asset["processed_exif_stripped"] is True
    with Image.open(export_dir / exported_asset["file"]) as exported_image:
        assert exported_image.size == (1920, 1080)

    export_event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "video.exported", AuditEvent.entity_id == mistake.video_id)
    )
    assert export_event is not None
    assert export_event.after["schema_version"] == "1.0"
    assert export_event.after["asset_ids"] == [final_asset.id]
    assert export_event.after["asset_count"] == 1
    assert export_event.after["warning_count"] == 1

    await db_session.refresh(final_asset)
    assert final_asset.status == "exported"

    with open(export_dir / "assets.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))

    assert len(rows) == 1
    assert rows[0]["schema_version"] == "1.0"
    assert rows[0]["video_id"] == str(mistake.video_id)
    assert rows[0]["mistake_id"] == str(mistake.id)
    assert rows[0]["side"] == "right"
    assert rows[0]["asset_id"] == str(final_asset.id)
    assert rows[0]["file"] == exported_asset["file"]
    assert rows[0]["license_note"] == "licensed by owner"
    assert rows[0]["license_document_ref"] == "invoice-42"


@pytest.mark.asyncio
async def test_export_fails_when_final_asset_file_is_missing(
    db_session,
    seed_mistake,
    seed_candidate,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "export_root", tmp_path / "exports")

    mistake = await seed_mistake()
    candidate = await seed_candidate(
        mistake=mistake,
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="missing/source.jpg",
        storage_status="ok",
    )
    await select_candidate_as_final(candidate.id, db_session)

    with pytest.raises(ValueError, match="file is missing"):
        await export_video_manifest(mistake.video_id, db_session)

    event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "storage.missing_file_detected")
    )
    assert event is not None
    assert event.entity_type == "final_asset"
    assert event.after["video_id"] == mistake.video_id
    assert event.after["storage_key"] == "missing/source.jpg"

@pytest.mark.asyncio
async def test_export_api_returns_latest_manifest_and_assets_csv(
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

    mistake = await seed_mistake(title="Storage Problem", short_title="Storage Problem")
    source_file = storage_root / "ready" / "asset.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(color=(40, 90, 140)))

    candidate = await seed_candidate(
        mistake=mistake,
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="ready/asset.jpg",
        storage_status="ok",
    )
    await select_candidate_as_final(candidate.id, db_session)
    await export_video_manifest(mistake.video_id, db_session)

    manifest_response = await client.get(f"/api/videos/{mistake.video_id}/manifest")
    assert manifest_response.status_code == 200
    assert manifest_response.json()["schema_version"] == "1.0"

    csv_response = await client.get(f"/api/videos/{mistake.video_id}/assets-csv")
    assert csv_response.status_code == 200
    assert csv_response.text.startswith("schema_version,video_id,mistake_id")
    assert "ready" not in csv_response.text

@pytest.mark.asyncio
async def test_export_job_result_includes_export_package_paths(
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

    mistake = await seed_mistake()
    source_file = storage_root / "job" / "asset.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(color=(20, 70, 120)))

    candidate = await seed_candidate(
        mistake=mistake,
        may_use_directly=True,
        rights_status="manual_licensed",
        storage_key_original="job/asset.jpg",
        storage_status="ok",
    )
    await select_candidate_as_final(candidate.id, db_session)

    result = await process_single_job(Job(type="export_final_assets", payload={"video_id": mistake.video_id}), db_session)

    assert Path(result["export_dir"]).exists()
    assert Path(result["manifest_path"]).exists()
    assert Path(result["assets_csv_path"]).exists()
    assert Path(result["manifest_path"]).parent == Path(result["assets_csv_path"]).parent



@pytest.mark.asyncio
async def test_export_fails_when_no_assets_meet_rights_policy(
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    mistake = await seed_mistake(title="Unknown Rights", short_title="Unknown Rights")
    source_file = storage_root / "unsafe" / "asset.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(color=(200, 20, 20)))

    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="manual",
            source_url="https://images.example.com/unsafe.jpg",
            rights_status="unknown",
            may_use_directly=True,
            storage_status="ok",
            storage_key_original="unsafe/asset.jpg",
            status="approved",
        )
    )
    await db_session.commit()

    with pytest.raises(ValueError, match="no exportable final assets"):
        await export_video_manifest(mistake.video_id, db_session)

    assert not list(export_root.glob("*"))


@pytest.mark.asyncio
async def test_export_rejects_storage_key_path_escape(
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setattr(settings, "storage_root", tmp_path / "storage")
    monkeypatch.setattr(settings, "export_root", tmp_path / "exports")

    mistake = await seed_mistake(title="Path Escape", short_title="Path Escape")
    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="manual",
            source_url="https://images.example.com/escape.jpg",
            rights_status="manual_licensed",
            may_use_directly=True,
            storage_status="ok",
            storage_key_processed="../outside.jpg",
            status="approved",
        )
    )
    await db_session.commit()

    with pytest.raises(ValueError, match="Invalid storage key"):
        await export_video_manifest(mistake.video_id, db_session)


@pytest.mark.asyncio
async def test_export_late_copy_failure_leaves_no_package_directory(
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    mistake = await seed_mistake(title="Atomic Export", short_title="Atomic Export")
    source_file = storage_root / "processed" / "atomic.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(size=(1920, 1080), color=(10, 20, 30)))

    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="manual",
            source_url="https://images.example.com/atomic.jpg",
            rights_status="manual_licensed",
            may_use_directly=True,
            storage_status="ok",
            storage_key_processed="processed/atomic.jpg",
            status="approved",
        )
    )
    await db_session.commit()

    def fail_copy(*args, **kwargs):
        raise OSError("simulated copy failure")

    monkeypatch.setattr("app.services.export_service.shutil.copy2", fail_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        await export_video_manifest(mistake.video_id, db_session)

    assert export_root.exists()
    assert list(export_root.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_slug", ["../escaped", "/tmp/escaped"])
async def test_export_with_legacy_unsafe_db_slug_stays_inside_export_root(
    db_session,
    tmp_path,
    monkeypatch,
    legacy_slug,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    video = Video(title=f"Legacy Unsafe Slug {legacy_slug}", slug=legacy_slug)
    db_session.add(video)
    await db_session.flush()
    mistake = Mistake(video_id=video.id, order_index=1, title="Unsafe Slug Mistake", short_title="Unsafe")
    db_session.add(mistake)
    await db_session.flush()

    source_file = storage_root / "processed" / "legacy.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(size=(1920, 1080), color=(100, 120, 140)))
    db_session.add(
        FinalAsset(
            video_id=video.id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="manual",
            source_url="https://images.example.com/legacy.jpg",
            rights_status="manual_licensed",
            may_use_directly=True,
            storage_status="ok",
            storage_key_processed="processed/legacy.jpg",
            status="approved",
        )
    )
    await db_session.commit()

    manifest_path = Path(await export_video_manifest(video.id, db_session))

    assert manifest_path.exists()
    assert manifest_path.resolve().is_relative_to(export_root.resolve())
    assert manifest_path.parent.name.startswith("escaped_") or manifest_path.parent.name.startswith("tmp-escaped_")
    assert not (tmp_path / "escaped").exists()
    assert not (tmp_path / "tmp" / "escaped").exists()


@pytest.mark.asyncio
async def test_export_generates_processed_file_from_original_before_copy(
    db_session,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    mistake = await seed_mistake(title="Needs Processing", short_title="Needs Processing")
    source_file = storage_root / "originals" / "asset.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(size=(640, 360), color=(10, 80, 130)))

    asset = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="wrong",
        source_type="manual",
        source_url="https://images.example.com/original.jpg",
        rights_status="manual_licensed",
        may_use_directly=True,
        storage_status="ok",
        storage_key_original="originals/asset.jpg",
        status="approved",
    )
    db_session.add(asset)
    await db_session.commit()

    manifest_path = Path(await export_video_manifest(mistake.video_id, db_session))
    await db_session.refresh(asset)

    assert asset.storage_key_processed.endswith("processed_1920x1080.jpg")
    processed_path = storage_root / asset.storage_key_processed
    assert processed_path.exists()
    with Image.open(processed_path) as processed:
        assert processed.size == (1920, 1080)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exported_file = manifest_path.parent / manifest["mistakes"][0]["wrong_assets"][0]["file"]
    with Image.open(exported_file) as exported:
        assert exported.size == (1920, 1080)


@pytest.mark.asyncio
async def test_export_manifest_warns_for_missing_final_asset_sides(
    db_session,
    seed_video,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    monkeypatch.setattr(settings, "export_root", export_root)

    video = await seed_video()
    first = await seed_mistake(video=video, order_index=1, title="Only Wrong", short_title="Only Wrong")
    second = await seed_mistake(video=video, order_index=2, title="No Assets", short_title="No Assets")
    source_file = storage_root / "processed" / "wrong.jpg"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(_jpeg_bytes(size=(1920, 1080), color=(120, 20, 20)))

    db_session.add(
        FinalAsset(
            video_id=first.video_id,
            mistake_id=first.id,
            side="wrong",
            source_type="manual",
            source_url="https://images.example.com/wrong.jpg",
            rights_status="manual_licensed",
            may_use_directly=True,
            storage_status="ok",
            storage_key_processed="processed/wrong.jpg",
            status="approved",
        )
    )
    await db_session.commit()

    manifest_path = Path(await export_video_manifest(first.video_id, db_session))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert manifest["warnings"] == [
        {
            "code": "missing_side_final_asset",
            "mistake_id": first.id,
            "order_index": 1,
            "side": "right",
            "message": "Mistake is missing an exportable right final asset",
        },
        {
            "code": "missing_final_assets",
            "mistake_id": second.id,
            "order_index": 2,
            "message": "Mistake has no exportable final assets",
        },
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_slug", ["*", "../outside"])
async def test_latest_export_lookup_uses_safe_legacy_db_slug(
    client,
    db_session,
    tmp_path,
    monkeypatch,
    legacy_slug,
):
    export_root = tmp_path / "exports"
    monkeypatch.setattr(settings, "export_root", export_root)

    other_dir = export_root / "other-video_20990101_000000"
    other_dir.mkdir(parents=True)
    (other_dir / "manifest.json").write_text('{"schema_version":"1.0","video":{"id":999}}', encoding="utf-8")
    (other_dir / "assets.csv").write_text("schema_version,video_id\n1.0,999\n", encoding="utf-8")

    video = Video(title=f"Legacy Glob Slug {legacy_slug}", slug=legacy_slug)
    db_session.add(video)
    await db_session.commit()
    await db_session.refresh(video)

    manifest_response = await client.get(f"/api/videos/{video.id}/manifest")
    csv_response = await client.get(f"/api/videos/{video.id}/assets-csv")

    assert manifest_response.status_code == 404
    assert csv_response.status_code == 404
