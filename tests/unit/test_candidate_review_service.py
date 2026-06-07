from types import SimpleNamespace

from app.services.candidate_review_service import build_review_aggregate, normalize_verdict


def review(reviewer_name: str, score: float):
    return SimpleNamespace(reviewer_name=reviewer_name, score=score)


def test_normalize_verdict_keeps_valid_explicit_value():
    assert normalize_verdict(0.95, " maybe ") == "maybe"


def test_normalize_verdict_derives_pass_maybe_fail_from_score():
    assert normalize_verdict(0.7) == "pass"
    assert normalize_verdict(0.4) == "maybe"
    assert normalize_verdict(0.39) == "fail"


def test_normalize_verdict_ignores_unknown_explicit_value():
    assert normalize_verdict(0.8, "approve") == "pass"


def test_build_review_aggregate_empty_reviews():
    aggregate = build_review_aggregate(123, [])

    assert aggregate == {
        "candidate_id": 123,
        "review_score": None,
        "review_count": 0,
        "pass_count": 0,
        "approved_by_consensus": False,
        "reviewers": [],
    }


def test_build_review_aggregate_uses_median_for_three_reviews():
    aggregate = build_review_aggregate(123, [
        review("codex", 0.2),
        review("antigravity", 0.8),
        review("claude_cli", 0.9),
    ])

    assert aggregate["review_score"] == 0.8
    assert aggregate["review_count"] == 3
    assert aggregate["pass_count"] == 2
    assert aggregate["approved_by_consensus"] is True
    assert aggregate["reviewers"] == ["antigravity", "claude_cli", "codex"]


def test_build_review_aggregate_uses_median_for_two_reviews():
    aggregate = build_review_aggregate(123, [
        review("codex", 0.4),
        review("antigravity", 0.8),
    ])

    assert aggregate["review_score"] == 0.6000000000000001
    assert aggregate["pass_count"] == 1
    assert aggregate["approved_by_consensus"] is False


def test_build_review_aggregate_requires_two_passing_scores_for_consensus():
    aggregate = build_review_aggregate(123, [
        review("codex", 0.7),
        review("antigravity", 0.69),
        review("claude_cli", 0.95),
    ])

    assert aggregate["pass_count"] == 2
    assert aggregate["approved_by_consensus"] is True
