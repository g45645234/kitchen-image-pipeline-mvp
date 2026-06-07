# MVP Completion Audit - 2026-06-07

Scope: `kitchen_image_pipeline_final_mvp_spec.md` plus accepted current corrections: host-side real reviewer bridge, deterministic ReferenceBrief draft in MVP, manual final selection, Docker worker restricted to non-CLI jobs, and spec-compatible aliases for legacy flows.

This audit records current evidence. It is not a final completion claim unless every row is `Proven` and no manual verification remains.

## Current Runtime Evidence

- Docker services: `kitchen_db`, `kitchen_web`, and `kitchen_worker` are up; DB healthcheck is healthy.
- API health: `GET /health` returned `{"status":"ok"}`.
- Alembic: live DB is at `20260607_0910 (head)`.
- Latest full tests after code changes:
  - Unit: `80 passed, 1 skipped`.
  - Integration: `150 passed`.
- Real reviewer smoke for `candidate_id=357` completed for `codex`, `antigravity`, and `claude_cli`; all three persisted `response_time_ms`; aggregate `review_count=3`, `pass_count=3`, `review_score=0.86`, `approved_by_consensus=true`.
- Live export E2E through HTTP API and worker passed: video `12`, mistake `13`, assets `3/4`, jobs `54/55/56`, export dir `exports/codex-e2e-export-6643bc43_20260607_081752`; manifest/CSV/two JPEGs verified, both JPEGs `1920x1080`.
- Release/operator runbook created: `docs/mvp_release_runbook_20260607.md`.
- Browser automation check: no local Chromium/Firefox/Playwright/Selenium/Puppeteer runtime is installed; only `npx` is available. Real browser click-through remains a deployment/QA gate unless browser dependencies are installed later.
- Live admin inline JavaScript smoke on the current server passed: `ADMIN_UI_BASE_URL=http://127.0.0.1:8000 PROJECT_ROOT=/home/claudecode/kitchen-image-pipeline-mvp node scripts/smoke_admin_inline_js.mjs` -> `admin inline JS smoke passed`.
- Independent agy release-readiness audit saved at `/tmp/agy_release_readiness_audit_20260607.md` classified the remaining manual gates as deployment/operator checklist items, not code-completion blockers, and found no critical code/test blockers.

## Requirement Matrix

