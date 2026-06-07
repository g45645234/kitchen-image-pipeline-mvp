from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REVIEWER_COMMAND_ENV = {
    "codex": "CODEX_CLI_COMMAND",
    "antigravity": "ANTIGRAVITY_CLI_COMMAND",
    "claude_cli": "CLAUDE_CLI_COMMAND",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def reviewer_env_status() -> dict[str, dict[str, Any]]:
    return {
        reviewer_name: {"configured": bool(os.environ.get(env_name))}
        for reviewer_name, env_name in REVIEWER_COMMAND_ENV.items()
    }


def write_status_file(
    status_path: str | None,
    locked_by: str,
    state: str,
    error: str | None = None,
    last_job_id: int | None = None,
) -> None:
    if not status_path:
        return
    path = Path(status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "service": "host_reviewer_bridge",
        "heartbeat_at": utc_now_iso(),
        "state": state,
        "pid": os.getpid(),
        "locked_by": locked_by,
        "reviewers": reviewer_env_status(),
        "last_job_id": last_job_id,
        "error": error[:1000] if error else None,
    }
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def extract_json_object(raw_output: str) -> str:
    cleaned = raw_output.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return cleaned
    return cleaned[start : end + 1]


def http_json(method: str, url: str, payload: dict[str, Any] | None = None, timeout: int = 30, admin_token: str | None = None) -> Any:
    data = None
    headers = {"Accept": "application/json"}
    if admin_token:
        headers["X-Admin-Token"] = admin_token
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code} from {url}: {detail}") from e
    return json.loads(body) if body else None


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def run_psql(database_url: str, sql: str) -> str:
    command = ["psql", database_url, "-X", "-A", "-t", "-c", sql]
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "psql failed")
    return completed.stdout.strip()


def claim_job(database_url: str, locked_by: str) -> dict[str, Any] | None:
    sql = """
WITH picked AS (
    SELECT id
    FROM jobs
    WHERE status = 'pending' AND type = 'run_candidate_reviewer'
    ORDER BY created_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
), updated AS (
    UPDATE jobs
    SET status = 'processing',
        started_at = COALESCE(started_at, NOW()),
        locked_by = {locked_by},
        locked_at = NOW(),
        attempts = attempts + 1
    WHERE id IN (SELECT id FROM picked)
    RETURNING id, payload, attempts, max_attempts
)
SELECT row_to_json(updated) FROM updated;
"""
    raw = run_psql(database_url, sql.format(locked_by=sql_literal(locked_by)))
    if not raw:
        return None
    return json.loads(raw)


def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    return bool(value)


def assert_safe_process_user(allow_root: bool) -> None:
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is None:
        return
    if get_effective_uid() == 0 and not allow_root:
        raise RuntimeError(
            "Refusing to run host_reviewer_bridge as root without HOST_REVIEWER_ALLOW_ROOT=true. "
            "Run it as the user that owns reviewer CLI auth, or explicitly opt in after reviewing the risk."
        )


def _preflight_record(checks: list[dict[str, Any]], name: str, ok: bool, message: str) -> None:
    checks.append({"name": name, "ok": ok, "message": message})


