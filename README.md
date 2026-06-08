# Kitchen Image Pipeline MVP

Lightweight MVP для ручного подбора, ревью и экспорта иллюстраций к видео про ошибки в дизайне кухни.

Главный сценарий MVP: оператор вручную создает видео и карточки ошибок, генерирует или добавляет поисковые запросы, запускает configured image search, скачивает подходящих кандидатов, запускает AI review по локальным файлам, вручную выбирает usable final assets с подтвержденными правами, при необходимости загружает собственные final assets, затем экспортирует manifest/CSV/processed images.

## Stack

- Backend: FastAPI, SQLAlchemy async, Alembic.
- Database: PostgreSQL.
- Worker: simple DB job runner in the `worker` container.
- Storage: local `./storage` and `./exports` folders.
- Admin UI: `/admin` and `/ui` use the same screens.

## Requirements

- Docker and Docker Compose.
- Optional host CLIs for real AI reviewer jobs: `codex`, `agy`, `claude`. They are not required for the manual MVP workflow.

## Quick Start

1. Create local env file:

```bash
cp .env.example .env
```

2. Start the app, worker, and PostgreSQL:

```bash
docker compose up -d
```

3. Apply migrations:

```bash
docker compose exec web alembic upgrade head
```

4. Open:

- Admin UI: http://localhost:8000/admin
- API docs: http://localhost:8000/docs
- Health: http://localhost:8000/health

In `APP_ENV=local` with empty `ADMIN_API_TOKEN`, the admin API/UI are open for local development. On a reachable server, set `ADMIN_API_TOKEN` and use `X-Admin-Token` or an `admin_api_token` cookie/reverse-proxy auth.

`MISTAKE_EXTRACTION_PROVIDER=mock` is the default and does not spend external LLM quota. Set it to `anthropic` only when transcript extraction through Anthropic is explicitly intended and `ANTHROPIC_API_KEY` is configured.

## Main Workflow

1. Create a video project in `/admin/videos` or with `POST /api/videos`.
2. Open the video mistakes page and create wrong/right mistake cards manually. This is the primary MVP flow and does not require LLM extraction.
3. For each mistake, open the candidates page.
4. Generate SearchQuery rows with `POST /api/mistakes/{mistake_id}/generate-search-queries`, or add/edit/delete SearchQuery rows manually in the UI.
5. Run candidate search with `POST /api/mistakes/{mistake_id}/search`. The worker creates candidates from existing SearchQuery rows through the selected provider, for example `mock_search`, `yandex_search_api`, or another configured provider. The legacy `/api/mistakes/{mistake_id}/candidates/search` endpoint still exists for compatibility.
6. Download candidates that should be reviewed with `POST /api/candidates/{candidate_id}/download`. Real AI reviewer jobs require `storage_key_original`; `/api/candidates/{candidate_id}/reviews/run` rejects URL-only candidates. The stored original may remain WebP or another downloaded raster format, but `GET /api/candidates/{candidate_id}/original` returns a browser-safe inline image for manual review, converting non-inline formats such as WebP to JPEG on the fly.
7. Run AI review with `POST /api/candidates/{candidate_id}/reviews/run`. The Docker worker must not claim `run_candidate_reviewer`; those jobs are processed by the host reviewer bridge so host-authenticated CLIs can inspect the downloaded image file through `image_file_path`.
8. Review candidate cards with filters, sorting, and pagination. Candidate cards show source, dimensions, status, rights, storage status, scores, reviewer summaries, and aggregate consensus.
9. Use candidates as reference-only when they are useful as ideas but not suitable as final assets.
10. Confirm rights with an operator comment before final selection when required. Candidates with `may_use_directly=false` cannot be selected as final.
11. Select a candidate as final or upload your own wrong/right final asset. Uploads store the immutable original immediately and enqueue `process_final_asset`; the worker then creates thumbnail, processed 1920x1080 JPEG, and metadata sidecar.
12. Check export readiness on the video mistakes page, then run export with `POST /api/videos/{video_id}/export`.
13. Download latest export with `GET /api/videos/{video_id}/manifest` and `GET /api/videos/{video_id}/assets-csv`.
14. Use storage cleanup dry-run/delete from the Jobs page or API when old storage keys become orphaned.