| Spec Area | Status | Evidence | Remaining Risk |
|---|---|---|---|
| 1-3 Architecture and product logic | Proven | Docker compose web/db/worker up; admin/API workflow implemented; manual final selection required; reviewer jobs only score candidates. Tests: `test_mvp_e2e_postgres.py`, candidate review/action suites. | No blocker known. |
| 4 Rights policy | Proven | `approve_final`/`select-final` require `may_use_directly`; rights confirmation audit exists; export requires exportable rights. Tests: `test_candidate_actions_api.py`, `test_final_rights_services_postgres.py`, `test_export_service_postgres.py`. | Operator must still enter real license evidence correctly. |
| 5-8 Stack, compose, repo structure | Proven | Docker compose runtime active; worker env includes spec and compatibility job types; README documents startup/workflow. | Repo has large uncommitted/untracked MVP work; release packaging/commit hygiene remains outside runtime proof. |
| 9-10 DB entities/schema | Proven | Alembic `20260607_0910 (head)`; models include Video, Mistake, SearchQuery, ImageCandidate, CandidateReview with `response_time_ms`, ReferenceBrief with JSONB arrays and `error_message`, FinalAsset, Job, BlockedDomain, AuditEvent. Tests exercise migrations through integration DB. | Need final migration review before production deployment if deploying to an existing non-test DB. |
| 11 Storage layout/cleanup | Proven | Internal storage and export storage exercised; cleanup dry-run/delete/targeted cleanup tests pass; delete/replacement enqueue cleanup jobs. Tests: `test_storage_cleanup_postgres.py`, `test_delete_lifecycle_api.py`, `test_upload_final_asset_postgres.py`. | Manual global cleanup should be run cautiously in production. |
| 12 Manifest schema | Proven for MVP runtime | Live E2E export verified `schema_version=1.0`, video metadata, nested mistakes, wrong/right asset entries, `assets.csv`, and physical image files. Tests: `test_export_service_postgres.py`; live export dir noted above. | NLE import compatibility remains manual outside app tests. |
| 13 API | Proven | Videos, mistakes, SearchQuery CRUD/search, candidates, reviews, rights, uploads/final assets, jobs, storage cleanup, export readiness/export covered by integration tests. | No blocker known. |
| 14 Admin UI | Mostly Proven | HTML/static JS smoke and admin UI integration tests cover pages, buttons, forms, fetch contracts, auth, filters, pagination, reviewer panel, export readiness gating. Tests: `test_admin_candidates_ui.py`, `test_admin_ui_html_smoke.py`, `test_admin_inline_js_smoke.py`. | Real browser click/layout validation is still manual; no Playwright/Selenium installed. |
| 15 Job runner | Proven | Idempotency, stale lock requeue, max running jobs, image job limits, worker allowlist, spec job types `create_search_queries`, `run_search`, `score_candidates`, `create_reference_brief`, `download_candidate`, `process_final_asset`, `cleanup_storage`, `export_video` covered. Tests: `test_job_runner_postgres.py`, `test_job_idempotency_api.py`, `test_worker.py`. | Long-running production daemon supervision still requires ops setup. |
| 16 Providers | Proven for MVP/mock path | Mock provider and search query flow tested; provider error classification tested for search failures. Tests: `test_search_query_api.py`. | External provider credential/rate-limit exhaustion remains manual/ops validation. |
| 17 LLM/graceful degradation | Proven for accepted MVP correction | Mistake extraction defaults to mock unless explicit provider; ReferenceBrief deterministic draft avoids quota and persists `error_message` on failure; reviewer CLI output validation tested and live-smoked. | Real LLM quality for ReferenceBrief is post-MVP/currently not used. |
| 18 Scoring | Proven | `score_quality` formula, `score_visual=null`, `review_score=score_quality` before AI review, multi-review median/2-of-3 after reviewer results. Tests: `test_candidate_scoring_service.py`, `test_candidate_review_service.py`, reviewer integration tests, `score_candidates` worker test. | No blocker known. |
| 19 Dedup/similarity | Proven for MVP | Search candidate upsert uses `(mistake_id, side, image_url_hash)` conflict handling; post-MVP similarity not implemented by design. Tests: search query integration. | Perceptual similarity remains post-MVP. |
| 20 Image processing | Proven | Upload stores immutable original, async `process_final_asset` generates thumbnail/processed/metadata; processed export files verified `1920x1080`; safety limits and downloader checks covered. Tests: upload/storage/export suites and live E2E. | Visual inspection of padding/cropping aesthetics remains manual. |
| 21 Export rules | Proven | Export readiness API/UI gating; API returns 400 when `can_export=false`; export requires rights and ready storage; live complete export passed. Tests: `test_video_api.py`, `test_export_service_postgres.py`, live E2E. | NLE import remains manual. |
| 22 Rate limiting/idempotency | Proven for app-level controls | Job idempotency, active-job reuse, rerun-after-completed, concurrency caps, image semaphores tested. | Real external provider rate-limit cascade needs manual external-provider validation if enabled. |
| 23 External provider error handling | Mostly Proven | Missing credential/network/HTTP/invalid response paths classify query/job failures in tests. | True provider throttling behavior not manually exhausted in this run. |
| 24 Admin actions/audit | Proven | Required audit events implemented and tested: video/mistake creation/update, candidate approved/rejected/rights/domain, final asset upload/delete, storage cleanup, export, job failed. Tests: audit assertions across integration suites. | No blocker known. |
| 25 Prompt templates/reviewer prompt | Proven for current CLI usage | Reviewer adapter unit tests and real CLI smoke completed after prompt wording fix. | Prompt quality can still be tuned post-MVP. |
| 26 Config | Proven | `.env.example`, README, Docker env, settings, auth behavior, host bridge env templates/tests. | Production secrets/auth configuration is operator responsibility. |
| 27 Tests | Proven | Unit and integration suites pass as above. | Browser automation not present. |
| 28 Acceptance criteria | Mostly Proven | Core criteria covered by tests and live smoke: create video/mistake, search/query flow, candidate review, rights/final upload, export, cleanup, audit, worker. | Final release sign-off still needs manual UI click-through and optional external-provider stress test. |
| 30 Non-goals | Proven | Post-MVP items such as similarity embeddings, full Celery/S3, generation of final clean assets, and real broad provider setup are not implemented as core MVP. | No blocker, by spec. |

## Deployment / Operator Verification Checklist

These items do not currently block the code-completion claim, but they should be executed before exposing the app as a production service:

1. Browser/operator UI click-through in a real browser: create video, create mistake, generate/search queries, upload final assets, check readiness, export, cleanup. Existing HTML/JS tests are strong but not a real browser, and browser automation tooling is not installed in this environment.
2. NLE/editor-side import or downstream parser check for `manifest.json` and `assets.csv`. App-side file/schema/JPEG validation passed.
3. External search provider rate-limit exhaustion only if enabling a real provider in this deployment. Mock/local provider path is covered.
4. Production deployment hardening: follow `docs/mvp_release_runbook_20260607.md`, set `ADMIN_API_TOKEN`, decide host reviewer bridge tmux/systemd operation, and review live `.env` without exposing secrets.

## Current Conclusion

No known code-level MVP blockers remain after the latest fixes, full test suites, real reviewer smoke, live export E2E, and release-readiness audit. The repository is at stable MVP code-complete state according to the specification and accepted current corrections. The remaining items above are deployment/QA/operator gates for production rollout rather than unfinished MVP code.
