from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import signal
import shlex
import shutil
import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.candidate import CandidateReview, ImageCandidate
from app.services.candidate_review_service import EXPECTED_REVIEWERS, get_candidate_review_aggregate, upsert_candidate_review
from app.services.storage_service import _normalize_storage_key, _path_for_storage_key


class CandidateReviewerError(ValueError):
    pass


REVIEWER_COMMAND_SETTINGS = {
    "codex": "codex_cli_command",
    "antigravity": "antigravity_cli_command",
    "claude_cli": "claude_cli_command",
}



def _parse_utc_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _read_host_bridge_status() -> dict[str, Any] | None:
    status_path = settings.host_reviewer_status_path
    if not status_path:
        return None
    path = Path(status_path)
    if not path.exists() or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("service") != "host_reviewer_bridge":
        return None
    heartbeat_at = _parse_utc_datetime(data.get("heartbeat_at"))
    if not heartbeat_at:
        return None
    age_seconds = max(0.0, (datetime.now(timezone.utc) - heartbeat_at).total_seconds())
    data["_heartbeat_at"] = heartbeat_at
    data["_age_seconds"] = age_seconds
    data["_fresh"] = age_seconds <= max(1, int(settings.host_reviewer_status_ttl_seconds))
    return data


def _web_process_command_diagnostics(command: str | None) -> tuple[bool, str | None, str | None]:
    if not command:
        return False, None, "command is not configured for host bridge"
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return False, None, f"invalid command syntax: {e}"
    if not parts:
        return False, None, "command is empty"
    executable_path = shutil.which(parts[0])
    return executable_path is not None, executable_path, None


def get_reviewer_cli_readiness() -> dict[str, dict[str, Any]]:
    readiness = {}
    bridge_status = _read_host_bridge_status()
    bridge_fresh = bool(bridge_status and bridge_status.get("_fresh"))
    bridge_state = str(bridge_status.get("state")) if bridge_status else None
    bridge_reviewers = bridge_status.get("reviewers", {}) if isinstance(bridge_status, dict) else {}
    host_bridge_message = (
        "Real reviewer jobs run through the host_reviewer_bridge process, not the Docker web process. "
        "Start the host bridge to process run_candidate_reviewer jobs."
    )
    for reviewer_name in EXPECTED_REVIEWERS:
        setting_name = REVIEWER_COMMAND_SETTINGS[reviewer_name]
        command = getattr(settings, setting_name)
        web_executable, web_executable_path, command_error = _web_process_command_diagnostics(command)
        host_reviewer_status = bridge_reviewers.get(reviewer_name, {}) if isinstance(bridge_reviewers, dict) else {}
        host_configured = bool(host_reviewer_status.get("configured")) if bridge_status else bool(command)
        ready = bool(bridge_fresh and host_configured)
        status = {
            "reviewer_name": reviewer_name,
            "setting_name": setting_name,
            "configured": host_configured,
            "executable": ready,
            "executable_path": None,
            "ready": ready,
            "execution_environment": "host_bridge",
            "message": host_bridge_message,
            "web_process_executable": web_executable,
            "web_process_executable_path": web_executable_path,
            "host_bridge_seen_at": bridge_status.get("_heartbeat_at") if bridge_status else None,
            "host_bridge_age_seconds": bridge_status.get("_age_seconds") if bridge_status else None,
            "host_bridge_state": bridge_state,
            "host_bridge_pid": bridge_status.get("pid") if bridge_status else None,
            "host_bridge_locked_by": bridge_status.get("locked_by") if bridge_status else None,
            "error": None,
        }
        if ready:
            status["message"] = "Host reviewer bridge heartbeat is fresh and this reviewer is configured on the host bridge."
        elif not bridge_status:
            status["error"] = command_error or "host bridge heartbeat file not found"
        elif not bridge_fresh:
            status["error"] = "host bridge heartbeat is stale"
        elif not host_configured:
            status["error"] = "command is not configured on host bridge"
        else:
            status["error"] = "host bridge is not ready"
        readiness[reviewer_name] = status
    return readiness

