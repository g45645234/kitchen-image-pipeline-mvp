from html.parser import HTMLParser

import pytest


class SmokeHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.title_text = []
        self.ids = []
        self.forms = []
        self.buttons = []
        self.links = []
        self.scripts = []
        self._script_depth = 0
        self._script_chunks = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        self.stack.append(tag)
        if attr.get("id"):
            self.ids.append(attr["id"])
        if tag == "form":
            self.forms.append(attr)
        elif tag == "button":
            self.buttons.append(attr)
        elif tag == "a":
            self.links.append(attr)
        elif tag == "script":
            self._script_depth += 1
            self._script_chunks = []

    def handle_endtag(self, tag):
        if tag == "script" and self._script_depth:
            self.scripts.append("".join(self._script_chunks))
            self._script_depth -= 1
            self._script_chunks = []
        if self.stack:
            self.stack.pop()

    def handle_data(self, data):
        if self.stack and self.stack[-1] == "title":
            self.title_text.append(data)
        if self._script_depth:
            self._script_chunks.append(data)


def parse_html(html: str) -> SmokeHTMLParser:
    parser = SmokeHTMLParser()
    parser.feed(html)
    return parser


def assert_common_html_contract(html: str):
    parser = parse_html(html)
    title = "".join(parser.title_text).strip()
    assert title
    assert "<script" not in title.lower()
    assert "fetch(" not in title
    assert "{{" not in html
    assert "{%" not in html
    assert not any(link.get("href") == "#" for link in parser.links)
    duplicate_ids = {item for item in parser.ids if parser.ids.count(item) > 1}
    assert duplicate_ids == set()
    return parser


def scripts_text(parser: SmokeHTMLParser) -> str:
    return "\n".join(parser.scripts)


def assert_script_markers(script: str, *markers: str):
    missing = [marker for marker in markers if marker not in script]
    assert missing == []


@pytest.mark.asyncio
async def test_videos_page_html_browser_smoke(client, seed_video):
    video = await seed_video(title="Smoke Video", slug="smoke-video")

    response = await client.get("/ui/videos")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    script = scripts_text(parser)
    assert any(form.get("id") == "create-video-form" for form in parser.forms)
    assert any(form.get("class") == "form-grid edit-video-form" and form.get("data-video-id") == str(video.id) for form in parser.forms)
    assert any(button.get("class") == "secondary-button delete-video-button" and button.get("data-video-id") == str(video.id) for button in parser.buttons)
    assert_script_markers(
        script,
        ".edit-video-form",
        "/api/videos/${form.dataset.videoId}?actor=admin-ui",
        "method: 'PATCH'",
        "body: JSON.stringify(data)",
        ".delete-video-button",
        "/api/videos/${button.dataset.videoId}?actor=admin-ui",
        "method: 'DELETE'",
        "window.confirm('Удалить видео и связанные карточки?')",
        "document.getElementById('create-video-form')",
        "fetch('/api/videos'",
        "method: 'POST'",
        "headers: { 'Content-Type': 'application/json' }",
        "body: JSON.stringify(data)",
        "window.location.href",
    )
    assert any(link.get("href") == f"/ui/videos/{video.id}/mistakes" for link in parser.links)


@pytest.mark.asyncio
async def test_video_mistakes_page_html_browser_smoke(client, seed_video, seed_mistake):
    video = await seed_video(title="Smoke Workflow", slug="smoke-workflow")
    mistake = await seed_mistake(video=video, title="Smoke mistake")

    response = await client.get(f"/ui/videos/{video.id}/mistakes")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    script = scripts_text(parser)
    assert any(form.get("id") == "create-mistake-form" for form in parser.forms)
    assert any(form.get("class") == "form-grid edit-mistake-form" and form.get("data-mistake-id") == str(mistake.id) for form in parser.forms)
    assert any(button.get("id") == "export-video-button" for button in parser.buttons)
    assert any(button.get("class") == "secondary-button delete-mistake-button" and button.get("data-mistake-id") == str(mistake.id) for button in parser.buttons)
    assert any(button.get("data-mistake-id") == str(mistake.id) for button in parser.buttons)
    assert_script_markers(
        script,
        ".edit-mistake-form",
        "/api/mistakes/${form.dataset.mistakeId}?actor=admin-ui",
        "method: 'PATCH'",
        "data.order_index = Number(data.order_index)",
        "data.negative_criteria",
        ".delete-mistake-button",
        "/api/mistakes/${button.dataset.mistakeId}?actor=admin-ui",
        "method: 'DELETE'",
        "window.confirm('Удалить карточку ошибки?')",
        "document.getElementById('create-mistake-form')",
        "/api/videos/${form.dataset.videoId}/mistakes",
        "data.order_index = Number(data.order_index)",
        "data.negative_criteria",
        "headers: { 'Content-Type': 'application/json' }",
        "body: JSON.stringify(data)",
        "/api/mistakes/${button.dataset.mistakeId}/search",
        "body: JSON.stringify(defaultSearchPayload())",
        "/api/videos/${button.dataset.videoId}/export",
        "method: 'POST'",
        "showJob(await response.json(), 'Поиск поставлен в очередь')",
        "showJob(await response.json(), 'Export поставлен в очередь')",
    )


