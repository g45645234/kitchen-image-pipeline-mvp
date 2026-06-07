import json
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services.candidate_review_runner import CandidateReviewerError, get_reviewer_cli_readiness, _parse_reviewer_output


def test_parse_reviewer_output_accepts_markdown_json_block():
    parsed = _parse_reviewer_output(
        """Here is the review:
```json
{
  "reviewer_name": "codex",
  "reviewer_version": "test",
  "score": 0.82,
  "verdict": "pass",
  "reason": "good match",
  "flags": {"watermark": false}
}
```
Done.""",
        "codex",
    )

    assert parsed == {
        "reviewer_version": "test",
        "score": 0.82,
        "verdict": "pass",
        "reason": "good match",
        "flags": {"watermark": False},
    }


def test_parse_reviewer_output_accepts_extra_text_around_json():
    parsed = _parse_reviewer_output(
        'prefix {"score": "0.4", "verdict": "maybe", "flags": {}} suffix',
        "antigravity",
    )

    assert parsed["score"] == 0.4
    assert parsed["verdict"] == "maybe"


@pytest.mark.parametrize(
    "raw, message",
    [
        ('{"reviewer_name": "claude_cli", "score": 0.5, "flags": {}}', "name mismatch"),
        ('{"score": 2, "flags": {}}', "between 0 and 1"),
        ('{"score": 0.5, "verdict": "approve", "flags": {}}', "verdict"),
        ('{"score": 0.5, "flags": []}', "flags"),
    ],
)
def test_parse_reviewer_output_rejects_bad_contract(raw, message):
    with pytest.raises(CandidateReviewerError, match=message):
        _parse_reviewer_output(raw, "codex")


def test_get_reviewer_cli_readiness_reports_host_bridge_contract(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "host_reviewer_status_path", tmp_path / "missing-host-reviewer-status.json")
    monkeypatch.setattr(settings, "codex_cli_command", "python3 --version")
    monkeypatch.setattr(settings, "antigravity_cli_command", None)
    monkeypatch.setattr(settings, "claude_cli_command", "/definitely/missing/claude")

    status = get_reviewer_cli_readiness()

    assert status["codex"]["configured"] is True
    assert status["codex"]["execution_environment"] == "host_bridge"
    assert status["codex"]["ready"] is False
    assert status["codex"]["executable"] is False
    assert status["codex"]["web_process_executable"] is True
    assert "host_reviewer_bridge" in status["codex"]["message"]
    assert status["codex"]["error"] == "host bridge heartbeat file not found"
    assert status["antigravity"]["configured"] is False
    assert status["antigravity"]["error"] == "command is not configured for host bridge"
    assert status["claude_cli"]["configured"] is True
    assert status["claude_cli"]["ready"] is False
    assert status["claude_cli"]["web_process_executable"] is False
    assert status["claude_cli"]["error"] == "host bridge heartbeat file not found"



def test_get_reviewer_cli_readiness_uses_fresh_host_bridge_heartbeat(monkeypatch, tmp_path):
    status_path = tmp_path / "host_reviewer_bridge_status.json"
    status_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "service": "host_reviewer_bridge",
                "heartbeat_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "state": "idle",
                "pid": 1234,
                "locked_by": "bridge-test",
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
    monkeypatch.setattr(settings, "codex_cli_command", None)
    monkeypatch.setattr(settings, "antigravity_cli_command", None)
    monkeypatch.setattr(settings, "claude_cli_command", None)

    status = get_reviewer_cli_readiness()

    assert status["codex"]["ready"] is True
    assert status["codex"]["executable"] is True
    assert status["codex"]["configured"] is True
    assert status["codex"]["host_bridge_state"] == "idle"
    assert status["codex"]["host_bridge_pid"] == 1234
    assert status["codex"]["host_bridge_locked_by"] == "bridge-test"
    assert status["codex"]["host_bridge_age_seconds"] >= 0
    assert status["codex"]["error"] is None
    assert status["antigravity"]["ready"] is False
    assert status["antigravity"]["error"] == "command is not configured on host bridge"
    assert status["claude_cli"]["ready"] is True


def test_get_reviewer_cli_readiness_rejects_stale_host_bridge_heartbeat(monkeypatch, tmp_path):
    status_path = tmp_path / "host_reviewer_bridge_status.json"
    heartbeat_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    status_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "service": "host_reviewer_bridge",
                "heartbeat_at": heartbeat_at.isoformat().replace("+00:00", "Z"),
                "state": "idle",
                "pid": 1234,
                "locked_by": "bridge-test",
                "reviewers": {"codex": {"configured": True}},
            }
        )
    )
    monkeypatch.setattr(settings, "host_reviewer_status_path", status_path)
    monkeypatch.setattr(settings, "host_reviewer_status_ttl_seconds", 30)

    status = get_reviewer_cli_readiness()

    assert status["codex"]["ready"] is False
    assert status["codex"]["configured"] is True
    assert status["codex"]["error"] == "host bridge heartbeat is stale"
