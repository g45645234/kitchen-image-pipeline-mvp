from app.models.candidate import ImageCandidate
from app.services.candidate_scoring_service import aspect_ratio_score, resolution_score, score_candidate


def candidate(**overrides):
    base = {
        "image_url": "https://example.com/image.jpg",
        "source_page_url": "https://example.com/page",
        "domain": "example.com",
        "original_width": 1920,
        "original_height": 1080,
        "rights_status": "unknown",
        "may_use_directly": False,
    }
    base.update(overrides)
    return ImageCandidate(**base)


def test_resolution_score_thresholds():
    assert resolution_score(1920, 1080) == 1.0
    assert resolution_score(1280, 720) == 0.8
    assert resolution_score(800, 500) == 0.5
    assert resolution_score(499, 300) == 0.1


def test_aspect_ratio_score_prefers_16_by_9():
    assert aspect_ratio_score(1920, 1080) == 1.0
    assert round(aspect_ratio_score(1080, 1920), 4) == 0.3164


def test_score_candidate_sets_quality_review_score_without_rights_penalty():
    unknown_rights = score_candidate(candidate(rights_status="unknown", may_use_directly=False))
    licensed = score_candidate(candidate(rights_status="free_to_use", may_use_directly=True))

    assert float(unknown_rights.score_quality) == 1.0
    assert float(unknown_rights.review_score) == 1.0
    assert unknown_rights.score_visual is None
    assert unknown_rights.reference_priority_score is None
    assert unknown_rights.is_low_quality is False
    assert unknown_rights.quality_flags["resolution_score"] == 1.0
    assert float(licensed.score_quality) == float(unknown_rights.score_quality)


def test_score_candidate_marks_low_quality_without_rejecting():
    scored = score_candidate(candidate(source_page_url=None, domain=None, original_width=400, original_height=250))

    assert scored.status is None
    assert scored.is_low_quality is True
    assert scored.quality_flags["low_resolution"] is True
    assert scored.quality_flags["missing_metadata"] is True
    assert float(scored.score_quality) < 0.5
