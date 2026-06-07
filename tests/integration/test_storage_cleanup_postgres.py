from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.services.storage_service import cleanup_storage, cleanup_storage_targets


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.mark.asyncio
async def test_cleanup_storage_dry_run_reports_orphans_missing_and_invalid_refs(
    db_session,
    seed_candidate,
    seed_mistake,
    tmp_path,
    monkeypatch,
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)

    present_candidate = _write(storage_root / "candidates" / "present.jpg", b"candidate")
    present_final = _write(storage_root / "final" / "present.jpg", b"final")
    orphan = _write(storage_root / "orphans" / "unused.tmp", b"orphan")

    mistake = await seed_mistake()
    await seed_candidate(
        mistake=mistake,
        storage_key_original="candidates/present.jpg",
        storage_key_thumbnail="candidates/missing-thumb.jpg",
        storage_key_processed="../escape.jpg",
        storage_status="ok",
    )
    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="right",
            source_type="manual_licensed",
            source_url="https://example.com/final.jpg",
            rights_status="manual_licensed",
            may_use_directly=True,
            storage_key_original="final/present.jpg",
            metadata_storage_key="final/missing-metadata.json",
            status="approved",
        )
    )
    await db_session.commit()

    result = await cleanup_storage(dry_run=True, db=db_session)

    assert result["dry_run"] is True
    assert result["referenced_files_count"] == 4
    assert result["files_on_disk_count"] == 3
    assert result["orphan_files_count"] == 1
    assert result["orphan_files"] == [{"storage_key": "orphans/unused.tmp", "bytes": len(b"orphan")}]
    assert result["orphan_bytes"] == len(b"orphan")
    assert result["deleted_files_count"] == 0
    assert result["freed_bytes"] == 0
    assert {item["storage_key"] for item in result["missing_files"]} == {
        "candidates/missing-thumb.jpg",
        "final/missing-metadata.json",
    }
    assert result["invalid_references_count"] == 1
    assert result["invalid_references"][0]["storage_key"] == "../escape.jpg"

    audit_actions = [
        row[0]
        for row in (await db_session.execute(select(AuditEvent.action).order_by(AuditEvent.id))).all()
    ]
    assert "storage.cleanup_started" in audit_actions
    assert "storage.missing_file_detected" in audit_actions
    assert "storage.cleanup_finished" in audit_actions
    finished_event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "storage.cleanup_finished").order_by(AuditEvent.id.desc())
    )
    assert finished_event.after["orphan_files_count"] == 1
    assert finished_event.after["missing_files_count"] == 2

    assert present_candidate.exists()
    assert present_final.exists()
    assert orphan.exists()


@pytest.mark.asyncio
async def test_cleanup_storage_delete_removes_only_orphan_files(db_session, tmp_path, monkeypatch):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    orphan = _write(storage_root / "orphans" / "unused.tmp", b"delete-me")

    result = await cleanup_storage(dry_run=False, db=db_session)

    assert result["dry_run"] is False
    assert result["orphan_files_count"] == 1
    assert result["deleted_files_count"] == 1
    assert result["freed_bytes"] == len(b"delete-me")
    finished_event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "storage.cleanup_finished").order_by(AuditEvent.id.desc())
    )
    assert finished_event.after["deleted_files_count"] == 1
    assert finished_event.after["freed_bytes"] == len(b"delete-me")
    assert not orphan.exists()


@pytest.mark.asyncio
async def test_cleanup_storage_targets_deletes_only_payload_keys_not_unrelated_orphans(
    db_session, tmp_path, monkeypatch
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    target = _write(storage_root / "targets" / "delete-me.tmp", b"target")
    unrelated = _write(storage_root / "unrelated" / "keep-me.tmp", b"keep")

    result = await cleanup_storage_targets(
        {"dry_run": False, "mode": "targeted", "reason": "pytest", "old_storage_keys": ["targets/delete-me.tmp"]},
        db_session,
    )

    assert result["mode"] == "targeted"
    assert result["deleted_files_count"] == 1
    assert result["orphan_files"] == [{"storage_key": "targets/delete-me.tmp", "bytes": len(b"target")}]
    assert not target.exists()
    assert unrelated.exists()

    finished_event = await db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "storage.cleanup_finished").order_by(AuditEvent.id.desc())
    )
    assert finished_event.after == result


@pytest.mark.asyncio
async def test_cleanup_storage_targets_dry_run_missing_invalid_and_referenced_keys(
    db_session, seed_mistake, tmp_path, monkeypatch
):
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(settings, "storage_root", storage_root)
    target = _write(storage_root / "targets" / "dry-run.tmp", b"target")
    referenced = _write(storage_root / "referenced" / "keep.jpg", b"referenced")

    mistake = await seed_mistake()
    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            source_type="manual_licensed",
            rights_status="manual_licensed",
            may_use_directly=True,
            storage_key_original="referenced/keep.jpg",
            status="approved",
        )
    )
    await db_session.commit()

    result = await cleanup_storage_targets(
        {
            "dry_run": "true",
            "mode": "targeted",
            "reason": "pytest",
            "old_storage_keys": [
                "targets/dry-run.tmp",
                "targets/missing.tmp",
                "referenced/keep.jpg",
                "../escape.tmp",
                "a//b.tmp",
            ],
        },
        db_session,
    )

    assert result["dry_run"] is True
    assert result["deleted_files_count"] == 0
    assert result["orphan_files"] == [{"storage_key": "targets/dry-run.tmp", "bytes": len(b"target")}]
    assert result["missing_target_files"] == [{"storage_key": "targets/missing.tmp"}]
    assert result["skipped_referenced_files_count"] == 1
    assert result["skipped_referenced_files"][0]["storage_key"] == "referenced/keep.jpg"
    assert result["invalid_target_keys"] == [{"storage_key": "../escape.tmp"}, {"storage_key": "a//b.tmp"}]
    assert target.exists()
    assert referenced.exists()
