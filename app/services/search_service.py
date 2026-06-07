import base64
import hashlib
import logging
import re
import httpx
import xml.etree.ElementTree as ET
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from app.config import settings
from app.models.audit import BlockedDomain
from app.models.mistake import Mistake
from app.models.candidate import SearchQuery, ImageCandidate
from app.services.candidate_scoring_service import score_candidate

logger = logging.getLogger(__name__)

UNSPLASH_API = "https://api.unsplash.com/search/photos"
YANDEX_SEARCH_API = "https://searchapi.api.cloud.yandex.net/v2/image/search"
PIXABAY_API = "https://pixabay.com/api/"
GOOGLE_CSE_API = "https://www.googleapis.com/customsearch/v1"
SUPPORTED_SEARCH_PROVIDERS = {"mock", "mock_search", "yandex_relay", "google", "pixabay", "unsplash", "yandex_search_api"}


async def _search_pixabay(query: str, limit: int, api_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            PIXABAY_API,
            params={
                "key": api_key,
                "q": query,
                "image_type": "photo",
                "orientation": "horizontal",
                "per_page": min(limit, 50),
                "safesearch": "true",
            },
        )
        resp.raise_for_status()
        return resp.json().get("hits", [])


async def _search_google(query: str, limit: int, api_key: str, cse_id: str) -> list[dict]:
    results = []
    # Google CSE возвращает максимум 10 результатов за запрос, до 100 через paging
    for start in range(1, min(limit, 100) + 1, 10):
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                GOOGLE_CSE_API,
                params={
                    "key": api_key,
                    "cx": cse_id,
                    "q": query,
                    "searchType": "image",
                    "imgType": "photo",
                    "imgSize": "large",
                    "num": min(10, limit - len(results)),
                    "start": start,
                    "safe": "off",
                },
            )
            resp.raise_for_status()
            items = resp.json().get("items", [])
            for item in items:
                results.append({
                    "image_url": item["link"],
                    "page_url": item.get("image", {}).get("contextLink"),
                    "thumb_url": item.get("image", {}).get("thumbnailLink"),
                    "width": item.get("image", {}).get("width"),
                    "height": item.get("image", {}).get("height"),
                    "title": item.get("title", ""),
                    "domain": item.get("displayLink", ""),
                })
            if len(items) < 10 or len(results) >= limit:
                break
    return results


async def _search_unsplash(query: str, limit: int, access_key: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            UNSPLASH_API,
            params={"query": query, "per_page": min(limit, 30), "orientation": "landscape"},
            headers={"Authorization": f"Client-ID {access_key}"},
        )
        resp.raise_for_status()
        return resp.json().get("results", [])


async def _search_yandex_relay(
    query: str, limit: int, relay_url: str, relay_secret: str, folder_id: str
) -> list[dict]:
    """Поиск через Yandex Search API v2 через relay-VM в Yandex Cloud."""
    payload = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
            "familyMode": "FAMILY_MODE_MODERATE",
        },
        "imageSpec": {"orientation": "IMAGE_ORIENTATION_HORIZONTAL"},
        "docsOnPage": str(min(limit, 50)),
        "folderId": folder_id,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{relay_url}/v2/image/search",
            json=payload,
            headers={"x-relay-secret": relay_secret},
        )
        resp.raise_for_status()
        data = resp.json()

    raw_b64 = data.get("rawData") or data.get("raw_data", "")
    if not raw_b64:
        logger.warning("Yandex relay: пустой rawData в ответе")
        return []

    xml_bytes = base64.b64decode(raw_b64)
    return _parse_yandex_xml(xml_bytes.decode("utf-8", errors="replace"))