def _check_status_path_writable(status_path: str | None) -> None:
    if not status_path:
        return
    path = Path(status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.preflight.tmp")
    temp_path.write_text("preflight", encoding="utf-8")
    temp_path.unlink()


def _check_reviewer_command_env() -> list[str]:
    errors: list[str] = []
    for reviewer_name, env_name in REVIEWER_COMMAND_ENV.items():
        command = os.environ.get(env_name)
        if not command:
            errors.append(f"{env_name} is not configured")
            continue
        try:
            parts = shlex.split(command)
        except ValueError as e:
            errors.append(f"{env_name} has invalid syntax: {e}")
            continue
        if not parts:
            errors.append(f"{env_name} is empty")
            continue
        executable = parts[0]
        if shutil.which(executable) is None:
            errors.append(f"{env_name} executable not found: {executable}")
    return errors


def run_preflight_checks(
    database_url: str,
    api_base_url: str,
    status_path: str | None,
    timeout: int,
    admin_token: str | None = None,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    try:
        result = run_psql(database_url, "SELECT 1;")
        _preflight_record(checks, "database", result.strip() == "1", "psql SELECT 1 succeeded")
    except Exception as e:
        _preflight_record(checks, "database", False, str(e))

    try:
        health = http_json("GET", f"{api_base_url.rstrip('/')}/health", timeout=timeout, admin_token=admin_token)
        _preflight_record(checks, "api", isinstance(health, dict) and health.get("status") == "ok", "API /health succeeded")
    except Exception as e:
        _preflight_record(checks, "api", False, str(e))

    try:
        _check_status_path_writable(status_path)
        _preflight_record(checks, "status_path", True, "status path is writable")
    except Exception as e:
        _preflight_record(checks, "status_path", False, str(e))

    command_errors = _check_reviewer_command_env()
    if command_errors:
        for error in command_errors:
            _preflight_record(checks, "reviewer_commands", False, error)
    else:
        _preflight_record(checks, "reviewer_commands", True, "all reviewer command env vars are configured and executable")

    errors = [check["message"] for check in checks if not check["ok"]]
    return {"ok": not errors, "checks": checks, "errors": errors}


def complete_job(database_url: str, job_id: int, locked_by: str, result: dict[str, Any]) -> None:
    sql = """
UPDATE jobs
SET status = 'completed',
    result = {result}::jsonb,
    error_message = NULL,
    finished_at = NOW(),
    locked_by = NULL,
    locked_at = NULL
WHERE id = {job_id}::integer
  AND status = 'processing'
  AND locked_by = {locked_by}
RETURNING id;
"""
    updated = run_psql(
        database_url,
        sql.format(
            job_id=int(job_id),
            locked_by=sql_literal(locked_by),
            result=sql_literal(json.dumps(result)),
        ),
    )
    if not updated:
        raise RuntimeError(f"job {int(job_id)} is not owned by {locked_by!r} or is no longer processing")


def fail_job(database_url: str, job: dict[str, Any], locked_by: str, error: str) -> None:
    next_status = "failed" if int(job["attempts"]) >= int(job["max_attempts"]) else "pending"
    sql = """
UPDATE jobs
SET status = {status},
    error_message = {error_message},
    finished_at = CASE WHEN {status} = 'failed' THEN NOW() ELSE finished_at END,
    locked_by = NULL,
    locked_at = NULL
WHERE id = {job_id}::integer
  AND status = 'processing'
  AND locked_by = {locked_by}
RETURNING id;
"""
    updated = run_psql(
        database_url,
        sql.format(
            job_id=int(job["id"]),
            locked_by=sql_literal(locked_by),
            status=sql_literal(next_status),
            error_message=sql_literal(error[:2000]),
        ),
    )
    if not updated:
        raise RuntimeError(f"job {int(job['id'])} is not owned by {locked_by!r} or is no longer processing")


def find_existing_review(api_base_url: str, candidate_id: int, reviewer_name: str, timeout: int, admin_token: str | None = None) -> dict[str, Any] | None:
    reviews = http_json("GET", f"{api_base_url}/api/candidates/{candidate_id}/reviews", timeout=timeout, admin_token=admin_token)
    for review in reviews or []:
        if str(review.get("reviewer_name", "")).strip().lower() == reviewer_name:
            return review
    return None


def _safe_join_storage_key(root: str, storage_key: str) -> Path:
    root_path = Path(root).resolve()
    candidate_path = (root_path / storage_key).resolve()
    if root_path not in candidate_path.parents and candidate_path != root_path:
        raise RuntimeError(f"unsafe storage key for local image path: {storage_key}")
    return candidate_path


def prepare_local_image_payload(review_payload: dict[str, Any]) -> None:
    candidate = review_payload.get("candidate") if isinstance(review_payload, dict) else None
    if not isinstance(candidate, dict):
        raise RuntimeError("review payload missing candidate object")
    storage_key = candidate.get("storage_key_original")
    image_path = candidate.get("image_file_path")
    host_storage_root = os.environ.get("HOST_STORAGE_ROOT")
    if host_storage_root and storage_key:
        image_path = str(_safe_join_storage_key(host_storage_root, str(storage_key)))
        candidate["image_file_path"] = image_path
        candidate["review_image_source"] = "local_file"
    if not image_path:
        raise RuntimeError("candidate must be downloaded before AI review: image_file_path is missing")
    path = Path(str(image_path))
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"candidate must be downloaded before AI review: local image file not found: {path}")
    candidate["image_file_available"] = True


def normalize_reviewer_output(raw_output: str) -> dict[str, Any]:
    try:
        parsed = json.loads(extract_json_object(raw_output))
    except json.JSONDecodeError as e:
        raise RuntimeError("reviewer returned invalid JSON") from e

    if isinstance(parsed, dict) and {"score", "verdict"}.issubset(parsed):
        return parsed

    nested_result = parsed.get("result") if isinstance(parsed, dict) else None
    if isinstance(nested_result, str):
        try:
            nested = json.loads(extract_json_object(nested_result))
        except json.JSONDecodeError as e:
            raise RuntimeError("reviewer wrapper result contained invalid JSON") from e
        if isinstance(nested, dict) and {"score", "verdict"}.issubset(nested):
            return nested

    raise RuntimeError("reviewer JSON missing required score/verdict fields")


def run_reviewer_command(reviewer_name: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    env_name = REVIEWER_COMMAND_ENV[reviewer_name]
    command = os.environ.get(env_name)
    if not command:
        raise RuntimeError(f"{env_name} is not configured")
    process = subprocess.Popen(
        shlex.split(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=json.dumps(payload, ensure_ascii=False), timeout=timeout)
    except subprocess.TimeoutExpired as e:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise RuntimeError(f"{reviewer_name} command timed out") from e
    if process.returncode != 0:
        raise RuntimeError(stderr.strip() or f"{reviewer_name} command failed")
    try:
        return normalize_reviewer_output(stdout)
    except RuntimeError as e:
        raise RuntimeError(f"{reviewer_name} {e}") from e


def process_one(database_url: str, api_base_url: str, locked_by: str, timeout: int, admin_token: str | None = None) -> bool:
    job = claim_job(database_url, locked_by)
    if not job:
        return False
    try:
        payload = job["payload"]
        candidate_id = int(payload["candidate_id"])
        reviewer_name = str(payload["reviewer_name"]).strip().lower()
        prompt_version = payload.get("prompt_version")
        force = parse_bool(payload.get("force", False))
        if not force:
            existing_review = find_existing_review(api_base_url, candidate_id, reviewer_name, timeout=timeout, admin_token=admin_token)
            if existing_review:
                complete_job(
                    database_url,
                    int(job["id"]),
                    locked_by,
                    {
                        "review_id": existing_review["id"],
                        "candidate_id": candidate_id,
                        "reviewer_name": reviewer_name,
                        "skipped_existing_review": True,
                    },
                )
                return True

        review_payload_url = f"{api_base_url}/api/candidates/{candidate_id}/review-payload"
        if prompt_version:
            review_payload_url += f"?prompt_version={urllib.parse.quote(str(prompt_version))}"
        review_payload = http_json("GET", review_payload_url, timeout=timeout, admin_token=admin_token)
        prepare_local_image_payload(review_payload)
        started_at = time.monotonic()
        review_result = run_reviewer_command(reviewer_name, review_payload, timeout=timeout)
        review_result["response_time_ms"] = max(0, int((time.monotonic() - started_at) * 1000))
        review_result["reviewer_name"] = reviewer_name
        saved_review = http_json(
            "PUT",
            f"{api_base_url}/api/candidates/{candidate_id}/reviews/{reviewer_name}",
            payload=review_result,
            timeout=timeout,
            admin_token=admin_token,
        )
        complete_job(
            database_url,
            int(job["id"]),
            locked_by,
            {
                "review_id": saved_review["id"],
                "candidate_id": candidate_id,
                "reviewer_name": reviewer_name,
                "response_time_ms": review_result["response_time_ms"],
            },
        )
        return True
    except Exception as e:
        try:
            fail_job(database_url, job, locked_by, str(e))
        except Exception as fail_error:
            e.add_note(f"Failed to update job failure state: {fail_error}")
        raise


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Host-side bridge for run_candidate_reviewer jobs")
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"), required=not os.environ.get("DATABASE_URL"))
    parser.add_argument("--api-base-url", default=os.environ.get("API_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--locked-by", default=os.environ.get("HOST_REVIEWER_LOCKED_BY", "host_reviewer_bridge"))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("REVIEWER_TIMEOUT_SECONDS", "120")))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("WORKER_POLL_INTERVAL_SECONDS", "5")))
    parser.add_argument("--admin-token", default=os.environ.get("ADMIN_API_TOKEN"))
    parser.add_argument(
        "--allow-root",
        action="store_true",
        default=parse_bool(os.environ.get("HOST_REVIEWER_ALLOW_ROOT")),
        help="Allow running the bridge as root. Prefer a dedicated user with reviewer CLI auth instead.",
    )
    parser.add_argument(
        "--status-path",
        default=os.environ.get("HOST_REVIEWER_STATUS_PATH", "./storage/host_reviewer_bridge_status.json"),
        help="Path to a JSON heartbeat file readable by the web process. Use an empty value to disable.",
    )
    parser.add_argument("--check", action="store_true", help="Run preflight checks and exit without claiming reviewer jobs.")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    assert_safe_process_user(args.allow_root)
    status_path = args.status_path or None
    if args.check:
        result = run_preflight_checks(
            args.database_url,
            args.api_base_url.rstrip("/"),
            status_path,
            args.timeout,
            args.admin_token,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ok"] else 1
    write_status_file(status_path, args.locked_by, "started")
    while True:
        try:
            write_status_file(status_path, args.locked_by, "polling")
            processed = process_one(args.database_url, args.api_base_url.rstrip("/"), args.locked_by, args.timeout, args.admin_token)
            write_status_file(status_path, args.locked_by, "processed" if processed else "idle")
            if args.once:
                return 0
            if not processed:
                time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            write_status_file(status_path, args.locked_by, "stopped")
            return 0
        except Exception as e:
            write_status_file(status_path, args.locked_by, "error", error=str(e))
            print(str(e), file=sys.stderr)
            if args.once:
                return 1
            time.sleep(args.poll_interval)


if __name__ == "__main__":
    raise SystemExit(main())
