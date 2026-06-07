import json
import sys

import pytest

from app.commands.host_reviewer_bridge import extract_json_object, parse_bool, prepare_local_image_payload, run_reviewer_command, sql_literal


def test_extract_json_object_from_extra_text():
    assert extract_json_object('prefix {"score": 0.8} suffix') == '{"score": 0.8}'




def test_normalize_reviewer_output_extracts_claude_result_wrapper():
    import app.commands.host_reviewer_bridge as bridge

    wrapped = {
        "type": "result",
        "result": "prefix ```json\n{\n  \"reviewer_name\": \"claude_cli\",\n  \"score\": 0.72,\n  \"verdict\": \"pass\",\n  \"flags\": {}\n}\n``` suffix",
    }

    assert bridge.normalize_reviewer_output(json.dumps(wrapped))["score"] == 0.72


def test_normalize_reviewer_output_rejects_wrapper_without_review_contract():
    import app.commands.host_reviewer_bridge as bridge

    with pytest.raises(RuntimeError, match="score/verdict"):
        bridge.normalize_reviewer_output(json.dumps({"type": "result", "result": "{}"}))


def test_run_reviewer_command_uses_reviewer_env(monkeypatch):
    script = 'import json, sys; payload=json.loads(sys.stdin.read()); print(json.dumps({"score": payload["score"], "verdict": "pass", "flags": {}}))'
    monkeypatch.setenv("CODEX_CLI_COMMAND", f"{sys.executable} -c {json.dumps(script)}")

    result = run_reviewer_command("codex", {"score": 0.91}, timeout=5)

    assert result == {"score": 0.91, "verdict": "pass", "flags": {}}


def test_prepare_local_image_payload_rewrites_to_host_storage_root(monkeypatch, tmp_path):
    image_path = tmp_path / "candidates" / "1.jpg"
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    payload = {"candidate": {"storage_key_original": "candidates/1.jpg", "image_file_path": "/src/storage/candidates/1.jpg"}}
    monkeypatch.setenv("HOST_STORAGE_ROOT", str(tmp_path))

    prepare_local_image_payload(payload)

    assert payload["candidate"]["image_file_path"] == str(image_path)
    assert payload["candidate"]["image_file_available"] is True
    assert payload["candidate"]["review_image_source"] == "local_file"


def test_prepare_local_image_payload_rejects_missing_file(tmp_path):
    payload = {"candidate": {"image_file_path": str(tmp_path / "missing.jpg")}}

    with pytest.raises(RuntimeError, match="local image file not found"):
        prepare_local_image_payload(payload)


def test_sql_literal_escapes_single_quotes():
    assert sql_literal("a'b") == "'a''b'"


def test_write_status_file_records_heartbeat_without_commands_or_tokens(monkeypatch, tmp_path):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setenv("CODEX_CLI_COMMAND", "codex exec --sandbox read-only")
    monkeypatch.setenv("ADMIN_API_TOKEN", "secret-token")
    status_path = tmp_path / "host_reviewer_bridge_status.json"

    bridge.write_status_file(str(status_path), "bridge-test", "idle", last_job_id=123)

    data = json.loads(status_path.read_text())
    assert data["service"] == "host_reviewer_bridge"
    assert data["schema_version"] == "1.0"
    assert data["state"] == "idle"
    assert data["locked_by"] == "bridge-test"
    assert data["last_job_id"] == 123
    assert data["reviewers"]["codex"]["configured"] is True
    assert data["reviewers"]["antigravity"]["configured"] is False
    raw = status_path.read_text()
    assert "codex exec" not in raw
    assert "secret-token" not in raw




