from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.orm import selectinload

from app.models.audit import AuditEvent
from app.models.candidate import ImageCandidate, ReferenceBrief
from app.services.candidate_status import APPROVED_REFERENCE, LEGACY_APPROVED_REFERENCE, REFERENCE_ONLY_ROLE

REFERENCE_BRIEF_STATUSES = {"draft", "approved", "failed"}


def is_reference_candidate(candidate: ImageCandidate) -> bool:
    return (
        candidate.usage_role == REFERENCE_ONLY_ROLE
        or candidate.status in {APPROVED_REFERENCE, LEGACY_APPROVED_REFERENCE}
    )


def _compact_items(items: list[str | None]) -> list[str]:
    seen: set[str] = set()
    compact: list[str] = []
    for item in items:
        value = " ".join(str(item or "").split())
        if not value or value in seen:
            continue
        compact.append(value)
        seen.add(value)
    return compact


def _negative_criteria(candidate: ImageCandidate) -> list[str]:
    criteria = candidate.mistake.negative_criteria if candidate.mistake else []
    if not isinstance(criteria, list):
        return []
    return [str(item) for item in criteria if str(item or "").strip()]


def build_reference_brief_draft(candidate: ImageCandidate) -> dict[str, Any]:
    mistake = candidate.mistake
    if not mistake:
        raise ValueError(f"Candidate {candidate.id} has no mistake")

    side_prompt = mistake.wrong_visual_prompt if candidate.side == "wrong" else mistake.right_visual_prompt
    opposite_prompt = mistake.right_visual_prompt if candidate.side == "wrong" else mistake.wrong_visual_prompt
    problem_parts = _compact_items([mistake.title, mistake.explanation, side_prompt])
    visual_problem = ". ".join(problem_parts) if problem_parts else f"Kitchen design reference for {candidate.side} side"

    dimensions = None
    if candidate.original_width and candidate.original_height:
        dimensions = f"reference source dimensions: {candidate.original_width}x{candidate.original_height}"

    quality_notes = []
    flags = candidate.quality_flags or {}
    if isinstance(flags, dict):
        quality_notes = [key for key, value in flags.items() if isinstance(value, bool) and value]

    important_visual_signs = _compact_items([
        side_prompt,
        f"side: {candidate.side}",
        f"source type: {candidate.source_type}",
        f"source domain: {candidate.domain}" if candidate.domain else None,
        dimensions,
        "quality flags: " + ", ".join(quality_notes) if quality_notes else None,
    ])

    do_not_copy = _compact_items([
        "конкретную фотографию, композицию, ракурс и планировку",
        "уникальные детали, бренды, светильники, декор и мебель из референса",
        "цветовую схему один-в-один",
        *_negative_criteria(candidate),
    ])

    clean_generation_parts = _compact_items([
        "Сгенерировать новую оригинальную иллюстрацию кухни без копирования референса.",
        f"Абстрактно показать визуальную идею: {side_prompt}" if side_prompt else None,
        f"Контекст ошибки: {mistake.title}" if mistake.title else None,
        f"Сравнительный правильный ориентир: {opposite_prompt}" if opposite_prompt else None,
    ])

    negative_parts = _compact_items([
        "водяные знаки",
        "текст на изображении",
        "логотипы и бренды",
        "копирование исходного фото",
        *_negative_criteria(candidate),
    ])

    return {
        "side": candidate.side,
        "visual_problem": visual_problem,
        "important_visual_signs": important_visual_signs,
        "do_not_copy": do_not_copy,
        "clean_generation_brief": " ".join(clean_generation_parts),
        "negative_prompt": ", ".join(negative_parts),
        "status": "draft",
    }


async def get_reference_candidate(candidate_id: int, db: AsyncSession, *, for_update: bool = False) -> ImageCandidate:
    query = (
        select(ImageCandidate)
        .where(ImageCandidate.id == candidate_id)
        .options(selectinload(ImageCandidate.mistake))
    )
    if for_update:
        query = query.with_for_update()
    result = await db.execute(query)
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")
    if not is_reference_candidate(candidate):
        raise ValueError("Candidate must be approved_reference/reference_only before creating a reference brief")
    return candidate


