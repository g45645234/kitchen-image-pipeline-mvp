from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.config import settings
from app.models.mistake import Mistake
from app.models.video import Video
from app.services.llm_service import extract_mistakes_from_transcript, mock_extract_mistakes_from_transcript


async def extract_mistakes_for_video(video_id: int, db: AsyncSession) -> dict:
    result = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)))
    video = result.scalars().first()
    if not video:
        raise ValueError(f"Video {video_id} not found")
    if not video.transcript:
        raise ValueError("Video transcript is empty")

    provider = settings.mistake_extraction_provider.strip().lower()
    if provider == "anthropic" and settings.anthropic_api_key:
        raw_mistakes = await extract_mistakes_from_transcript(video.transcript)
    else:
        raw_mistakes = mock_extract_mistakes_from_transcript(video.transcript)
        provider = "mock"

    existing = await db.execute(
        select(Mistake.order_index).where(Mistake.video_id == video_id, Mistake.deleted_at.is_(None))
    )
    used_indexes = {row[0] for row in existing.fetchall()}
    next_index = max(used_indexes, default=0) + 1
    created_ids: list[int] = []

    for i, raw in enumerate(raw_mistakes):
        mistake = Mistake(
            video_id=video_id,
            order_index=next_index + i,
            title=raw.get("title") or f"Ошибка {next_index + i}",
            short_title=raw.get("short_title"),
            explanation=raw.get("explanation"),
            wrong_visual_prompt=raw.get("wrong_visual_prompt"),
            right_visual_prompt=raw.get("right_visual_prompt"),
            negative_criteria=raw.get("negative_criteria") or [],
            time_start=raw.get("time_start"),
            time_end=raw.get("time_end"),
        )
        db.add(mistake)
        await db.flush()
        created_ids.append(mistake.id)

    await db.commit()
    return {
        "provider": provider,
        "video_id": video_id,
        "created_mistake_ids": created_ids,
        "created_count": len(created_ids),
    }
