from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.audit import AuditEvent, BlockedDomain
from app.models.candidate import ImageCandidate
from app.services.candidate_status import APPROVED_REFERENCE, REFERENCE_ONLY_ROLE


BLOCK_DOMAIN_AUTO_REJECT_STATUSES = {"new", "auto_reviewed", "pending", "review"}


def _normalize_domain(domain: str | None) -> str | None:
    normalized = (domain or "").strip().rstrip(".").lower()
    return normalized or None


async def mark_candidate_as_reference(
    candidate_id: int,
    db: AsyncSession,
    mark_high_value: bool = False,
    comment: str | None = None,
    actor: str = "admin-ui",
) -> ImageCandidate:
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    before: dict[str, Any] = {
        "status": candidate.status,
        "usage_role": candidate.usage_role,
        "reference_priority_score": float(candidate.reference_priority_score) if candidate.reference_priority_score is not None else None,
    }

    candidate.status = APPROVED_REFERENCE
    candidate.usage_role = REFERENCE_ONLY_ROLE
    if mark_high_value:
        candidate.reference_priority_score = 1.0

    after: dict[str, Any] = {
        "status": candidate.status,
        "usage_role": candidate.usage_role,
        "reference_priority_score": float(candidate.reference_priority_score) if candidate.reference_priority_score is not None else None,
        "mark_high_value": mark_high_value,
    }

    for action in ["reference_marked", "candidate.approved_reference"]:
        db.add(
            AuditEvent(
                actor=actor,
                entity_type="candidate",
                entity_id=candidate.id,
                action=action,
                before=before,
                after=after,
                comment=comment,
                created_at=datetime.now(timezone.utc),
            )
        )

    await db.commit()
    await db.refresh(candidate)
    return candidate


async def block_candidate_domain(
    candidate_id: int,
    db: AsyncSession,
    reason: str | None = None,
    actor: str = "admin-ui",
) -> BlockedDomain:
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")
    normalized_domain = _normalize_domain(candidate.domain)
    if not normalized_domain:
        raise ValueError("Candidate has no domain to block")
    candidate_before = {
        "domain": normalized_domain,
        "status": candidate.status,
        "reject_reason": candidate.reject_reason,
    }
    candidate.domain = normalized_domain

    result = await db.execute(select(BlockedDomain).where(BlockedDomain.domain == normalized_domain).with_for_update())
    blocked = result.scalars().first()
    before = None
    if blocked:
        before = {"domain": blocked.domain, "reason": blocked.reason}
        blocked.reason = reason or blocked.reason
    else:
        blocked = BlockedDomain(domain=normalized_domain, reason=reason)
        db.add(blocked)

    candidate.status = "rejected"
    candidate.reject_reason = "blocked_domain"

    await db.execute(
        update(ImageCandidate)
        .where(ImageCandidate.id != candidate.id)
        .where(func.lower(func.rtrim(ImageCandidate.domain, ".")) == normalized_domain)
        .where(ImageCandidate.status.in_(BLOCK_DOMAIN_AUTO_REJECT_STATUSES))
        .values(status="auto_rejected", reject_reason="blocked_domain")
    )

    domain_after = {"domain": candidate.domain, "reason": blocked.reason}
    candidate_after = {**domain_after, "status": candidate.status, "reject_reason": candidate.reject_reason}
    for event in [
        AuditEvent(
            actor=actor,
            entity_type="domain",
            entity_id=candidate.id,
            action="domain_blocked",
            before=before,
            after=domain_after,
            comment=reason,
            created_at=datetime.now(timezone.utc),
        ),
        AuditEvent(
            actor=actor,
            entity_type="candidate",
            entity_id=candidate.id,
            action="candidate.domain_blocked",
            before=candidate_before,
            after=candidate_after,
            comment=reason,
            created_at=datetime.now(timezone.utc),
        ),
    ]:
        db.add(event)

    await db.commit()
    await db.refresh(blocked)
    return blocked
