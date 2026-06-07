# MVP Release Runbook - 2026-06-07

This runbook is the operator checklist for promoting `kitchen-image-pipeline-mvp` from code-complete MVP to a stable deployed MVP. It assumes the current code state and Alembic head `20260607_0910`.

## 1. Preflight State

Run from `/home/claudecode/kitchen-image-pipeline-mvp`:

```bash
docker compose ps
curl -fsS http://127.0.0.1:8000/health
docker compose exec -T web alembic current
```

Expected:

- `kitchen_db`, `kitchen_web`, `kitchen_worker` are up.
- `/health` returns `{"status":"ok"}`.
- Alembic current is `20260607_0910 (head)`.

## 2. Required Test Gates

```bash
docker compose exec -T -e PYTHONPATH=/src -e TEST_DATABASE_URL=postgresql+psycopg://app:app@db:5432/kitchen_assets_test web pytest tests/unit -q
docker compose exec -T -e PYTHONPATH=/src -e TEST_DATABASE_URL=postgresql+psycopg://app:app@db:5432/kitchen_assets_test web pytest tests/integration -q
ADMIN_UI_BASE_URL=http://127.0.0.1:8000 PROJECT_ROOT=/home/claudecode/kitchen-image-pipeline-mvp node scripts/smoke_admin_inline_js.mjs
```

Current latest known results:

- Unit: `80 passed, 1 skipped`.
- Integration: `150 passed`.
- Admin inline JS smoke: expected `admin inline JS smoke passed`.

## 3. Production Auth Hardening

For an internet-reachable server, do not run without admin protection.

Set in `.env` or deployment environment:

```bash
APP_ENV=production
ADMIN_API_TOKEN=<long-random-token>
```

Operator access options:

- API clients send `X-Admin-Token: <token>`.
- Browser UI sets an `admin_api_token=<token>` cookie, or the reverse proxy protects `/ui`, `/admin`, `/api`, `/docs`, `/storage`, and `/jobs` paths.

Expected behavior:

- Missing token returns `401` for protected API/UI routes.
- If `APP_ENV!=local` and token is missing, protected routes fail closed with `503`.

## 4. Worker Job Types

Docker worker should not claim real AI reviewer jobs. Confirm:

```bash
docker compose exec -T worker env | rg '^WORKER_JOB_TYPES='
```

Expected value includes:

```text
extract_mistakes,create_search_queries,search_all_queries,run_search,score_candidates,review_candidate,create_reference_brief,download_candidate,process_final_asset,export_video,export_final_assets,cleanup_storage
```

Expected value does not include:

```text
run_candidate_reviewer
```

## 5. Host Reviewer Bridge

Run preflight without spending AI quota:

```bash
cd /home/claudecode/kitchen-image-pipeline-mvp
export DATABASE_URL=postgresql://app:app@127.0.0.1:5433/kitchen_assets
export API_BASE_URL=http://127.0.0.1:8000
export ADMIN_API_TOKEN=<same-token-if-enabled>
export HOST_STORAGE_ROOT=/home/claudecode/kitchen-image-pipeline-mvp/storage
export HOST_REVIEWER_ALLOW_ROOT=false
export CODEX_CLI_COMMAND="python3 -m app.commands.reviewer_cli_adapter --reviewer codex --backend-command 'codex exec --sandbox read-only'"
export ANTIGRAVITY_CLI_COMMAND="python3 -m app.commands.reviewer_cli_adapter --reviewer antigravity --backend-command '/root/.local/bin/agy --print' --prompt-as-arg"
export CLAUDE_CLI_COMMAND="python3 -m app.commands.reviewer_cli_adapter --reviewer claude_cli --backend-command 'runuser -u claudecode -- claude --print --output-format json --no-session-persistence' --prompt-as-arg"
PYTHONPATH=/home/claudecode/kitchen-image-pipeline-mvp python3 -m app.commands.host_reviewer_bridge --check
```

Expected: JSON `ok=true` for database, API, status path, and reviewer commands.

For production, prefer systemd using `deploy/systemd/kitchen-host-reviewer-bridge.service.example` and `deploy/systemd/env.host-reviewer.example`. Use tmux only for supervised manual/staging runs.

## 6. Real Reviewer Smoke Gate

Only run after explicit approval to spend CLI quota.

