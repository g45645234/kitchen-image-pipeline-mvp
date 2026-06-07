from fastapi import APIRouter, Request, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path
from urllib.parse import urlencode
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import and_, func, or_
from sqlalchemy.future import select

from app.auth import require_admin_api_token
from app.db import get_db
from app.services.candidate_status import candidate_status_filter_values
from app.services.candidate_review_runner import get_reviewer_cli_readiness
from app.services.export_service import build_video_export_readiness
from app.services.final_asset_service import final_asset_health
from app.services.candidate_review_service import (
    EXPECTED_REVIEWERS,
    PASS_THRESHOLD,
    REQUIRED_PASS_COUNT,
    build_review_aggregate,
)
from app.models.asset import FinalAsset
from app.models.mistake import Mistake
from app.models.video import Video
from app.models.job import Job
from app.models.candidate import CandidateReview, ImageCandidate, ReferenceBrief, SearchQuery

router = APIRouter(tags=["ui"], dependencies=[Depends(require_admin_api_token)])
templates = Jinja2Templates(directory=Path(__file__).parent.parent / "templates")

def _ui_prefix(request: Request) -> str:
    return "/admin" if request.url.path.startswith("/admin") else "/ui"


@router.get("", response_class=HTMLResponse)
async def admin_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    return await list_admin_videos(request, db)


@router.get("/videos", response_class=HTMLResponse)
async def list_admin_videos(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.deleted_at.is_(None)).order_by(Video.created_at.desc()))
    videos = result.scalars().all()

    counts = {}
    if videos:
        video_ids = [video.id for video in videos]
        mistake_counts = await db.execute(
            select(Mistake.video_id, func.count(Mistake.id))
            .where(Mistake.video_id.in_(video_ids))
            .group_by(Mistake.video_id)
        )
        final_counts = await db.execute(
            select(FinalAsset.video_id, func.count(FinalAsset.id))
            .where(FinalAsset.video_id.in_(video_ids), FinalAsset.status.in_(["approved", "exported"]))
            .group_by(FinalAsset.video_id)
        )
        counts = {video.id: {"mistakes": 0, "final_assets": 0} for video in videos}
        for video_id, count in mistake_counts.all():
            counts[video_id]["mistakes"] = count
        for video_id, count in final_counts.all():
            counts[video_id]["final_assets"] = count

    return templates.TemplateResponse(
        request=request,
        name="videos.html",
        context={"videos": videos, "counts": counts, "ui_prefix": _ui_prefix(request)},
    )


@router.get("/videos/{video_id}/mistakes", response_class=HTMLResponse)
async def list_admin_video_mistakes(video_id: int, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Video).where(Video.id == video_id, Video.deleted_at.is_(None)))
    video = result.scalars().first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    result = await db.execute(select(Mistake).where(Mistake.video_id == video_id, Mistake.deleted_at.is_(None)).order_by(Mistake.order_index))
    mistakes = result.scalars().all()

    mistake_ids = [mistake.id for mistake in mistakes]
    candidate_counts = {mistake.id: 0 for mistake in mistakes}
    final_counts = {mistake.id: {"wrong": 0, "right": 0} for mistake in mistakes}
    search_query_counts = {mistake.id: 0 for mistake in mistakes}
    if mistake_ids:
        candidate_result = await db.execute(
            select(ImageCandidate.mistake_id, func.count(ImageCandidate.id))
            .where(ImageCandidate.mistake_id.in_(mistake_ids))
            .group_by(ImageCandidate.mistake_id)
        )
        for mistake_id, count in candidate_result.all():
            candidate_counts[mistake_id] = count

        final_result = await db.execute(
            select(FinalAsset.mistake_id, FinalAsset.side, func.count(FinalAsset.id))
            .where(FinalAsset.mistake_id.in_(mistake_ids), FinalAsset.status.in_(["approved", "exported"]))
            .group_by(FinalAsset.mistake_id, FinalAsset.side)
        )
        for mistake_id, side, count in final_result.all():
            final_counts[mistake_id][side] = count

        search_query_result = await db.execute(
            select(SearchQuery.mistake_id, func.count(SearchQuery.id))
            .where(SearchQuery.mistake_id.in_(mistake_ids))
            .group_by(SearchQuery.mistake_id)
        )
        for mistake_id, count in search_query_result.all():
            search_query_counts[mistake_id] = count

    export_readiness = await build_video_export_readiness(video_id, db)

    return templates.TemplateResponse(
        request=request,
        name="video_mistakes.html",
        context={
            "ui_prefix": _ui_prefix(request),
            "video": video,
            "mistakes": mistakes,
            "candidate_counts": candidate_counts,
            "final_counts": final_counts,
            "search_query_counts": search_query_counts,
            "export_readiness": export_readiness,
        },
    )


