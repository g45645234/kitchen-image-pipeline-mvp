from types import SimpleNamespace

from app.config import settings
from app.services.final_asset_service import final_asset_health


def _asset(**overrides):
    data = {
        "rights_status": "own",
        "may_use_directly": True,
        "storage_status": "ok",
        "storage_key_original": "projects/1/final_assets/1/original.jpg",
        "storage_key_thumbnail": "projects/1/final_assets/1/thumb.jpg",
        "storage_key_processed": "projects/1/final_assets/1/processed_1920x1080.jpg",
        "metadata_storage_key": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_final_asset_health_reports_legacy_unknown_rights_and_missing_keys():
    health = final_asset_health(
        _asset(
            rights_status="unknown",
            may_use_directly=False,
            storage_status="ok",
            storage_key_original=None,
            storage_key_thumbnail=None,
            storage_key_processed=None,
        )
    )

    assert health["ok"] is False
    assert "rights_not_exportable" in health["warnings"]
    assert "missing_storage_keys" in health["warnings"]
    assert "missing_processed_asset" in health["warnings"]
    assert health["missing_storage_fields"] == [
        "storage_key_original",
        "storage_key_thumbnail",
        "storage_key_processed",
    ]


def test_final_asset_health_reports_ok_when_required_files_exist(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    for storage_key in [
        "projects/1/final_assets/1/original.jpg",
        "projects/1/final_assets/1/thumb.jpg",
        "projects/1/final_assets/1/processed_1920x1080.jpg",
    ]:
        path = tmp_path / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    health = final_asset_health(_asset())

    assert health == {
        "ok": True,
        "warnings": [],
        "missing_storage_fields": [],
        "invalid_storage_fields": [],
        "missing_files": [],
        "empty_files": [],
        "available_storage_fields": [
            "storage_key_original",
            "storage_key_thumbnail",
            "storage_key_processed",
        ],
    }


def test_final_asset_health_reports_missing_files_and_invalid_keys(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)

    health = final_asset_health(
        _asset(
            storage_key_original="../escaped.jpg",
            storage_key_thumbnail="projects/1/final_assets/1/thumb.jpg",
            storage_key_processed="projects/1/final_assets/1/processed_1920x1080.jpg",
        )
    )

    assert health["ok"] is False
    assert "invalid_storage_keys" in health["warnings"]
    assert "missing_storage_files" in health["warnings"]
    assert health["invalid_storage_fields"] == ["storage_key_original"]
    assert {item["field"] for item in health["missing_files"]} == {
        "storage_key_thumbnail",
        "storage_key_processed",
    }
    assert health["available_storage_fields"] == []


def test_final_asset_health_reports_empty_files(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "storage_root", tmp_path)
    for storage_key in [
        "projects/1/final_assets/1/original.jpg",
        "projects/1/final_assets/1/thumb.jpg",
        "projects/1/final_assets/1/processed_1920x1080.jpg",
    ]:
        path = tmp_path / storage_key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"")

    health = final_asset_health(_asset())

    assert health["ok"] is False
    assert "empty_storage_files" in health["warnings"]
    assert {item["field"] for item in health["empty_files"]} == {
        "storage_key_original",
        "storage_key_thumbnail",
        "storage_key_processed",
    }
    assert health["available_storage_fields"] == []