1. Pick a safe candidate id.
2. Ensure the candidate has a downloaded original before AI review:

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/candidates/<candidate_id>/download \
  -H "X-Admin-Token: $ADMIN_API_TOKEN"
```

Wait until the `download_candidate` job is `completed`, then confirm the candidate has `storage_key_original` or inspect:

```bash
curl -fsS http://127.0.0.1:8000/api/candidates/<candidate_id>/review-payload \
  -H "X-Admin-Token: $ADMIN_API_TOKEN"
```

Expected: payload includes `storage_key_original`, `image_file_path`, `image_file_available=true`, and `review_image_source=local_file`.

3. Create forced reviewer jobs:

```bash
curl -fsS -X POST http://127.0.0.1:8000/api/candidates/<candidate_id>/reviews/run \
  -H 'Content-Type: application/json' \
  -H "X-Admin-Token: $ADMIN_API_TOKEN" \
  --data '{"reviewers":["codex","antigravity","claude_cli"],"prompt_version":"release-smoke-YYYYMMDD","force":true}'
```

4. Process exactly three jobs with the host bridge or let the daemon process them. The host bridge must have `HOST_STORAGE_ROOT` set to the host-side storage directory.
5. Verify:

```bash
curl -fsS http://127.0.0.1:8000/api/candidates/<candidate_id>/reviews/aggregate   -H "X-Admin-Token: $ADMIN_API_TOKEN"
```

Expected:

- `review_count=3`.
- All three reviewer names present.
- `candidate_reviews.response_time_ms` is non-null for each new review.

Latest known real smoke for `candidate_id=357` passed with all three reviewers and persisted latencies.

Latest known local-image Antigravity batch for video mistake `14`: 50 `yandex_search_api` candidates existed, 45 downloaded successfully, and 45/45 downloaded candidates were reviewed by `antigravity` with local image files. Verdict summary: `pass=20`, `maybe=2`, `fail=23`.

## 7. Operator UI Click-Through

Until Playwright/Selenium is installed, this remains a manual browser gate.

In a real browser:

1. Open `/ui` or `/admin`.
2. Create a video.
3. Create a mistake.
4. Generate SearchQuery rows and run search with mock/local provider.
5. Open candidate review page and verify filters/pagination/actions render.
6. Upload wrong/right own final assets.
7. Wait for processing jobs.
8. Confirm export readiness shows `can_export=true`.
9. Export the video.
10. Download or inspect `manifest.json`, `assets.csv`, and image files.
11. Run a cleanup dry-run and inspect report.

Record the temporary video id, job ids, export path, and any UI defects.

## 8. Export Artifact Gate

For at least one complete temporary video, verify:

- `manifest.json` parses as JSON and has `schema_version=1.0`.
- `assets.csv` parses and has one row per exported asset.
- Every manifest asset file exists under the export directory.
- Every exported JPEG is exactly `1920x1080`.
- A downstream parser or NLE import can consume the package, if such tooling is available.

Latest known app-side E2E export package smoke passed at:

```text
exports/codex-e2e-export-6643bc43_20260607_081752/
```

## 9. External Provider Gate

If real search providers are enabled, test one controlled provider failure path before production:

- Missing credentials.
- HTTP/rate-limit response.
- Timeout or invalid response.

Expected:

- Worker does not crash.
- SearchQuery status becomes `failed` or job result becomes `partially_failed` as appropriate.
- Error message is operator-readable.

If only mock/local provider is enabled, this is not a release blocker.

## 10. Backup and Cleanup Policy

Before production use:

- Confirm PostgreSQL backup/restore command or provider snapshot policy.
- Confirm `storage/` and `exports/` retention policy.
- Use cleanup dry-run before delete mode:

```bash
python -m app.commands.cleanup_storage --dry-run
python -m app.commands.cleanup_storage --delete
```

Do not run delete cleanup on production without reviewing the dry-run report.

## 11. Release Decision

Code-level MVP can be considered complete when:

- Required test gates pass.
- Runtime preflight passes.
- Auth is configured for reachable deployment.
- Host reviewer bridge preflight passes, or reviewer bridge is explicitly deferred.
- Manual browser click-through has no blocking defects.
- Export artifact gate passes for a fresh package.

Known non-blockers/post-MVP:

- Real browser automation is not installed.
- External provider stress testing is required only when real providers are enabled.
- Full S3/Celery/embedding/similarity/clean generation remain post-MVP by spec.
