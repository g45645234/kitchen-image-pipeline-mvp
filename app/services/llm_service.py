import json
import re
import logging
from anthropic import AsyncAnthropic
from app.config import settings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Ты помогаешь создавать иллюстрации для видео об ошибках в дизайне кухни.
Из расшифровки видео извлеки список ошибок дизайна кухни. Для каждой ошибки заполни:

- title: название ошибки (до 80 символов, на русском)
- short_title: краткое название (до 40 символов, латиницей, kebab-case, для имён файлов)
- explanation: объяснение ошибки (1-2 предложения, на русском)
- wrong_visual_prompt: описание для поиска фото "как делать НЕПРАВИЛЬНО" (на английском, конкретно, для image search)
- right_visual_prompt: описание для поиска фото "как делать ПРАВИЛЬНО" (на английском, конкретно, для image search)
- negative_criteria: список строк — что НЕ должно быть на иллюстрации (на русском, 2-4 пункта)
- time_start: временная метка начала в формате MM:SS (если упоминается в тексте, иначе null)
- time_end: временная метка конца в формате MM:SS (если упоминается в тексте, иначе null)

Верни ТОЛЬКО валидный JSON-массив объектов без markdown-обёртки и без пояснений."""


def mock_extract_mistakes_from_transcript(transcript: str) -> list[dict]:
    compact = " ".join(transcript.split())
    excerpt = compact[:80] or "кухня"
    return [
        {
            "title": f"Черновик ошибки: {excerpt}",
            "short_title": "draft-kitchen-mistake",
            "explanation": "Mock extraction draft. Отредактируйте карточку вручную перед поиском кандидатов.",
            "wrong_visual_prompt": "kitchen design mistake, poor kitchen layout or finishes",
            "right_visual_prompt": "well designed kitchen, corrected layout and finishes",
            "negative_criteria": ["водяные знаки", "текст на изображении", "люди крупным планом"],
            "time_start": None,
            "time_end": None,
        }
    ]


async def extract_mistakes_from_transcript(transcript: str) -> list[dict]:
    if not settings.anthropic_api_key:
        raise ValueError("ANTHROPIC_API_KEY не настроен в .env")

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)

    logger.info("Sending transcript to Claude for mistake extraction")
    response = await client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Расшифровка видео:\n\n{transcript}"
        }]
    )

    text = response.content[0].text.strip()
    # Убираем markdown-блок если Claude всё равно его добавил
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)

    try:
        mistakes = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON: {text[:500]}")
        raise ValueError(f"Claude вернул невалидный JSON: {e}")

    if not isinstance(mistakes, list):
        raise ValueError("Claude вернул не массив")

    return mistakes