@pytest.mark.asyncio
async def test_candidates_page_html_browser_smoke(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake(title="Candidate smoke")
    candidate = await seed_candidate(mistake=mistake, status="approved_reference", usage_role="reference_only")

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    script = scripts_text(parser)
    assert any(form.get("class") == "upload-final-form" and form.get("data-side") == "wrong" for form in parser.forms)
    assert any(form.get("class") == "upload-final-form" and form.get("data-side") == "right" for form in parser.forms)
    assert any(form.get("class") == "reject-candidate-form" and form.get("data-candidate-id") == str(candidate.id) for form in parser.forms)
    assert any(button.get("class") == "review-run-button" and button.get("data-candidate-id") == str(candidate.id) for button in parser.buttons)
    assert any(button.get("class") == "rights-button" and button.get("data-candidate-id") == str(candidate.id) for button in parser.buttons)
    assert any(button.get("class") == "reference-button" and button.get("data-candidate-id") == str(candidate.id) for button in parser.buttons)
    assert any(button.get("class") == "reference-brief-button" and button.get("data-candidate-id") == str(candidate.id) for button in parser.buttons)
    assert any(button.get("class") == "block-button" and button.get("data-candidate-id") == str(candidate.id) for button in parser.buttons)
    assert_script_markers(
        script,
        ".generate-search-queries-button",
        "/api/mistakes/${button.dataset.mistakeId}/generate-search-queries",
        ".manual-search-query-form",
        "/api/mistakes/${form.dataset.mistakeId}/search-queries",
        "data.results_count = Number(data.results_count || 20)",
        ".edit-search-query-button",
        "/api/mistakes/${button.dataset.mistakeId}/search-queries/${button.dataset.queryId}",
        "method: 'PATCH'",
        ".delete-search-query-button",
        "method: 'DELETE'",
        ".search-button",
        "/api/mistakes/${button.dataset.mistakeId}/search",
        "body: JSON.stringify(defaultSearchPayload())",
        ".upload-final-form",
        "const data = new FormData(form)",
        "data.append('side', form.dataset.side)",
        "/api/mistakes/${form.dataset.mistakeId}/upload-final-asset",
        "body: data",
        ".review-run-button",
        "/api/candidates/${candidateId}/reviews/run",
        "headers: { 'Content-Type': 'application/json' }",
        "body: JSON.stringify({})",
        ".select-final-form",
        "/api/candidates/${candidateId}/select-final",
        ".rights-button",
        "/api/candidates/${candidateId}/confirm-rights",
        "if (!comment || !comment.trim()) return",
        "rights_status: 'manual_licensed'",
        "source_url: button.dataset.sourceUrl || null",
        "author_name: button.dataset.authorName || null",
        "license_note: comment.trim()",
        "comment: comment.trim()",
        ".final-rights-button",
        "/api/final-assets/${assetId}/confirm-rights",
        "const assetId = button.dataset.assetId",
        "author_name: button.dataset.authorName || null",
        ".reference-button",
        "/api/candidates/${candidateId}/use-as-reference",
        "if (comment === null) return",
        "mark_high_value: true",
        ".reference-brief-button",
        "/api/candidates/${candidateId}/reference-brief",
        ".reject-candidate-form",
        "/api/candidates/${candidateId}/review",
        "const reason = new FormData(form).get('reason')",
        "action: 'reject'",
        ".block-button",
        "/api/candidates/${candidateId}/block-domain",
        "if (reason === null) return",
        "reason: reason.trim() || null",
        "window.location.reload",
    )


@pytest.mark.asyncio
async def test_jobs_page_html_browser_smoke(client, db_session):
    from app.models.job import Job

    db_session.add(Job(type="cleanup_storage", status="pending", payload={"dry_run": True}))
    await db_session.commit()

    response = await client.get("/ui/jobs")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    script = scripts_text(parser)
    assert any(button.get("class") == "secondary-button cleanup-button cleanup-dry-run-button" for button in parser.buttons)
    assert any(button.get("class") == "secondary-button cleanup-button cleanup-delete-button" for button in parser.buttons)
    assert_script_markers(
        script,
        ".cleanup-button",
        "/api/jobs/cleanup?dry_run=${button.dataset.dryRun}",
        "method: 'POST'",
        "window.location.reload",
    )


@pytest.mark.asyncio
async def test_admin_candidates_alias_keeps_action_contracts(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake(title="Admin Candidate smoke")
    candidate = await seed_candidate(mistake=mistake)

    response = await client.get(f"/admin/videos/{mistake.video_id}/mistakes/{mistake.id}")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    script = scripts_text(parser)
    hrefs = [link.get("href") for link in parser.links]
    assert f"/admin/videos/{mistake.video_id}/mistakes" in hrefs
    assert any(button.get("class") == "review-run-button" and button.get("data-candidate-id") == str(candidate.id) for button in parser.buttons)
    assert_script_markers(
        script,
        "/api/candidates/${candidateId}/reviews/run",
        "/api/candidates/${candidateId}/confirm-rights",
        "/api/candidates/${candidateId}/use-as-reference",
        "/api/candidates/${candidateId}/block-domain",
        "/api/candidates/${candidateId}/review",
        "/api/mistakes/${form.dataset.mistakeId}/upload-final-asset",
    )


@pytest.mark.asyncio
async def test_admin_alias_header_uses_admin_prefix(client, seed_video):
    video = await seed_video(title="Admin Smoke", slug="admin-smoke")

    response = await client.get("/admin/videos")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    hrefs = [link.get("href") for link in parser.links]
    assert "/admin/videos" in hrefs
    assert "/admin/jobs" in hrefs
    assert "/ui/videos" in hrefs
    assert f"/admin/videos/{video.id}/mistakes" in hrefs


@pytest.mark.asyncio
async def test_ui_alias_header_links_to_admin_cross_alias(client, seed_video):
    video = await seed_video(title="UI Smoke", slug="ui-smoke")

    response = await client.get("/ui/videos")

    assert response.status_code == 200
    parser = assert_common_html_contract(response.text)
    hrefs = [link.get("href") for link in parser.links]
    assert "/ui/videos" in hrefs
    assert "/ui/jobs" in hrefs
    assert "/admin/videos" in hrefs
    assert f"/ui/videos/{video.id}/mistakes" in hrefs
