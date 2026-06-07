from __future__ import annotations

from datetime import datetime, timezone
from statistics import median
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from app.models.candidate import CandidateReview, ImageCandidate

PASS_THRESHOLD = 0.7
REQUIRED_PASS_COUNT = 2
EXPECTED_REVIEWERS = ("codex", "antigravity", "claude_cli")


def normalize_verdict(score: float, verdict: str | None = None) -> str:
    if verdict:
        normalized = verdict.strip().lower()
        if normalized in {"pass", "maybe", "fail"}:
            return normalized
    if score >= PASS_THRESHOLD:
        return "pass"
    if score >= 0.4:
        return "maybe"
    return "fail"


def build_review_aggregate(candidate_id: int, reviews: list[CandidateReview]) -> dict[str, Any]:
    scores = [float(r.score) for r in reviews]
    review_score = float(median(scores)) if scores else None
    pass_count = sum(1 for score in scores if score >= PASS_THRESHOLD)
    reviewers = sorted({r.reviewer_name for r in reviews})
    return {
        "candidate_id": candidate_id,
        "review_score": review_score,
        "review_count": len(reviews),
        "pass_count": pass_count,
        "approved_by_consensus": pass_count >= REQUIRED_PASS_COUNT,
        "reviewers": reviewers,
    }


async def get_candidate_review_aggregate(
    candidate_id: int,
    db: AsyncSession,
) -> dict[str, Any]:
    result = await db.execute(select(ImageCandidate.id).where(ImageCandidate.id == candidate_id))
    if not result.first():
        raise ValueError(f"Candidate {candidate_id} not found")

    result = await db.execute(
        select(CandidateReview)
        .where(CandidateReview.candidate_id == candidate_id)
        .order_by(CandidateReview.reviewer_name)
    )
    reviews = result.scalars().all()
    return build_review_aggregate(candidate_id, reviews)


async def recalculate_candidate_review_aggregate(
    candidate_id: int,
    db: AsyncSession,
) -> dict[str, Any]:
    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    result = await db.execute(
        select(CandidateReview)
        .where(CandidateReview.candidate_id == candidate_id)
        .order_by(CandidateReview.reviewer_name)
    )
    reviews = result.scalars().all()
    aggregate = build_review_aggregate(candidate_id, reviews)

    candidate.review_score = aggregate["review_score"]
    candidate.reviewed_by = "multi" if reviews else None
    candidate.reviewed_at = datetime.now(timezone.utc) if reviews else None
    if reviews and candidate.status == "new":
        candidate.status = "auto_reviewed"

    await db.flush()
    return aggregate


async def upsert_candidate_review(
    candidate_id: int,
    reviewer_name: str,
    score: float,
    db: AsyncSession,
    reviewer_version: str | None = None,
    verdict: str | None = None,
    reason: str | None = None,
    flags: dict[str, Any] | None = None,
    response_time_ms: int | None = None,
) -> tuple[CandidateReview, dict[str, Any]]:
    if not 0 <= score <= 1:
        raise ValueError("score must be between 0 and 1")
    if response_time_ms is not None and response_time_ms < 0:
        raise ValueError("response_time_ms must be non-negative")

    reviewer_name = reviewer_name.strip().lower()
    if reviewer_name not in EXPECTED_REVIEWERS:
        raise ValueError(f"reviewer_name must be one of: {', '.join(EXPECTED_REVIEWERS)}")

    result = await db.execute(select(ImageCandidate).where(ImageCandidate.id == candidate_id).with_for_update())
    candidate = result.scalars().first()
    if not candidate:
        raise ValueError(f"Candidate {candidate_id} not found")

    result = await db.execute(
        select(CandidateReview).where(
            CandidateReview.candidate_id == candidate_id,
            CandidateReview.reviewer_name == reviewer_name,
        )
    )
    review = result.scalars().first()
    if not review:
        review = CandidateReview(
            candidate_id=candidate.id,
            mistake_id=candidate.mistake_id,
            side=candidate.side,
            reviewer_name=reviewer_name,
        )
        db.add(review)

    review.reviewer_version = reviewer_version
    review.score = score
    review.verdict = normalize_verdict(score, verdict)
    review.reason = reason
    review.flags = flags or {}
    review.response_time_ms = response_time_ms

    await db.flush()
    aggregate = await recalculate_candidate_review_aggregate(candidate_id, db)
    await db.commit()
    await db.refresh(review)
    return review, aggregate