@router.get("/jobs", response_class=HTMLResponse)
async def list_admin_jobs(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Job).order_by(Job.created_at.desc()).limit(100))
    jobs = result.scalars().all()
    return templates.TemplateResponse(
        request=request,
        name="jobs.html",
        context={"ui_prefix": _ui_prefix(request), "jobs": jobs},
    )


@router.get("/videos/{video_id}/mistakes/{mistake_id}", response_class=HTMLResponse)
async def review_candidates_nested(
    video_id: int,
    mistake_id: int,
    request: Request,
    view: str = Query("all", pattern="^(all|consensus|disputed|selected)$"),
    side: str | None = Query(None, pattern="^(wrong|right)$"),
    status_filter: str | None = Query(None, alias="status"),
    rights_status: str | None = None,
    source_provider: str | None = None,
    sort: str = Query("id", pattern="^-?(id|review_score|resolution|created_at)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None)))
    mistake = result.scalars().first()
    if not mistake or mistake.video_id != video_id:
        raise HTTPException(status_code=404, detail="Mistake not found for video")
    return await review_candidates(
        mistake_id=mistake_id,
        request=request,
        view=view,
        side=side,
        status_filter=status_filter,
        rights_status=rights_status,
        source_provider=source_provider,
        sort=sort,
        limit=limit,
        offset=offset,
        db=db,
    )


def _candidate_order_by(sort: str):
    resolution = func.coalesce(ImageCandidate.original_width, 0) * func.coalesce(ImageCandidate.original_height, 0)
    sort_options = {
        "id": [ImageCandidate.id.asc()],
        "-id": [ImageCandidate.id.desc()],
        "review_score": [ImageCandidate.review_score.asc().nullslast(), ImageCandidate.id.asc()],
        "-review_score": [ImageCandidate.review_score.desc().nullslast(), ImageCandidate.id.asc()],
        "resolution": [resolution.asc(), ImageCandidate.id.asc()],
        "-resolution": [resolution.desc(), ImageCandidate.id.asc()],
        "created_at": [ImageCandidate.created_at.asc(), ImageCandidate.id.asc()],
        "-created_at": [ImageCandidate.created_at.desc(), ImageCandidate.id.asc()],
    }
    return sort_options[sort]


def _candidate_view_condition(view: str):
    if view == "selected":
        selected_candidates = (
            select(FinalAsset.candidate_id)
            .where(FinalAsset.candidate_id.is_not(None))
            .where(FinalAsset.status.in_(["approved", "exported"]))
        )
        return ImageCandidate.id.in_(selected_candidates)

    pass_count = func.count(CandidateReview.id).filter(CandidateReview.score >= PASS_THRESHOLD)
    if view == "consensus":
        consensus_candidates = (
            select(CandidateReview.candidate_id)
            .group_by(CandidateReview.candidate_id)
            .having(pass_count >= REQUIRED_PASS_COUNT)
        )
        return ImageCandidate.id.in_(consensus_candidates)

    if view == "disputed":
        review_count = func.count(CandidateReview.id)
        pass_verdict_count = func.count(CandidateReview.id).filter(CandidateReview.verdict == "pass")
        non_pass_verdict_count = func.count(CandidateReview.id).filter(CandidateReview.verdict.in_(["maybe", "fail"]))
        score_spread = func.max(CandidateReview.score) - func.min(CandidateReview.score)
        disputed_candidates = (
            select(CandidateReview.candidate_id)
            .group_by(CandidateReview.candidate_id)
            .having(
                and_(
                    review_count >= 2,
                    or_(
                        and_(pass_verdict_count >= 1, non_pass_verdict_count >= 1),
                        score_spread >= 0.35,
                    ),
                )
            )
        )
        return ImageCandidate.id.in_(disputed_candidates)

    return None


def _page_url(request: Request, **updates) -> str:
    params = dict(request.query_params)
    for key, value in updates.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    query = urlencode(params)
    return f"{request.url.path}?{query}" if query else request.url.path


@router.get("/mistakes/{mistake_id}/candidates", response_class=HTMLResponse)
async def review_candidates(
    mistake_id: int,
    request: Request,
    view: str = Query("all", pattern="^(all|consensus|disputed|selected)$"),
    side: str | None = Query(None, pattern="^(wrong|right)$"),
    status_filter: str | None = Query(None, alias="status"),
    rights_status: str | None = None,
    source_provider: str | None = None,
    sort: str = Query("id", pattern="^-?(id|review_score|resolution|created_at)$"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Mistake).where(Mistake.id == mistake_id, Mistake.deleted_at.is_(None)))
    mistake = result.scalars().first()
    if not mistake:
        raise HTTPException(status_code=404, detail="Mistake not found")

    candidate_filters = [ImageCandidate.mistake_id == mistake_id]
    if side:
        candidate_filters.append(ImageCandidate.side == side)
    if status_filter:
        candidate_filters.append(ImageCandidate.status.in_(candidate_status_filter_values(status_filter)))
    if rights_status:
        candidate_filters.append(ImageCandidate.rights_status == rights_status)
    if source_provider:
        candidate_filters.append(ImageCandidate.source_provider == source_provider)
    view_condition = _candidate_view_condition(view)
    if view_condition is not None:
        candidate_filters.append(view_condition)

    total_candidates = await db.scalar(select(func.count()).select_from(ImageCandidate).where(*candidate_filters)) or 0
    result = await db.execute(
        select(ImageCandidate)
        .where(*candidate_filters)
        .order_by(*_candidate_order_by(sort))
        .offset(offset)
        .limit(limit)
    )
    candidates = result.scalars().all()

    sq_result = await db.execute(
        select(SearchQuery)
        .where(SearchQuery.mistake_id == mistake_id)
        .order_by(SearchQuery.id.desc())
    )
    sq_all = sq_result.scalars().all()
    search_query_by_id = {sq.id: sq for sq in sq_all}
    last_query: dict[str, str] = {}
    for sq in sq_all:
        if sq.side not in last_query:
            last_query[sq.side] = sq.query_text

    candidate_ids = [c.id for c in candidates]
    reviews_by_candidate: dict[int, list[CandidateReview]] = {cid: [] for cid in candidate_ids}
    reference_briefs_by_candidate: dict[int, ReferenceBrief] = {}
    aggregates_by_candidate = {}
    if candidate_ids:
        review_result = await db.execute(
            select(CandidateReview)
            .where(CandidateReview.candidate_id.in_(candidate_ids))
            .order_by(CandidateReview.candidate_id, CandidateReview.reviewer_name)
        )
        for review in review_result.scalars().all():
            reviews_by_candidate.setdefault(review.candidate_id, []).append(review)

        brief_result = await db.execute(
            select(ReferenceBrief)
            .where(ReferenceBrief.candidate_id.in_(candidate_ids))
            .order_by(ReferenceBrief.candidate_id)
        )
        for brief in brief_result.scalars().all():
            reference_briefs_by_candidate[brief.candidate_id] = brief

    for c in candidates:
        aggregates_by_candidate[c.id] = build_review_aggregate(c.id, reviews_by_candidate.get(c.id, []))

    result = await db.execute(
        select(FinalAsset)
        .where(FinalAsset.mistake_id == mistake_id)
        .where(FinalAsset.status.in_(["approved", "exported"]))
        .order_by(FinalAsset.side, FinalAsset.id)
    )
    final_assets = result.scalars().all()
    final_asset_by_side = {asset.side: asset for asset in final_assets}
    final_asset_health_by_id = {asset.id: final_asset_health(asset) for asset in final_assets}
    selected_candidate_ids = {asset.candidate_id for asset in final_assets if asset.candidate_id}

    expected_review_count = len(EXPECTED_REVIEWERS)
    candidate_review_state = {}
    for c in candidates:
        reviews = reviews_by_candidate.get(c.id, [])
        aggregate = aggregates_by_candidate[c.id]
        verdicts = {r.verdict for r in reviews}
        scores = [float(r.score) for r in reviews]
        has_pass = "pass" in verdicts
        has_non_pass = bool(verdicts & {"maybe", "fail"})
        score_spread = max(scores) - min(scores) if scores else None

        candidate_review_state[c.id] = {
            "is_consensus": aggregate["approved_by_consensus"],
            "is_disputed": len(reviews) >= 2 and ((has_pass and has_non_pass) or (score_spread or 0) >= 0.35),
            "is_selected": c.id in selected_candidate_ids,
            "score_spread": score_spread,
        }

    def visible(candidate: ImageCandidate) -> bool:
        state = candidate_review_state[candidate.id]
        if view == "consensus":
            return state["is_consensus"]
        if view == "disputed":
            return state["is_disputed"]
        if view == "selected":
            return state["is_selected"]
        return True

    visible_candidates = [c for c in candidates if visible(c)]
    wrong = [c for c in visible_candidates if c.side == "wrong"]
    right = [c for c in visible_candidates if c.side == "right"]

    return templates.TemplateResponse(
        request=request,
        name="review_candidates.html",
        context={
            "ui_prefix": _ui_prefix(request),
            "mistake": mistake,
            "candidates": candidates,
            "total_candidates": total_candidates,
            "wrong": wrong,
            "right": right,
            "candidate_filters": {
                "view": view,
                "side": side or "",
                "status": status_filter or "",
                "rights_status": rights_status or "",
                "source_provider": source_provider or "",
                "sort": sort,
                "limit": limit,
                "offset": offset,
            },
            "pagination": {
                "limit": limit,
                "offset": offset,
                "total": total_candidates,
                "start": min(offset + 1, total_candidates) if total_candidates else 0,
                "end": min(offset + limit, total_candidates),
                "prev_url": _page_url(request, offset=max(offset - limit, 0), limit=limit) if offset > 0 else None,
                "next_url": _page_url(request, offset=offset + limit, limit=limit) if offset + limit < total_candidates else None,
            },
            "wrong_query": last_query.get("wrong", ""),
            "right_query": last_query.get("right", ""),
            "search_queries": sq_all,
            "search_query_by_id": search_query_by_id,
            "reviews_by_candidate": reviews_by_candidate,
            "reference_briefs_by_candidate": reference_briefs_by_candidate,
            "aggregates_by_candidate": aggregates_by_candidate,
            "final_asset_by_side": final_asset_by_side,
            "final_asset_health_by_id": final_asset_health_by_id,
            "selected_candidate_ids": selected_candidate_ids,
            "view": view,
            "expected_review_count": expected_review_count,
            "candidate_review_state": candidate_review_state,
            "reviewer_cli_status": get_reviewer_cli_readiness(),
        },
    )
