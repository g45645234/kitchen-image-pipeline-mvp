import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.candidate import CandidateReview, ImageCandidate
from app.services.candidate_review_service import get_candidate_review_aggregate, upsert_candidate_review


@pytest.mark.asyncio
async def test_upsert_candidate_review_creates_and_recalculates(db_session, seed_candidate):
    candidate = await seed_candidate()

    review, aggregate = await upsert_candidate_review(
        candidate_id=candidate.id,
        reviewer_name=" Codex ",
        reviewer_version="test",
        score=0.82,
        verdict="approve",
        reason="good match",
        flags={"quality": "good"},
        response_time_ms=456,
        db=db_session,
    )

    assert review.reviewer_name == "codex"
    assert review.mistake_id == candidate.mistake_id
    assert review.side == candidate.side
    assert review.verdict == "pass"
    assert review.flags == {"quality": "good"}
    assert review.response_time_ms == 456
    assert aggregate["review_score"] == 0.82
    assert aggregate["pass_count"] == 1

    refreshed = await db_session.get(ImageCandidate, candidate.id)
    assert float(refreshed.review_score) == 0.82
    assert refreshed.reviewed_by == "multi"
    assert refreshed.reviewed_at is not None
    assert refreshed.status == "auto_reviewed"


@pytest.mark.asyncio
async def test_upsert_same_reviewer_updates_existing_row(db_session, seed_candidate):
    candidate = await seed_candidate()
    first, _ = await upsert_candidate_review(candidate.id, "codex", 0.5, db_session, verdict="maybe")
    second, aggregate = await upsert_candidate_review(candidate.id, "codex", 0.9, db_session, verdict="pass")

    assert second.id == first.id
    assert aggregate["review_score"] == 0.9
    count = await db_session.scalar(select(func.count()).select_from(CandidateReview))
    assert count == 1


@pytest.mark.asyncio
async def test_two_passing_reviews_create_consensus(db_session, seed_candidate):
    candidate = await seed_candidate()
    await upsert_candidate_review(candidate.id, "codex", 0.7, db_session)
    await upsert_candidate_review(candidate.id, "antigravity", 0.95, db_session)

    aggregate = await get_candidate_review_aggregate(candidate.id, db_session)

    assert aggregate["approved_by_consensus"] is True
    assert aggregate["pass_count"] == 2
    assert aggregate["reviewers"] == ["antigravity", "codex"]


@pytest.mark.asyncio
async def test_upsert_rejects_invalid_inputs(db_session, seed_candidate):
    candidate = await seed_candidate()

    with pytest.raises(ValueError):
        await upsert_candidate_review(candidate.id, "codex", 1.5, db_session)
    with pytest.raises(ValueError):
        await upsert_candidate_review(candidate.id, "unknown", 0.5, db_session)
    with pytest.raises(ValueError):
        await upsert_candidate_review(candidate.id, "codex", 0.5, db_session, response_time_ms=-1)
    with pytest.raises(ValueError):
        await upsert_candidate_review(999999, "codex", 0.5, db_session)


@pytest.mark.asyncio
async def test_db_constraints_reject_invalid_review_rows(db_session, seed_candidate):
    candidate = await seed_candidate()
    db_session.add(
        CandidateReview(
            candidate_id=candidate.id,
            mistake_id=candidate.mistake_id,
            side=candidate.side,
            reviewer_name="codex",
            score=2,
            verdict="pass",
            flags={},
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
