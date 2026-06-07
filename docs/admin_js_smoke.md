# Admin Inline JavaScript Smoke

`scripts/smoke_admin_inline_js.mjs` is a lightweight host-side smoke test for the server-rendered admin UI. It fetches live HTML from `ADMIN_UI_BASE_URL` (default `http://127.0.0.1:8000`), extracts inline `<script>` blocks, executes them in a Node VM, and uses mocked `document`, `window`, `fetch`, `FormData`, `alert`, `confirm`, and `prompt` objects to trigger the main click/submit handlers.

It covers the MVP admin workflow contracts for creating/editing/deleting videos and mistakes, candidate search, reviewer job enqueueing, rights confirmation, reference/reject/block actions, own final upload, final selection, cleanup, and export. The mocked `fetch` calls are asserted for URL, method, selected JSON/FormData payloads, and successful reload/navigation behavior. No external reviewer CLI/API calls are made.

Limitations: this is not a browser E2E replacement. It does not verify CSS, layout, actual clickability, browser form validation, event bubbling fidelity, file input behavior, or real navigation/rendering after reload. Playwright or Selenium should still be added later if the UI becomes more complex or if visual/clickability regressions become a material risk.

Run from the project root while the app is running:

```bash
ADMIN_UI_BASE_URL=http://127.0.0.1:8000 PROJECT_ROOT=/home/claudecode/kitchen-image-pipeline-mvp node scripts/smoke_admin_inline_js.mjs
```
