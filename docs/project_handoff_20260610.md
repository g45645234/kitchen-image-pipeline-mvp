# Project Handoff - 2026-06-10

This document is for transferring `kitchen-image-pipeline-mvp` to another developer. It intentionally does not include secrets, API keys, private SSH keys, admin tokens, or paid-service credentials. Share those out of band.

## Repository State

- Repository: `https://github.com/g45645234/kitchen-image-pipeline-mvp`
- Active branch: `codex/multi-reviewers`
- Latest handoff commit before this document: `706e837 Add side feedback notes for candidate review`
- Current live DB migration head: `20260610_1200`
- Main app path on the current server: `/home/claudecode/kitchen-image-pipeline-mvp`
- Working tree note: the server has unrelated untracked local files: `image.jpg`, `render_139_2026-05-03_v1.png`, `review_session.py`. Do not commit or delete them unless the owner explicitly confirms.

## What The Project Does

The MVP manages image selection for kitchen-design mistake videos:

1. Create/import a video and mistake cards.
2. Generate or manually add search queries for each mistake side (`wrong` / `right`).
3. Run image search through configured providers, currently including `yandex_search_api`.
4. Download candidate originals to local storage.
5. Run AI review through three host-side CLI reviewers: `codex`, `antigravity`, `claude_cli`.
6. Let the operator manually approve/reject/reference candidates.
7. Confirm rights before selecting final assets.
8. Export final images and metadata.

AI reviewers only score candidate suitability. They must not confirm rights or select final assets.

## Current Deployment

Docker Compose services on the current server:

- `kitchen_db`: Postgres 16, published as host port `5433`.
- `kitchen_web`: FastAPI/uvicorn, published as host port `8000`, runs with reload and bind-mounted source.
- `kitchen_worker`: background worker for non-reviewer jobs.

Useful commands:

```bash
cd /home/claudecode/kitchen-image-pipeline-mvp

docker compose ps
docker compose logs -f web
docker compose logs -f worker
docker exec kitchen_db psql -U app -d kitchen_assets
```

Apply migrations:

```bash
docker exec kitchen_web alembic upgrade head
```

Run tests correctly from Docker:

```bash
docker exec -e PYTHONPATH=/src kitchen_web pytest -q
```

Important: running `docker exec kitchen_web pytest -q` without `PYTHONPATH=/src` may import an installed `app` package from `site-packages` and fail during collection.

Last verified test result:

```text
82 passed, 165 skipped
```

## Host Reviewer Bridge

Real AI reviewer jobs are processed outside Docker by the host bridge so the host-authenticated CLIs can inspect local image files.

Current tmux session:

```bash
tmux attach -t kitchen-reviewer
```

Heartbeat file:

```bash
/home/claudecode/kitchen-image-pipeline-mvp/storage/host_reviewer_bridge_status.json
```

At handoff time the bridge heartbeat was fresh, state `idle`, and all three reviewers were configured:

- `codex`
- `antigravity`
- `claude_cli`

Do not enqueue or run real reviewer jobs without explicit owner approval. They can spend external AI quota.

Related documentation:

- `docs/reviewer_cli_worker.md`
- `docs/mvp_release_runbook_20260607.md`

## Important Recent Changes

Recent commits on `codex/multi-reviewers` include:

- `706e837` - added side-specific search/AI-review feedback notes.
- `d9da8d8` - candidate originals are served as browser-safe inline images; WebP/non-inline formats are converted to JPEG for browser viewing.
- `3b1b6ef` - manual review hides placeholder candidates without usable previews.
- `0b2f204` - failed downloads are hidden from review queues.
- `e4421fb` - thumbnail-only Yandex candidates are marked unavailable.

### Side Feedback Notes

The candidates page now has a `Комментарий к поиску/AI-review` block for both sides. It saves notes per `mistake_id + side`.

API:

```http
PUT /api/mistakes/{mistake_id}/side-feedback/{side}
```

DB table:

```text
mistake_side_feedback
```

Migration:

```text
alembic/versions/20260610_1200_add_mistake_side_feedback.py
```

This is meant to close the feedback loop when the human reviewer sees a whole result set is wrong and can explain why. The next developer should use these notes when generating better search queries and when tightening the AI-review rubric.

## Current Live Data Status

Current active video being worked on: video `14`.

Final assets selected for video `14` at handoff:

| Mistake ID | Title | Final sides selected |
|---:|---|---|
| 14 | Неправильная вытяжка между шкафами | `wrong`, `right` |
| 15 | Отказ от измельчителя пищевых отходов | `wrong`, `right` |
| 16 | Полное отсутствие вытяжки | `right` only |
| 17 | Плита на 4 конфорки в маленькой кухне | `wrong`, `right` |
| 18 | Отказ от посудомойки ради экономии места | `wrong`, `right` |

Remaining missing final asset:

```text
mistake_id=16, side=wrong
```

Current saved human feedback for `16/wrong`:

```text
Текущие картинки из выдачи не подходят. Нужен более точный поиск и AI-review по этой стороне.
```

The previous consensus candidates for `16/wrong`, including `1292` and `1331`, were rejected by the operator as irrelevant.

## Immediate Next Work

1. Open the `16/wrong` candidate page in the current deployment.
2. Read the saved feedback note in the new feedback block.
3. Ask the owner for a more specific human explanation of why the rejected images fail, for example:
   - product-card/catalog image instead of real kitchen;
   - close-up of appliance without surrounding cabinets/walls;
   - image contains a hood or ventilation despite being a `wrong` example;
   - no visible stove/cooktop;
   - render/illustration instead of photo;
   - does not show the consequence or visual problem clearly.
4. Convert that feedback into better Russian Yandex queries and negative filters.
5. Search/download a smaller, more targeted candidate pool.
6. Run AI review only after candidates have local originals.
7. Stop pending reviewer jobs once enough good `2 of 3` consensus candidates exist, to save AI quota.
8. Have the operator manually inspect full local images and choose final only after rights are confirmed.

Candidate page shape:

```text
/ui/videos/14/mistakes/16?view=consensus&side=wrong&source_provider=yandex_search_api&sort=-review_score
```

## Known Product/Technical Follow-ups

Highest priority:

- Tighten the AI-review prompt/rubric. This was intentionally deferred. See `docs/reviewer_cli_worker.md`, section `Deferred Review Prompt Work`.
- Make search generation consume `mistake_side_feedback` so bad result-set patterns directly improve the next query batch.
- Consider adding structured feedback fields later, but keep free-text first because the operator is still discovering the taxonomy.

Operational/QA:

- Real browser click-through remains manual; no Playwright/Selenium runtime is installed on the server.
- External provider rate-limit behavior has not been stress-tested.
- Production hardening still needs owner decisions: admin token, SSH policy, reviewer bridge under systemd vs tmux, backup/restore, and secret handling.

Post-MVP by current project spec:

- Perceptual similarity/dedup beyond URL hash.
- Real LLM generation for reference briefs.
- S3/Celery/embedding pipeline.
- Full NLE import validation.

## Credentials And Access The New Developer Needs

Ask the owner for these separately:

- GitHub repository access.
- SSH access to the current server.
- Current `.env` values, especially admin token and provider credentials.
- Yandex Search API / relay credentials if continuing real image search.
- Authenticated CLI access for `codex`, `agy`, and `claude_cli` if running real AI reviews.
- Any paid quota limits or approval rules before invoking external CLIs.

Do not commit `.env`, private keys, tokens, downloaded proprietary assets, or local temporary files.

## Useful DB Checks

Missing final assets for a video:

```sql
select m.id as missing_mistake_id, s.side
from mistakes m
cross join (values ('wrong'),('right')) s(side)
left join final_assets fa on fa.mistake_id=m.id and fa.side=s.side
where m.video_id=14 and fa.id is null
order by m.id, s.side;
```

Final assets for video 14:

```sql
select m.id, m.order_index, m.title, fa.side, fa.candidate_id
from mistakes m
left join final_assets fa on fa.mistake_id=m.id and fa.status in ('approved','exported')
where m.video_id=14
order by m.order_index, fa.side;
```

Reviewer job status:

```sql
select status, type, count(*)
from jobs
group by status, type
order by status, type;
```

Side feedback notes:

```sql
select mistake_id, side, feedback_text
from mistake_side_feedback
order by mistake_id, side;
```

## Handoff Rule Of Thumb

The next developer should avoid broad rewrites. The project is working as an operator-driven MVP. The safest next improvements are narrow feedback-loop changes: better query generation, better reviewer prompt criteria, and clearer UI state after manual decisions.