def _reviewer_command(reviewer_name: str) -> list[str]:
    setting_name = REVIEWER_COMMAND_SETTINGS.get(reviewer_name)
    if not setting_name:
        raise CandidateReviewerError(f"Unknown reviewer: {reviewer_name}")
    command = getattr(settings, setting_name)
    if not command:
        raise CandidateReviewerError(f"No CLI command configured for reviewer: {reviewer_name}")
    return shlex.split(command)


def _reviewer_payload(candidate: ImageCandidate, prompt_version: str) -> dict[str, Any]:
    mistake = candidate.mistake
    if not mistake:
        raise CandidateReviewerError(f"Candidate {candidate.id} has no loaded mistake")

    storage_key_original = _normalize_storage_key(candidate.storage_key_original)
    if not storage_key_original:
        raise CandidateReviewerError(
            f"Candidate {candidate.id} must be downloaded before AI review: storage_key_original is missing"
        )
    image_file_path = _path_for_storage_key(settings.storage_root, storage_key_original)
    if not image_file_path.exists() or not image_file_path.is_file():
        raise CandidateReviewerError(
            f"Candidate {candidate.id} must be downloaded before AI review: file not found for {storage_key_original}"
        )

    return {
        "prompt_version": prompt_version,
        "candidate": {
            "id": candidate.id,
            "mistake_id": candidate.mistake_id,
            "side": candidate.side,
            "source_type": candidate.source_type,
            "source_provider": candidate.source_provider,
            "source_page_url": candidate.source_page_url,
            "image_url": candidate.image_url,
            "thumbnail_url": candidate.thumbnail_url,
            "storage_key_original": candidate.storage_key_original,
            "storage_key_thumbnail": candidate.storage_key_thumbnail,
            "storage_key_processed": candidate.storage_key_processed,
            "image_file_path": str(image_file_path),
            "image_file_available": True,
            "review_image_source": "local_file",
            "original_width": candidate.original_width,
            "original_height": candidate.original_height,
            "domain": candidate.domain,
            "author_name": candidate.author_name,
            "license_label": candidate.license_label,
            "rights_status": candidate.rights_status,
            "usage_role": candidate.usage_role,
            "may_use_directly": candidate.may_use_directly,
            "quality_flags": candidate.quality_flags,
            "is_low_quality": candidate.is_low_quality,
        },
        "mistake": {
            "id": mistake.id,
            "video_id": mistake.video_id,
            "order_index": mistake.order_index,
            "title": mistake.title,
            "short_title": mistake.short_title,
            "time_start": mistake.time_start,
            "time_end": mistake.time_end,
            "wrong_visual_prompt": mistake.wrong_visual_prompt,
            "right_visual_prompt": mistake.right_visual_prompt,
            "negative_criteria": mistake.negative_criteria,
        },
        "rubric": {
            "score_min": 0.0,
            "score_max": 1.0,
            "pass_threshold": 0.7,
            "verdicts": ["pass", "maybe", "fail"],
            "note": "Evaluate visual relevance and quality only. Do not confirm rights and do not select final assets.",
        },
    }


def _extract_json_object(raw_output: str) -> str:
    cleaned = raw_output.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", cleaned, re.DOTALL | re.IGNORECASE)
    if fenced:
        cleaned = fenced.group(1).strip()
    if cleaned.startswith("{") and cleaned.endswith("}"):
        return cleaned
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return cleaned
    return cleaned[start : end + 1]


def _parse_reviewer_output(raw_output: str, reviewer_name: str) -> dict[str, Any]:
    try:
        data = json.loads(_extract_json_object(raw_output))
    except json.JSONDecodeError as e:
        raise CandidateReviewerError(f"Reviewer {reviewer_name} returned invalid JSON") from e

    if not isinstance(data, dict):
        raise CandidateReviewerError(f"Reviewer {reviewer_name} output must be a JSON object")

    output_reviewer = str(data.get("reviewer_name") or reviewer_name).strip().lower()
    if output_reviewer != reviewer_name:
        raise CandidateReviewerError(f"Reviewer output name mismatch: expected {reviewer_name}, got {output_reviewer}")

    try:
        score = float(data["score"])
    except (KeyError, TypeError, ValueError) as e:
        raise CandidateReviewerError(f"Reviewer {reviewer_name} output must include numeric score") from e
    if not 0 <= score <= 1:
        raise CandidateReviewerError(f"Reviewer {reviewer_name} score must be between 0 and 1")

    verdict = str(data.get("verdict") or "").strip().lower() or None
    if verdict is not None and verdict not in {"pass", "maybe", "fail"}:
        raise CandidateReviewerError(f"Reviewer {reviewer_name} verdict must be pass, maybe, or fail")

    flags = data.get("flags", {})
    if flags is None:
        flags = {}
    if not isinstance(flags, dict):
        raise CandidateReviewerError(f"Reviewer {reviewer_name} flags must be an object")

    return {
        "reviewer_version": data.get("reviewer_version"),
        "score": score,
        "verdict": verdict,
        "reason": data.get("reason"),
        "flags": flags,
    }


