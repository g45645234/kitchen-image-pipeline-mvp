from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.asset import FinalAsset
from app.models.audit import AuditEvent
from app.models.candidate import ImageCandidate
from app.services.candidate_action_service import mark_candidate_as_reference
from app.services.final_asset_service import select_candidate_as_final


async def process_candidate_review(
    candidate_id: int,
    action: str,
    reason: str | None,
    db: AsyncSession,
    comment: str | None = None,
):
    """Review decision job handler used by POST /candidates/{id}/review."""
    if action == "approve":
        action = "approve_final"
    if action not in {"approve_final", "approve_reference", "reject"}:
        raise ValueError("action must be approve_final, approve_reference, or reject")

    if action == "approve_final":
        await select_candidate_as_final(candidate_id, db, reviewed_by="legacy-review")
        return True

    if action == "approve_reference":
        await mark_candidate_as_reference(
            candidate_id=candidate_id,
            db=db,
            mark_high_value=True,
            comment=comment,
            actor="legacy-review",
        )
        return True

    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    before = {
        "status": candidate.status,
        "reject_reason": candidate.reject_reason,
        "reviewed_by": candidate.reviewed_by,
        "reviewed_at": candidate.reviewed_at.isoformat() if candidate.reviewed_at else None,
    }
    candidate.status = "rejected"
    candidate.reject_reason = reason
    candidate.reviewed_by = "legacy-review"
    candidate.reviewed_at = datetime.now(timezone.utc)
    db.add(
        AuditEvent(
            actor="legacy-review",
            entity_type="candidate",
            entity_id=candidate.id,
            action="candidate.rejected",
            before=before,
            after={
                "status": candidate.status,
                "reject_reason": candidate.reject_reason,
                "reviewed_by": candidate.reviewed_by,
                "reviewed_at": candidate.reviewed_at.isoformat() if candidate.reviewed_at else None,
            },
            comment=comment or reason,
            created_at=datetime.now(timezone.utc),
        )
    )

    result = await db.execute(
        select(FinalAsset).where(FinalAsset.candidate_id == candidate.id).with_for_update()
    )
    for asset in result.scalars().all():
        await db.delete(asset)

    await db.commit()
    return True