async def get_reference_brief(candidate_id: int, db: AsyncSession) -> ReferenceBrief:
    result = await db.execute(select(ReferenceBrief).where(ReferenceBrief.candidate_id == candidate_id))
    brief = result.scalars().first()
    if not brief:
        raise ValueError(f"Reference brief for candidate {candidate_id} not found")
    return brief


async def create_or_update_reference_brief(
    candidate_id: int,
    db: AsyncSession,
    *,
    prompt_version: str = "mock-v1",
    actor: str = "reference-brief-job",
) -> ReferenceBrief:
    candidate = await get_reference_candidate(candidate_id, db, for_update=True)
    draft = build_reference_brief_draft(candidate)

    result = await db.execute(select(ReferenceBrief).where(ReferenceBrief.candidate_id == candidate_id).with_for_update())
    brief = result.scalars().first()
    created = brief is None
    before = None
    if brief is None:
        brief = ReferenceBrief(candidate_id=candidate.id, mistake_id=candidate.mistake_id, side=candidate.side)
        db.add(brief)
    else:
        before = {
            "status": brief.status,
            "visual_problem": brief.visual_problem,
            "important_visual_signs": brief.important_visual_signs,
            "do_not_copy": brief.do_not_copy,
            "clean_generation_brief": brief.clean_generation_brief,
            "negative_prompt": brief.negative_prompt,
            "error_message": brief.error_message,
        }

    draft["error_message"] = None
    for key, value in draft.items():
        setattr(brief, key, value)

    await db.flush()
    db.add(
        AuditEvent(
            actor=actor,
            entity_type="reference_brief",
            entity_id=brief.id,
            action="reference_brief.created" if created else "reference_brief.updated",
            before=before,
            after={**draft, "candidate_id": candidate.id, "prompt_version": prompt_version},
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    await db.refresh(brief)
    return brief


async def mark_reference_brief_failed(
    candidate_id: int,
    db: AsyncSession,
    *,
    error_message: str,
    prompt_version: str = "mock-v1",
    actor: str = "reference-brief-job",
) -> ReferenceBrief | None:
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        return None

    result = await db.execute(select(ReferenceBrief).where(ReferenceBrief.candidate_id == candidate_id).with_for_update())
    brief = result.scalars().first()
    created = brief is None
    before = None
    if not brief:
        brief = ReferenceBrief(candidate_id=candidate.id, mistake_id=candidate.mistake_id, side=candidate.side)
        db.add(brief)
    else:
        before = {
            "status": brief.status,
            "error_message": brief.error_message,
        }

    brief.status = "failed"
    brief.error_message = error_message[:2000]

    await db.flush()
    db.add(
        AuditEvent(
            actor=actor,
            entity_type="reference_brief",
            entity_id=brief.id,
            action="reference_brief.failed",
            before=before,
            after={
                "candidate_id": candidate.id,
                "prompt_version": prompt_version,
                "status": brief.status,
                "error_message": brief.error_message,
                "created": created,
            },
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    await db.refresh(brief)
    return brief


async def update_reference_brief_manual(
    candidate_id: int,
    db: AsyncSession,
    *,
    updates: dict[str, Any],
    actor: str = "admin-ui",
) -> ReferenceBrief:
    await get_reference_candidate(candidate_id, db)
    result = await db.execute(select(ReferenceBrief).where(ReferenceBrief.candidate_id == candidate_id).with_for_update())
    brief = result.scalars().first()
    if not brief:
        raise ValueError(f"Reference brief for candidate {candidate_id} not found")

    if "status" in updates and updates["status"] is not None and updates["status"] not in REFERENCE_BRIEF_STATUSES:
        raise ValueError("Reference brief status must be draft, approved, or failed")

    before = {
        "status": brief.status,
        "visual_problem": brief.visual_problem,
        "important_visual_signs": brief.important_visual_signs,
        "do_not_copy": brief.do_not_copy,
        "clean_generation_brief": brief.clean_generation_brief,
        "negative_prompt": brief.negative_prompt,
        "error_message": brief.error_message,
    }
    for key, value in updates.items():
        if value is not None:
            setattr(brief, key, value)

    await db.flush()
    db.add(
        AuditEvent(
            actor=actor,
            entity_type="reference_brief",
            entity_id=brief.id,
            action="reference_brief.manual_update",
            before=before,
            after=updates,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    await db.refresh(brief)
    return brief