async def _search_yandex_search_api(
    query: str, limit: int, api_key: str, folder_id: str
) -> list[dict]:
    """Поиск изображений через Yandex Search API v2 (прямой вызов, только из YC)."""
    payload = {
        "query": {
            "searchType": "SEARCH_TYPE_RU",
            "queryText": query,
            "familyMode": "FAMILY_MODE_MODERATE",
        },
        "imageSpec": {"orientation": "IMAGE_ORIENTATION_HORIZONTAL"},
        "docsOnPage": str(min(limit, 50)),
        "folderId": folder_id,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            YANDEX_SEARCH_API,
            json=payload,
            headers={"Authorization": f"Api-Key {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()

    raw_b64 = data.get("rawData") or data.get("raw_data", "")
    if not raw_b64:
        logger.warning("Yandex Search API: пустой rawData в ответе")
        return []

    xml_bytes = base64.b64decode(raw_b64)
    return _parse_yandex_xml(xml_bytes.decode("utf-8", errors="replace"))


def _parse_yandex_xml(xml_text: str) -> list[dict]:
    """Разбирает XML-ответ Яндекса v2, извлекает данные об изображениях."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        logger.error(f"Ошибка парсинга XML от Яндекса: {e}")
        return []

    error = root.find(".//error")
    if error is not None:
        raise ValueError(f"Яндекс Search API ошибка: {error.text}")

    results = []
    for doc in root.findall(".//doc"):
        img_props = doc.find("image-properties")
        if img_props is None:
            continue

        img_url = (img_props.findtext("image-link") or "").strip()
        if not img_url:
            continue

        thumb_url = (img_props.findtext("thumbnail-link") or "").strip() or None
        page_url = (img_props.findtext("html-link") or "").strip() or None
        orig_w = img_props.findtext("original-width")
        orig_h = img_props.findtext("original-height")
        domain = doc.findtext("domain", "")

        results.append({
            "image_url": img_url,
            "page_url": page_url,
            "thumb_url": thumb_url,
            "width": int(orig_w) if orig_w and orig_w.isdigit() else None,
            "height": int(orig_h) if orig_h and orig_h.isdigit() else None,
            "domain": domain,
        })

    return results


def _mock_candidates(mistake_id: int, side: str, count: int = 2) -> list[dict]:
    results = []
    for i in range(count):
        img_url = f"https://mock-image-server.local/images/{mistake_id}_{side}_{i}.jpg"
        results.append({"_mock": True, "url": img_url})
    return results


def _normalize_domain(domain: str | None) -> str | None:
    normalized = (domain or "").strip().rstrip(".").lower()
    return normalized or None


async def _apply_blocked_domain_policy(db: AsyncSession, candidate: ImageCandidate) -> None:
    candidate.domain = _normalize_domain(candidate.domain)
    if not candidate.domain:
        return
    blocked = await db.scalar(select(BlockedDomain).where(BlockedDomain.domain == candidate.domain))
    if blocked:
        candidate.status = "auto_rejected"
        candidate.reject_reason = "blocked_domain"


async def _upsert_candidate(db: AsyncSession, candidate: ImageCandidate):
    """INSERT ... ON CONFLICT (mistake_id, side, image_url_hash) DO NOTHING."""
    await _apply_blocked_domain_policy(db, candidate)
    score_candidate(candidate)
    stmt = (
        pg_insert(ImageCandidate)
        .values(
            mistake_id=candidate.mistake_id,
            query_id=candidate.query_id,
            side=candidate.side,
            source_type=candidate.source_type,
            source_provider=candidate.source_provider,
            source_page_url=candidate.source_page_url,
            image_url=candidate.image_url,
            image_url_hash=candidate.image_url_hash,
            thumbnail_url=candidate.thumbnail_url,
            status=candidate.status,
            reject_reason=candidate.reject_reason,
            rights_status=candidate.rights_status,
            original_width=candidate.original_width,
            original_height=candidate.original_height,
            domain=candidate.domain,
            author_name=candidate.author_name,
            license_label=candidate.license_label,
            may_use_directly=candidate.may_use_directly,
            score_quality=candidate.score_quality,
            score_visual=candidate.score_visual,
            reference_priority_score=candidate.reference_priority_score,
            review_score=candidate.review_score,
            quality_flags=candidate.quality_flags,
            is_low_quality=candidate.is_low_quality,
        )
        .on_conflict_do_nothing(
            index_elements=["mistake_id", "side", "image_url_hash"]
        )
    )
    await db.execute(stmt)


_WRONG_MARKERS = "dark poor dim cluttered bad design mistake"
_RIGHT_MARKERS = "bright clean modern professional well-lit beautiful"

_STOPWORDS = {"with", "and", "the", "for", "over", "from", "into", "that",
              "this", "have", "than", "but", "not", "are", "was", "were",
              "be", "been", "being", "or", "on", "in", "of", "to", "a",
              "an", "at", "by", "as", "is", "it", "its"}


_MIN_WIDTH = 400
_MIN_HEIGHT = 300


def _size_ok(photo: dict) -> bool:
    w = photo.get("width") or photo.get("original_width") or photo.get("imageWidth")
    h = photo.get("height") or photo.get("original_height") or photo.get("imageHeight")
    if w is None or h is None:
        return True  # размер неизвестен — не отбрасываем
    return int(w) >= _MIN_WIDTH and int(h) >= _MIN_HEIGHT


def _build_pixabay_query(prompt: str, side: str) -> str:
    """Превращает длинный визуальный промпт в короткий запрос для Pixabay с контрастными маркерами."""
    words = [w.strip(",.;:\"'") for w in prompt.lower().split()]
    keywords = [w for w in words if w and w not in _STOPWORDS and len(w) > 2]
    short = " ".join(keywords[:6])
    markers = _WRONG_MARKERS if side == "wrong" else _RIGHT_MARKERS
    return f"{short} {markers}"


_WRONG_TO_RIGHT_RU = [
    (re.compile(r"\bслишком\s+тёмн(\w*)\b", re.IGNORECASE), r"светл\1"),
    (re.compile(r"\bтёмн(\w*)\b", re.IGNORECASE), r"светл\1"),
    (re.compile(r"\bнеправильн(\w*)\b", re.IGNORECASE), r"правильн\1"),
    (re.compile(r"\bотсутствие\b", re.IGNORECASE), "наличие"),
    (re.compile(r"\bплохой\b", re.IGNORECASE), "хороший"),
    (re.compile(r"\bплохое\b", re.IGNORECASE), "хорошее"),
    (re.compile(r"\bплохая\b", re.IGNORECASE), "хорошая"),
]


def _build_yandex_query(title: str, side: str) -> str:
    """Строит русский поисковый запрос для Яндекса.
    wrong — запрос с негативным описанием проблемы.
    right — запрос с противоположными ключевыми словами (тёмный→светлый, неправильн→правильн и т.д.)
    """
    if side == "wrong":
        return f"кухня {title}"
    right_title = title
    for pattern, replacement in _WRONG_TO_RIGHT_RU:
        right_title = pattern.sub(replacement, right_title)
    return f"кухня {right_title} интерьер"


def _normalize_provider(provider: str) -> str:
    return "mock_search" if provider == "mock" else provider


def normalize_search_provider(provider: str) -> str:
    return _normalize_provider(provider)


def _pick_provider() -> str:
    """Выбирает провайдер по приоритету: yandex_relay > google > pixabay > unsplash > mock."""
    if settings.yandex_relay_url and settings.yandex_relay_secret and settings.yandex_folder_id:
        return "yandex_relay"
    if settings.google_api_key and settings.google_cse_id:
        return "google"
    if settings.pixabay_api_key:
        return "pixabay"
    if settings.unsplash_access_key:
        return "unsplash"
    if settings.yandex_api_key and settings.yandex_folder_id:
        return "yandex_search_api"
    return "mock_search"


def _query_text_for_mistake(mistake: Mistake, side: str, provider: str) -> str | None:
    prompt = mistake.wrong_visual_prompt if side == "wrong" else mistake.right_visual_prompt
    if not prompt:
        return None
    if provider == "pixabay":
        return _build_pixabay_query(prompt, side)
    if provider in ("yandex_relay", "yandex_search_api"):
        return _build_yandex_query(mistake.title, side)
    return prompt


def _default_results_count(provider: str) -> int:
    if provider in ("yandex_relay", "yandex_search_api"):
        return 50
    if provider == "mock_search":
        return 2
    return 10


def _effective_limit(value: int | None, provider: str) -> int:
    return min(value or _default_results_count(provider), settings.max_search_limit_per_query)


def _missing_provider_credentials(provider: str) -> str | None:
    if provider == "yandex_relay" and not (settings.yandex_relay_url and settings.yandex_relay_secret and settings.yandex_folder_id):
        return "credentials_missing"
    if provider == "google" and not (settings.google_api_key and settings.google_cse_id):
        return "credentials_missing"
    if provider == "pixabay" and not settings.pixabay_api_key:
        return "credentials_missing"
    if provider == "unsplash" and not settings.unsplash_access_key:
        return "credentials_missing"
    if provider == "yandex_search_api" and not (settings.yandex_api_key and settings.yandex_folder_id):
        return "credentials_missing"
    return None


def _provider_error_code(error: Exception) -> str:
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.HTTPStatusError):
        status_code = error.response.status_code
        if status_code in {401, 403}:
            return "credentials_missing"
        if status_code == 429:
            return "rate_limited"
        if status_code >= 500:
            return "provider_unavailable"
        return "invalid_response"
    if isinstance(error, (httpx.TransportError, httpx.HTTPError)):
        return "provider_unavailable"
    if isinstance(error, (ValueError, KeyError, TypeError)):
        return "invalid_response"
    return "unknown_error"


def _mark_query_failed(query: SearchQuery, provider: str, code: str, detail: str | None = None) -> None:
    query.status = "failed"
    query.error_message = f"{provider}: {code}" + (f" ({detail[:300]})" if detail else "")


def _search_result_payload(mistake_id: int, provider: str, queries: list[SearchQuery]) -> dict:
    completed = [query for query in queries if query.status == "completed"]
    failed = [query for query in queries if query.status == "failed"]
    if failed and completed:
        status = "partially_failed"
    elif failed and not completed:
        status = "failed"
    else:
        status = "completed"
    return {
        "status": status,
        "mistake_id": mistake_id,
        "provider": provider,
        "queries": [
            {
                "id": query.id,
                "side": query.side,
                "status": query.status,
                "results_count": query.results_count,
                "error_message": query.error_message,
            }
            for query in queries
        ],
    }


async def generate_search_queries_for_mistake(
    mistake_id: int,
    db: AsyncSession,
    sides: list[str] | None = None,
    provider: str | None = None,
    limit_per_query: int | None = None,
) -> list[SearchQuery]:
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None)))
    mistake = result.scalars().first()
    if not mistake:
        raise ValueError(f"Mistake {mistake_id} not found")

    provider = _normalize_provider(provider or _pick_provider())
    if provider not in SUPPORTED_SEARCH_PROVIDERS:
        raise ValueError(f"Invalid provider: {provider}")
    requested_sides = sides or ["wrong", "right"]
    invalid_sides = [side for side in requested_sides if side not in {"wrong", "right"}]
    if invalid_sides:
        raise ValueError(f"Invalid sides: {', '.join(invalid_sides)}")

    queries: list[SearchQuery] = []
    for side in requested_sides:
        query_text = _query_text_for_mistake(mistake, side, provider)
        if not query_text:
            continue
        result_existing = await db.execute(
            select(SearchQuery)
            .where(
                SearchQuery.mistake_id == mistake_id,
                SearchQuery.side == side,
                SearchQuery.source_provider == provider,
            )
            .order_by(SearchQuery.id.desc())
            .limit(1)
        )
        query = result_existing.scalars().first()
        if not query:
            query = SearchQuery(mistake_id=mistake_id, side=side, source_provider=provider)
            db.add(query)
        query.query_text = query_text
        query.language = "ru" if provider in {"yandex_relay", "yandex_search_api"} else "unknown"
        query.status = "pending"
        query.results_count = _effective_limit(limit_per_query, provider)
        query.error_message = None
        queries.append(query)

    await db.commit()
    for query in queries:
        await db.refresh(query)
    return queries


async def _execute_prepared_search_queries(
    mistake_id: int,
    provider: str,
    queries: list[SearchQuery],
    db: AsyncSession,
) -> dict:
    for q in queries:
        if provider == "yandex_relay":
            missing_credentials = _missing_provider_credentials(provider)
            if missing_credentials:
                _mark_query_failed(q, provider, missing_credentials)
                continue
            try:
                photos = await _search_yandex_relay(
                    q.query_text,
                    q.results_count or 10,
                    settings.yandex_relay_url,
                    settings.yandex_relay_secret,
                    settings.yandex_folder_id,
                )
            except Exception as e:
                logger.warning(f"Yandex relay search failed: {e}")
                _mark_query_failed(q, provider, _provider_error_code(e), str(e))
                continue

            for photo in photos:
                if not _size_ok(photo):
                    continue
                img_url = photo["image_url"]
                img_hash = hashlib.md5(img_url.encode()).hexdigest()
                candidate = ImageCandidate(
                    mistake_id=mistake_id,
                    query_id=q.id,
                    side=q.side,
                    source_type="search",
                    source_provider="yandex",
                    source_page_url=photo.get("page_url"),
                    image_url=img_url,
                    image_url_hash=img_hash,
                    thumbnail_url=photo.get("thumb_url"),
                    status="new",
                    rights_status="unknown",
                    original_width=photo.get("width"),
                    original_height=photo.get("height"),
                    domain=photo.get("domain") or None,
                    may_use_directly=False,
                )
                await _upsert_candidate(db, candidate)

        elif provider == "google":
            missing_credentials = _missing_provider_credentials(provider)
            if missing_credentials:
                _mark_query_failed(q, provider, missing_credentials)
                continue
            try:
                photos = await _search_google(
                    q.query_text, q.results_count or 10,
                    settings.google_api_key, settings.google_cse_id,
                )
            except Exception as e:
                logger.warning(f"Google search failed: {e}")
                _mark_query_failed(q, provider, _provider_error_code(e), str(e))
                continue

            for photo in photos:
                img_url = photo["image_url"]
                if not img_url:
                    continue
                img_hash = hashlib.md5(img_url.encode()).hexdigest()
                candidate = ImageCandidate(
                    mistake_id=mistake_id,
                    query_id=q.id,
                    side=q.side,
                    source_type="search",
                    source_provider="google",
                    source_page_url=photo.get("page_url"),
                    image_url=img_url,
                    image_url_hash=img_hash,
                    thumbnail_url=photo.get("thumb_url"),
                    status="new",
                    rights_status="unknown",
                    original_width=photo.get("width"),
                    original_height=photo.get("height"),
                    domain=photo.get("domain") or None,
                    may_use_directly=False,
                )
                await _upsert_candidate(db, candidate)

        elif provider == "pixabay":
            missing_credentials = _missing_provider_credentials(provider)
            if missing_credentials:
                _mark_query_failed(q, provider, missing_credentials)
                continue
            try:
                photos = await _search_pixabay(q.query_text, q.results_count or 10, settings.pixabay_api_key)
            except Exception as e:
                logger.warning(f"Pixabay search failed: {e}")
                _mark_query_failed(q, provider, _provider_error_code(e), str(e))
                continue

            for photo in photos:
                img_url = photo.get("largeImageURL") or photo.get("webformatURL", "")
                if not img_url:
                    continue
                img_hash = hashlib.md5(img_url.encode()).hexdigest()
                candidate = ImageCandidate(
                    mistake_id=mistake_id,
                    query_id=q.id,
                    side=q.side,
                    source_type="search",
                    source_provider="pixabay",
                    source_page_url=photo.get("pageURL"),
                    image_url=img_url,
                    image_url_hash=img_hash,
                    thumbnail_url=photo.get("previewURL"),
                    status="new",
                    rights_status="free_to_use",
                    original_width=photo.get("imageWidth"),
                    original_height=photo.get("imageHeight"),
                    author_name=photo.get("user"),
                    license_label="Pixabay License",
                    may_use_directly=True,
                )
                await _upsert_candidate(db, candidate)

        elif provider == "yandex_search_api":
            missing_credentials = _missing_provider_credentials(provider)
            if missing_credentials:
                _mark_query_failed(q, provider, missing_credentials)
                continue
            try:
                photos = await _search_yandex_search_api(
                    q.query_text,
                    q.results_count or 10,
                    settings.yandex_api_key,
                    settings.yandex_folder_id,
                )
            except Exception as e:
                logger.warning(f"Yandex Search API failed: {e}")
                _mark_query_failed(q, provider, _provider_error_code(e), str(e))
                continue

            for photo in photos:
                if not _size_ok(photo):
                    continue
                img_url = photo["image_url"]
                img_hash = hashlib.md5(img_url.encode()).hexdigest()
                candidate = ImageCandidate(
                    mistake_id=mistake_id,
                    query_id=q.id,
                    side=q.side,
                    source_type="search",
                    source_provider="yandex_search_api",
                    source_page_url=photo.get("page_url"),
                    image_url=img_url,
                    image_url_hash=img_hash,
                    thumbnail_url=photo.get("thumb_url"),
                    status="new",
                    rights_status="unknown",
                    original_width=photo.get("width"),
                    original_height=photo.get("height"),
                    domain=photo.get("domain") or None,
                    may_use_directly=False,
                )
                await _upsert_candidate(db, candidate)

        elif provider == "unsplash":
            missing_credentials = _missing_provider_credentials(provider)
            if missing_credentials:
                _mark_query_failed(q, provider, missing_credentials)
                continue
            try:
                photos = await _search_unsplash(q.query_text, q.results_count or 5, settings.unsplash_access_key)
            except Exception as e:
                logger.warning(f"Unsplash search failed: {e}")
                _mark_query_failed(q, provider, _provider_error_code(e), str(e))
                continue

            for photo in photos:
                img_url = photo["urls"]["regular"]
                img_hash = hashlib.md5(img_url.encode()).hexdigest()
                candidate = ImageCandidate(
                    mistake_id=mistake_id,
                    query_id=q.id,
                    side=q.side,
                    source_type="search",
                    source_provider="unsplash",
                    source_page_url=photo.get("links", {}).get("html"),
                    image_url=img_url,
                    image_url_hash=img_hash,
                    status="new",
                    rights_status="free_to_use",
                    original_width=photo.get("width"),
                    original_height=photo.get("height"),
                    author_name=photo.get("user", {}).get("name"),
                    license_label="Unsplash License",
                    may_use_directly=True,
                )
                await _upsert_candidate(db, candidate)

        else:
            for item in _mock_candidates(mistake_id, q.side, q.results_count or 2):
                img_url = item["url"]
                img_hash = hashlib.md5(img_url.encode()).hexdigest()
                candidate = ImageCandidate(
                    mistake_id=mistake_id,
                    query_id=q.id,
                    side=q.side,
                    source_type="search",
                    source_provider="mock_search",
                    image_url=img_url,
                    image_url_hash=img_hash,
                    status="new",
                    rights_status="unknown",
                    may_use_directly=False,
                )
                await _upsert_candidate(db, candidate)

    for q in queries:
        if q.status == "running":
            q.status = "completed"
            q.error_message = None
    await db.commit()
    return _search_result_payload(mistake_id, provider, queries)


async def execute_search_query(query_id: int, db: AsyncSession, limit_per_query: int | None = None):
    result = await db.execute(select(SearchQuery).where(SearchQuery.id == query_id))
    query = result.scalars().first()
    if not query:
        raise ValueError(f"SearchQuery {query_id} not found")

    result_mistake = await db.execute(
        select(Mistake).where(Mistake.id == query.mistake_id, Mistake.deleted_at.is_(None))
    )
    if not result_mistake.scalars().first():
        raise ValueError(f"Mistake {query.mistake_id} not found")

    provider = _normalize_provider(query.source_provider)
    if provider not in SUPPORTED_SEARCH_PROVIDERS:
        raise ValueError(f"Invalid provider: {provider}")
    if query.side not in {"wrong", "right"}:
        raise ValueError(f"Invalid side: {query.side}")
    if not query.query_text or not query.query_text.strip():
        raise ValueError(f"SearchQuery {query_id} has empty query_text")

    query.source_provider = provider
    query.status = "running"
    query.results_count = _effective_limit(limit_per_query or query.results_count, provider)
    query.error_message = None
    await db.commit()
    await db.refresh(query)

    return await _execute_prepared_search_queries(query.mistake_id, provider, [query], db)


async def execute_search_for_mistake(
    mistake_id: int,
    db: AsyncSession,
    sides: list[str] | None = None,
    provider: str | None = None,
    limit_per_query: int | None = None,
):
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None)))
    mistake = result.scalars().first()
    if not mistake:
        raise ValueError(f"Mistake {mistake_id} not found")

    provider = _normalize_provider(provider or _pick_provider())
    if provider not in SUPPORTED_SEARCH_PROVIDERS:
        raise ValueError(f"Invalid provider: {provider}")
    requested_sides = sides or ["wrong", "right"]
    invalid_sides = [side for side in requested_sides if side not in {"wrong", "right"}]
    if invalid_sides:
        raise ValueError(f"Invalid sides: {', '.join(invalid_sides)}")
    logger.info(f"Search provider: {provider} for mistake {mistake_id}")

    result_queries = await db.execute(
        select(SearchQuery)
        .where(
            SearchQuery.mistake_id == mistake_id,
            SearchQuery.source_provider == provider,
            SearchQuery.side.in_(requested_sides),
        )
        .order_by(SearchQuery.side, SearchQuery.id.desc())
    )
    existing_by_side: dict[str, SearchQuery] = {}
    for query in result_queries.scalars().all():
        existing_by_side.setdefault(query.side, query)

    queries = []
    for side in requested_sides:
        query_text = _query_text_for_mistake(mistake, side, provider)
        if not query_text:
            continue
        q = existing_by_side.get(side)
        if not q:
            q = SearchQuery(mistake_id=mistake_id, side=side, source_provider=provider)
            db.add(q)
        q.query_text = query_text
        q.status = "running"
        q.results_count = _effective_limit(limit_per_query or q.results_count, provider)
        q.error_message = None
        queries.append(q)

    await db.commit()
    for q in queries:
        await db.refresh(q)

    return await _execute_prepared_search_queries(mistake_id, provider, queries, db)
