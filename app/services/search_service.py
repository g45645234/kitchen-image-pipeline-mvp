import hashlib
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from app.models.mistake import Mistake
from app.models.candidate import SearchQuery, ImageCandidate

async def execute_search_for_mistake(mistake_id: int, db: AsyncSession):
    """
    Симулирует поиск картинок (кандидатов) для конкретной ошибки дизайна.
    """
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id))
    mistake = result.scalars().first()
    if not mistake:
        raise ValueError(f"Mistake {mistake_id} not found")

    # 1. Симулируем создание поисковых запросов
    queries = []
    for side in ['wrong', 'right']:
        prompt = mistake.wrong_visual_prompt if side == 'wrong' else mistake.right_visual_prompt
        if not prompt:
            continue
            
        q = SearchQuery(
            mistake_id=mistake_id,
            side=side,
            source_provider="mock_search",
            query_text=prompt,
            status="completed",
            results_count=2
        )
        db.add(q)
        queries.append(q)
    
    await db.commit()
    for q in queries:
        await db.refresh(q)

    # 2. Симулируем нахождение кандидатов
    for q in queries:
        for i in range(q.results_count or 2):
            img_url = f"https://mock-image-server.local/images/{mistake_id}_{q.side}_{i}.jpg"
            img_hash = hashlib.md5(img_url.encode()).hexdigest()
            
            candidate = ImageCandidate(
                mistake_id=mistake_id,
                query_id=q.id,
                side=q.side,
                source_type="search",
                source_provider=q.source_provider,
                image_url=img_url,
                image_url_hash=img_hash,
                status="new",
                rights_status="unknown"
            )
            db.add(candidate)
            
    await db.commit()
    return True
