from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.audit import AuditEvent
from app.models.asset import FinalAsset
from app.models.candidate import ImageCandidate


async def confirm_candidate_rights(
    candidate_id: int,
    db: AsyncSession,
    rights_status: str,
    comment: str,
    source_url: str | None = None,
    license_note: str | None = None,
    license_document_ref: str | None = None,
    author_name: str | None = None,
    actor: str = "admin-ui",
) -> ImageCandidate:
    comment = comment.strip()
    if not comment:
        raise ValueError("comment is required to confirm rights")
    if rights_status != "manual_licensed":
        raise ValueError("only manual_licensed rights confirmation is supported")

    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    before: dict[str, Any] = {
        "rights_status": candidate.rights_status,
        "may_use_directly": candidate.may_use_directly,
        "source_page_url": candidate.source_page_url,
        "author_name": candidate.author_name,
    }

    candidate.rights_status = rights_status
    candidate.may_use_directly = True
    if source_url:
        candidate.source_page_url = source_url
    if author_name:
        candidate.author_name = author_name

    after: dict[str, Any] = {
        "rights_status": candidate.rights_status,
        "may_use_directly": candidate.may_use_directly,
        "source_url": source_url,
        "license_note": license_note or comment,
        "license_document_ref": license_document_ref,
        "author_name": candidate.author_name,
    }

    for action in ["rights_confirmed", "candidate.rights_confirmed"]:
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

async def confirm_final_asset_rights(
    asset_id: int,
    db: AsyncSession,
    rights_status: str,
    comment: str,
    source_url: str | None = None,
    license_note: str | None = None,
    license_document_ref: str | None = None,
    author_name: str | None = None,
    actor: str = "admin-ui",
) -> FinalAsset:
    comment = comment.strip()
    if not comment:
        raise ValueError("comment is required to confirm rights")
    if rights_status != "manual_licensed":
        raise ValueError("only manual_licensed rights confirmation is supported")

    result = await db.execute(select(FinalAsset).where(FinalAsset.id == asset_id).with_for_update())
    asset = result.scalars().first()
    if not asset:
        raise ValueError(f"Final asset {asset_id} not found")
    if asset.status not in {"approved", "exported"}:
        raise ValueError("only active final assets can have rights confirmed")

    before: dict[str, Any] = {
        "rights_status": asset.rights_status,
        "may_use_directly": asset.may_use_directly,
        "source_url": asset.source_url,
        "license_note": asset.license_note,
        "license_document_ref": asset.license_document_ref,
        "author_name": asset.author_name,
        "rights_confirmed_by": asset.rights_confirmed_by,
        "rights_confirmed_at": asset.rights_confirmed_at.isoformat() if asset.rights_confirmed_at else None,
    }

    asset.rights_status = rights_status
    asset.may_use_directly = True
    if source_url:
        asset.source_url = source_url
    asset.license_note = license_note or comment
    asset.license_document_ref = license_document_ref
    if author_name:
        asset.author_name = author_name
    asset.rights_confirmed_by = actor
    asset.rights_confirmed_at = datetime.now(timezone.utc)
    asset.updated_at = datetime.now(timezone.utc)

    after: dict[str, Any] = {
        "rights_status": asset.rights_status,
        "may_use_directly": asset.may_use_directly,
        "source_url": asset.source_url,
        "license_note": asset.license_note,
        "license_document_ref": asset.license_document_ref,
        "author_name": asset.author_name,
        "rights_confirmed_by": asset.rights_confirmed_by,
        "rights_confirmed_at": asset.rights_confirmed_at.isoformat() if asset.rights_confirmed_at else None,
    }

    db.add(
        AuditEvent(
            actor=actor,
            entity_type="final_asset",
            entity_id=asset.id,
            action="final_asset.rights_confirmed",
            before=before,
            after=after,
            comment=comment,
            created_at=datetime.now(timezone.utc),
        )
    )

    await db.commit()
    await db.refresh(asset)
    return asset
