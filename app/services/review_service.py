from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models.candidate import ImageCandidate
from app.models.asset import FinalAsset
from app.models.mistake import Mistake

async def process_candidate_review(candidate_id: int, action: str, reason: str, db: AsyncSession):
    """
    Обрабатывает решение по кандидату. Если approve - переносит в FinalAsset.
    """
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id))
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    candidate.status = "approved" if action == "approve" else "rejected"
    candidate.reject_reason = reason if action == "reject" else None
    
    if action == "approve":
        # Получаем video_id через ошибку
        mistake_result = await db.execute(select(Mistake).where(Mistake.id == candidate.mistake_id))
        mistake = mistake_result.scalars().first()
        
        # Создаем FinalAsset
        asset = FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            candidate_id=candidate.id,
            side=candidate.side,
            source_type=candidate.source_type,
            source_url=candidate.image_url,
            rights_status=candidate.rights_status,
            storage_key_original=candidate.storage_key_original,
            storage_key_thumbnail=candidate.storage_key_thumbnail,
            storage_status="ok",
            status="approved"
        )
        db.add(asset)
        
    await db.commit()
    return True