def test_process_one_skips_existing_review_without_cli_call(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    calls = []
    job = {
        "id": 10,
        "payload": {"candidate_id": 42, "reviewer_name": "codex", "prompt_version": "v1"},
        "attempts": 1,
        "max_attempts": 3,
    }

    monkeypatch.setattr(bridge, "claim_job", lambda database_url, locked_by: job)
    monkeypatch.setattr(
        bridge,
        "find_existing_review",
        lambda api_base_url, candidate_id, reviewer_name, timeout, admin_token=None: {
            "id": 77,
            "reviewer_name": reviewer_name,
        },
    )

    def fail_if_called(*args, **kwargs):
        raise AssertionError("reviewer command should not run when review already exists")

    monkeypatch.setattr(bridge, "run_reviewer_command", fail_if_called)
    monkeypatch.setattr(bridge, "fail_job", fail_if_called)
    monkeypatch.setattr(bridge, "complete_job", lambda database_url, job_id, locked_by, result: calls.append((job_id, locked_by, result)))

    processed = bridge.process_one("db-url", "http://api", "tester", timeout=5, admin_token="secret")

    assert processed is True
    assert calls == [
        (
            10,
            "tester",
            {
                "review_id": 77,
                "candidate_id": 42,
                "reviewer_name": "codex",
                "skipped_existing_review": True,
            },
        )
    ]


def test_process_one_passes_admin_token_to_payload_and_save_calls(monkeypatch, tmp_path):
    import app.commands.host_reviewer_bridge as bridge

    http_calls = []
    completed = []
    job = {
        "id": 11,
        "payload": {"candidate_id": 42, "reviewer_name": "codex", "prompt_version": "v2", "force": True},
        "attempts": 1,
        "max_attempts": 3,
    }

    monkeypatch.setattr(bridge, "claim_job", lambda database_url, locked_by: job)

    image_path = tmp_path / "candidate.jpg"
    image_path.write_bytes(b"image")

    def fake_http_json(method, url, payload=None, timeout=30, admin_token=None):
        http_calls.append((method, url, payload, timeout, admin_token))
        if method == "GET":
            return {"candidate": {"id": 42, "image_file_path": str(image_path), "storage_key_original": "candidate.jpg"}}
        return {"id": 88}

    monkeypatch.setattr(bridge, "http_json", fake_http_json)
    monkeypatch.setattr(
        bridge,
        "run_reviewer_command",
        lambda reviewer_name, payload, timeout: {"score": 0.8, "verdict": "pass", "flags": {}},
    )
    monkeypatch.setattr(bridge, "complete_job", lambda database_url, job_id, locked_by, result: completed.append((job_id, locked_by, result)))

    processed = bridge.process_one("db-url", "http://api", "tester", timeout=7, admin_token="secret")

    assert processed is True
    assert http_calls[0] == (
        "GET",
        "http://api/api/candidates/42/review-payload?prompt_version=v2",
        None,
        7,
        "secret",
    )
    assert http_calls[1][0] == "PUT"
    assert http_calls[1][1] == "http://api/api/candidates/42/reviews/codex"
    assert http_calls[1][2]["reviewer_name"] == "codex"
    assert http_calls[1][2]["response_time_ms"] >= 0
    assert http_calls[1][4] == "secret"
    assert completed == [
        (
            11,
            "tester",
            {
                "review_id": 88,
                "candidate_id": 42,
                "reviewer_name": "codex",
                "response_time_ms": http_calls[1][2]["response_time_ms"],
            },
        )
    ]


def test_process_one_requeues_or_fails_via_fail_job_on_error(monkeypatch, tmp_path):
    import app.commands.host_reviewer_bridge as bridge

    failed = []
    job = {
        "id": 12,
        "payload": {"candidate_id": 42, "reviewer_name": "codex"},
        "attempts": 2,
        "max_attempts": 3,
    }

    monkeypatch.setattr(bridge, "claim_job", lambda database_url, locked_by: job)
    monkeypatch.setattr(bridge, "find_existing_review", lambda *args, **kwargs: None)
    image_path = tmp_path / "candidate.jpg"
    image_path.write_bytes(b"image")
    monkeypatch.setattr(
        bridge,
        "http_json",
        lambda *args, **kwargs: {"candidate": {"id": 42, "image_file_path": str(image_path), "storage_key_original": "candidate.jpg"}},
    )
    monkeypatch.setattr(bridge, "run_reviewer_command", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(bridge, "fail_job", lambda database_url, failed_job, locked_by, error: failed.append((failed_job, locked_by, error)))

    try:
        bridge.process_one("db-url", "http://api", "tester", timeout=5)
    except RuntimeError as e:
        assert str(e) == "boom"
    else:
        raise AssertionError("process_one should re-raise reviewer command errors")

    assert failed == [(job, "tester", "boom")]


def test_parse_bool_treats_false_strings_as_false():
    assert parse_bool("false") is False
    assert parse_bool("0") is False
    assert parse_bool("off") is False
    assert parse_bool("true") is True
    assert parse_bool(True) is True


def test_process_one_force_string_false_does_not_bypass_existing_review(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    completed = []
    job = {
        "id": 13,
        "payload": {"candidate_id": 42, "reviewer_name": "codex", "force": "false"},
        "attempts": 1,
        "max_attempts": 3,
    }

    monkeypatch.setattr(bridge, "claim_job", lambda database_url, locked_by: job)
    monkeypatch.setattr(
        bridge,
        "find_existing_review",
        lambda api_base_url, candidate_id, reviewer_name, timeout, admin_token=None: {
            "id": 90,
            "reviewer_name": reviewer_name,
        },
    )
    monkeypatch.setattr(bridge, "run_reviewer_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("force=false must skip existing review")))
    monkeypatch.setattr(bridge, "complete_job", lambda database_url, job_id, locked_by, result: completed.append((job_id, locked_by, result)))

    assert bridge.process_one("db-url", "http://api", "tester", timeout=5) is True
    assert completed == [
        (
            13,
            "tester",
            {
                "review_id": 90,
                "candidate_id": 42,
                "reviewer_name": "codex",
                "skipped_existing_review": True,
            },
        )
    ]


def test_complete_job_requires_processing_owner(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    sql_calls = []

    def fake_run_psql(database_url, sql):
        sql_calls.append(sql)
        return "10"

    monkeypatch.setattr(bridge, "run_psql", fake_run_psql)

    bridge.complete_job("db-url", 10, "bridge-a", {"ok": True})

    sql = sql_calls[0]
    assert "WHERE id = 10::integer" in sql
    assert "AND status = 'processing'" in sql
    assert "AND locked_by = 'bridge-a'" in sql
    assert "RETURNING id" in sql


def test_complete_job_raises_when_owner_guard_updates_no_rows(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setattr(bridge, "run_psql", lambda database_url, sql: "")

    with pytest.raises(RuntimeError, match="not owned"):
        bridge.complete_job("db-url", 10, "bridge-a", {"ok": True})


def test_fail_job_requeues_before_max_attempts_and_fails_at_max(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    sql_calls = []
    monkeypatch.setattr(bridge, "run_psql", lambda database_url, sql: sql_calls.append(sql) or "12")

    bridge.fail_job("db-url", {"id": 12, "attempts": 1, "max_attempts": 3}, "bridge-a", "x" * 2100)
    bridge.fail_job("db-url", {"id": 12, "attempts": 3, "max_attempts": 3}, "bridge-a", "boom")

    assert "status = 'pending'" in sql_calls[0]
    assert "AND status = 'processing'" in sql_calls[0]
    assert "AND locked_by = 'bridge-a'" in sql_calls[0]
    assert "RETURNING id" in sql_calls[0]
    assert "x" * 2000 in sql_calls[0]
    assert "x" * 2001 not in sql_calls[0]
    assert "status = 'failed'" in sql_calls[1]


def test_process_one_preserves_original_error_when_fail_job_fails(monkeypatch, tmp_path):
    import app.commands.host_reviewer_bridge as bridge

    job = {
        "id": 14,
        "payload": {"candidate_id": 42, "reviewer_name": "codex"},
        "attempts": 1,
        "max_attempts": 3,
    }

    monkeypatch.setattr(bridge, "claim_job", lambda database_url, locked_by: job)
    monkeypatch.setattr(bridge, "find_existing_review", lambda *args, **kwargs: None)
    image_path = tmp_path / "candidate.jpg"
    image_path.write_bytes(b"image")
    monkeypatch.setattr(
        bridge,
        "http_json",
        lambda *args, **kwargs: {"candidate": {"id": 42, "image_file_path": str(image_path), "storage_key_original": "candidate.jpg"}},
    )
    monkeypatch.setattr(bridge, "run_reviewer_command", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("reviewer boom")))
    monkeypatch.setattr(bridge, "fail_job", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("psql boom")))

    with pytest.raises(RuntimeError) as exc_info:
        bridge.process_one("db-url", "http://api", "tester", timeout=5)

    assert str(exc_info.value) == "reviewer boom"
    assert any("psql boom" in note for note in exc_info.value.__notes__)



def test_parse_bool_string_false_is_false():
    import app.commands.host_reviewer_bridge as bridge

    assert bridge.parse_bool("false") is False
    assert bridge.parse_bool("0") is False
    assert bridge.parse_bool("") is False
    assert bridge.parse_bool("true") is True


def test_complete_job_sql_guards_processing_owner(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    statements = []
    monkeypatch.setattr(bridge, "run_psql", lambda database_url, sql: statements.append(sql) or "123")

    bridge.complete_job("db-url", 123, "bridge-a", {"ok": True})

    sql = statements[0]
    assert "AND status = 'processing'" in sql
    assert "AND locked_by = 'bridge-a'" in sql
    assert "RETURNING id" in sql


def test_fail_job_sql_guards_processing_owner_and_requeues(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    statements = []
    monkeypatch.setattr(bridge, "run_psql", lambda database_url, sql: statements.append(sql) or "123")

    bridge.fail_job(
        "db-url",
        {"id": 123, "attempts": 1, "max_attempts": 3},
        "bridge-a",
        "boom" * 1000,
    )

    sql = statements[0]
    assert "status = 'pending'" in sql
    assert "AND status = 'processing'" in sql
    assert "AND locked_by = 'bridge-a'" in sql
    assert "RETURNING id" in sql


def test_process_one_force_string_false_does_not_bypass_existing_review_skip(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    calls = []
    job = {
        "id": 13,
        "payload": {"candidate_id": 42, "reviewer_name": "codex", "force": "false"},
        "attempts": 1,
        "max_attempts": 3,
    }

    monkeypatch.setattr(bridge, "claim_job", lambda database_url, locked_by: job)
    monkeypatch.setattr(bridge, "find_existing_review", lambda *args, **kwargs: {"id": 99})
    monkeypatch.setattr(bridge, "run_reviewer_command", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should skip")))
    monkeypatch.setattr(bridge, "complete_job", lambda database_url, job_id, locked_by, result: calls.append((job_id, locked_by, result)))

    assert bridge.process_one("db-url", "http://api", "tester", timeout=5) is True
    assert calls[0][2]["skipped_existing_review"] is True

def test_assert_safe_process_user_rejects_root_without_explicit_opt_in(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setattr(bridge.os, "geteuid", lambda: 0, raising=False)

    with pytest.raises(RuntimeError, match="HOST_REVIEWER_ALLOW_ROOT=true"):
        bridge.assert_safe_process_user(False)


def test_assert_safe_process_user_allows_explicit_root_opt_in(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setattr(bridge.os, "geteuid", lambda: 0, raising=False)

    bridge.assert_safe_process_user(True)


def test_parse_args_reads_allow_root_env(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")
    monkeypatch.setenv("HOST_REVIEWER_ALLOW_ROOT", "true")

    args = bridge.parse_args([])

    assert args.allow_root is True

def test_run_preflight_checks_reports_success(monkeypatch, tmp_path):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setenv("CODEX_CLI_COMMAND", f"{sys.executable} --version")
    monkeypatch.setenv("ANTIGRAVITY_CLI_COMMAND", f"{sys.executable} --version")
    monkeypatch.setenv("CLAUDE_CLI_COMMAND", f"{sys.executable} --version")
    monkeypatch.setattr(bridge, "run_psql", lambda database_url, sql: "1")
    monkeypatch.setattr(bridge, "http_json", lambda *args, **kwargs: {"status": "ok"})

    result = bridge.run_preflight_checks(
        "db-url",
        "http://api",
        str(tmp_path / "host_reviewer_bridge_status.json"),
        timeout=5,
    )

    assert result["ok"] is True
    assert result["errors"] == []
    assert {check["name"] for check in result["checks"]} == {"database", "api", "status_path", "reviewer_commands"}


def test_run_preflight_checks_reports_missing_reviewer_command(monkeypatch, tmp_path):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.delenv("CODEX_CLI_COMMAND", raising=False)
    monkeypatch.setenv("ANTIGRAVITY_CLI_COMMAND", f"{sys.executable} --version")
    monkeypatch.setenv("CLAUDE_CLI_COMMAND", f"{sys.executable} --version")
    monkeypatch.setattr(bridge, "run_psql", lambda database_url, sql: "1")
    monkeypatch.setattr(bridge, "http_json", lambda *args, **kwargs: {"status": "ok"})

    result = bridge.run_preflight_checks(
        "db-url",
        "http://api",
        str(tmp_path / "host_reviewer_bridge_status.json"),
        timeout=5,
    )

    assert result["ok"] is False
    assert any("CODEX_CLI_COMMAND is not configured" in error for error in result["errors"])


def test_parse_args_reads_check_flag(monkeypatch):
    import app.commands.host_reviewer_bridge as bridge

    monkeypatch.setenv("DATABASE_URL", "postgresql://example")

    args = bridge.parse_args(["--check"])

    assert args.check is True