async def run_reviewer_cli(reviewer_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    command = _reviewer_command(reviewer_name)
    process = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    input_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(input=input_bytes),
            timeout=settings.reviewer_timeout_seconds,
        )
    except (TimeoutError, asyncio.TimeoutError) as e:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.communicate()
        raise CandidateReviewerError(f"Reviewer {reviewer_name} timed out") from e

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:1000]
        raise CandidateReviewerError(f"Reviewer {reviewer_name} command failed: {detail}")

    raw_output = stdout.decode("utf-8", errors="replace").strip()
    return _parse_reviewer_output(raw_output, reviewer_name)


async def build_candidate_reviewer_payload(
    candidate_id: int,
    db: AsyncSession,
    prompt_version: str | None = None,
) -> dict[str, Any]:
    result = await db.execute(
        select(ImageCandidate)
        .where(ImageCandidate.id == candidate_id)
        .options(selectinload(ImageCandidate.mistake))
    )
    candidate = result.scalars().first()
    if not candidate:
        raise CandidateReviewerError(f"Candidate {candidate_id} not found")
    return _reviewer_payload(candidate, prompt_version or settings.reviewer_prompt_version)


async def run_candidate_reviewer(
    candidate_id: int,
    reviewer_name: str,
    db: AsyncSession,
    prompt_version: str | None = None,
    force: bool = False,
) -> dict[str, Any]:
    reviewer_name = reviewer_name.strip().lower()
    if reviewer_name not in EXPECTED_REVIEWERS:
        raise CandidateReviewerError(f"reviewer_name must be one of: {', '.join(EXPECTED_REVIEWERS)}")

    prompt_version = prompt_version or settings.reviewer_prompt_version
    if not force:
        result = await db.execute(
            select(CandidateReview).where(
                CandidateReview.candidate_id == candidate_id,
                CandidateReview.reviewer_name == reviewer_name,
            )
        )
        existing_review = result.scalars().first()
        if existing_review:
            aggregate = await get_candidate_review_aggregate(candidate_id, db)
            return {
                "review_id": existing_review.id,
                "candidate_id": candidate_id,
                "reviewer_name": existing_review.reviewer_name,
                "review_score": aggregate["review_score"],
                "pass_count": aggregate["pass_count"],
                "approved_by_consensus": aggregate["approved_by_consensus"],
                "skipped_existing_review": True,
            }

    payload = await build_candidate_reviewer_payload(candidate_id, db, prompt_version)
    started_at = time.monotonic()
    review_data = await run_reviewer_cli(reviewer_name, payload)
    response_time_ms = max(0, int((time.monotonic() - started_at) * 1000))
    review, aggregate = await upsert_candidate_review(
        candidate_id=candidate_id,
        reviewer_name=reviewer_name,
        reviewer_version=review_data.get("reviewer_version") or prompt_version,
        score=review_data["score"],
        verdict=review_data.get("verdict"),
        reason=review_data.get("reason"),
        flags=review_data.get("flags"),
        response_time_ms=response_time_ms,
        db=db,
    )
    return {
        "review_id": review.id,
        "candidate_id": candidate_id,
        "reviewer_name": review.reviewer_name,
        "response_time_ms": review.response_time_ms,
        "review_score": aggregate["review_score"],
        "pass_count": aggregate["pass_count"],
        "approved_by_consensus": aggregate["approved_by_consensus"],
    }