## Important API Endpoints

- `POST /api/videos`
- `GET /api/videos?limit=100&offset=0&status=draft`
- `PATCH /api/videos/{video_id}`
- `DELETE /api/videos/{video_id}`
- `POST /api/videos/{video_id}/mistakes`
- `POST /api/videos/{video_id}/extract-mistakes`
- `GET /api/videos/{video_id}/mistakes?limit=100&offset=0`
- `PATCH /api/mistakes/{mistake_id}`
- `DELETE /api/mistakes/{mistake_id}`
- `POST /api/mistakes/{mistake_id}/generate-search-queries`
- `GET /api/mistakes/{mistake_id}/search-queries?side=wrong&limit=100&offset=0`
- `POST /api/mistakes/{mistake_id}/search-queries`
- `PATCH /api/mistakes/{mistake_id}/search-queries/{query_id}`
- `DELETE /api/mistakes/{mistake_id}/search-queries/{query_id}`
- `POST /api/mistakes/{mistake_id}/search`
- `GET /api/mistakes/{mistake_id}/candidates?side=wrong&status=review&limit=50&offset=0&sort=-review_score`
- `POST /api/candidates/{candidate_id}/use-as-reference`
- `POST /api/candidates/{candidate_id}/reference-brief`
- `GET /api/candidates/{candidate_id}/reference-brief`
- `PATCH /api/candidates/{candidate_id}/reference-brief`
- `POST /api/candidates/{candidate_id}/confirm-rights`
- `POST /api/candidates/{candidate_id}/download`
- `GET /api/candidates/{candidate_id}/review-payload`
- `POST /api/candidates/{candidate_id}/reviews/run`
- `GET /api/candidates/{candidate_id}/reviews/aggregate`
- `POST /api/candidates/{candidate_id}/select-final`
- `POST /api/mistakes/{mistake_id}/upload-final-asset`
- `GET /api/videos/{video_id}/export-readiness`
- `POST /api/videos/{video_id}/export`
- `GET /api/videos/{video_id}/final-assets?limit=100&offset=0`
- `POST /api/final-assets/{asset_id}/process`
- `DELETE /api/final-assets/{asset_id}`
- `POST /api/storage/cleanup-dry-run`
- `POST /api/storage/cleanup`

## Background Jobs

The Docker worker handles only non-CLI jobs:

```text
extract_mistakes,create_search_queries,search_all_queries,run_search,score_candidates,review_candidate,create_reference_brief,download_candidate,process_final_asset,export_video,export_final_assets,cleanup_storage
```

Real reviewer jobs (`run_candidate_reviewer`) must run on the host bridge, not in Docker, because reviewer CLIs are authenticated on the host and need access to downloaded local image files. Set `HOST_STORAGE_ROOT` to the host-side storage directory, for example `/home/claudecode/kitchen-image-pipeline-mvp/storage`, so payload paths from Docker storage can be rewritten for host CLIs. See `docs/reviewer_cli_worker.md` for the no-credit preflight check, tmux/systemd handoff, and real CLI smoke notes.

## Testing

Run the main suites with the test database URL:

```bash
docker compose exec -T -e PYTHONPATH=/src -e TEST_DATABASE_URL=postgresql+psycopg://app:app@db:5432/kitchen_assets_test web pytest tests/unit -q
docker compose exec -T -e PYTHONPATH=/src -e TEST_DATABASE_URL=postgresql+psycopg://app:app@db:5432/kitchen_assets_test web pytest tests/integration -q
```

Run the browserless admin JavaScript smoke against the live local app:

```bash
ADMIN_UI_BASE_URL=http://127.0.0.1:8000 PROJECT_ROOT=/home/claudecode/kitchen-image-pipeline-mvp node scripts/smoke_admin_inline_js.mjs
```

## Project Structure

```text
alembic/              database migrations
app/models/           SQLAlchemy models
app/schemas/          Pydantic schemas
app/routers/          API and admin UI routes
app/services/         search, storage, rights, export, job, reviewer services
app/templates/        admin UI templates
app/commands/         cleanup and host reviewer bridge CLIs
docs/                 operational notes and smoke-test docs
fixtures/             mock search data
storage/              local originals, derivatives, metadata
exports/              generated export packages
tests/                unit and integration tests
```
