from __future__ import annotations

from app.models.candidate import ImageCandidate

IDEAL_ASPECT_RATIO = 16 / 9


def resolution_score(width: int | None, height: int | None) -> float:
    if not width or not height:
        return 0.1
    if width >= 1920 and height >= 1080:
        return 1.0
    if width >= 1280 and height >= 720:
        return 0.8
    if width >= 800 and height >= 500:
        return 0.5
    return 0.1


def aspect_ratio_score(width: int | None, height: int | None) -> float:
    if not width or not height or height <= 0:
        return 0.0
    ratio = width / height
    return max(0.0, 1 - abs(ratio - IDEAL_ASPECT_RATIO) / IDEAL_ASPECT_RATIO)


def source_metadata_score(candidate: ImageCandidate) -> float:
    has_image_url = bool(candidate.image_url)
    has_dimensions = candidate.original_width is not None and candidate.original_height is not None
    has_full_metadata = all([candidate.source_page_url, candidate.image_url, candidate.domain, has_dimensions])
    if has_full_metadata:
        return 1.0
    if has_image_url and has_dimensions:
        return 0.7
    if has_image_url:
        return 0.4
    return 0.0


def quality_flags_for_candidate(candidate: ImageCandidate, scores: dict[str, float]) -> dict[str, bool]:
    width = candidate.original_width
    height = candidate.original_height
    return {
        "low_resolution": bool(width is not None and height is not None and (width < 500 or height < 300)),
        "poor_aspect_ratio": scores["aspect_ratio_score"] < 0.75,
        "missing_metadata": scores["source_metadata_score"] < 1.0,
        "missing_dimensions": width is None or height is None,
        "missing_source_page_url": not bool(candidate.source_page_url),
        "missing_domain": not bool(candidate.domain),
    }


def score_candidate(candidate: ImageCandidate) -> ImageCandidate:
    scores = {
        "resolution_score": resolution_score(candidate.original_width, candidate.original_height),
        "aspect_ratio_score": aspect_ratio_score(candidate.original_width, candidate.original_height),
        "source_metadata_score": source_metadata_score(candidate),
    }
    score_quality = round(
        0.50 * scores["resolution_score"]
        + 0.35 * scores["aspect_ratio_score"]
        + 0.15 * scores["source_metadata_score"],
        4,
    )
    flags = quality_flags_for_candidate(candidate, scores)
    candidate.score_quality = score_quality
    candidate.score_visual = None
    candidate.reference_priority_score = candidate.reference_priority_score
    candidate.review_score = score_quality
    candidate.quality_flags = {**scores, **flags}
    candidate.is_low_quality = any(flags.values())
    return candidate
