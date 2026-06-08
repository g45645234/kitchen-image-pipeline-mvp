import pytest


@pytest.mark.asyncio
async def test_candidates_ui_renders_run_reviewers_action(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    candidate = await seed_candidate(mistake=mistake)

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert 'class="review-run-button"' in response.text
    assert f'data-candidate-id="{candidate.id}"' in response.text
    assert f'/api/candidates/${{candidateId}}/reviews/run' in response.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_reviewer_readiness_panel(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    await seed_candidate(mistake=mistake)

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert "Reviewers:" in response.text
    assert "codex:" in response.text
    assert "antigravity:" in response.text
    assert "claude_cli:" in response.text


@pytest.mark.asyncio
async def test_candidates_ui_links_downloaded_candidate_to_local_original(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    local_candidate = await seed_candidate(
        mistake=mistake,
        storage_key_original="candidates/local.jpg",
        storage_status="ok",
        image_url="https://blocked.example/full.jpg",
        thumbnail_url="https://blocked.example/thumb.jpg",
        image_url_hash="local-original-link",
    )

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert f'href="/api/candidates/{local_candidate.id}/original"' in response.text
    assert f'src="/api/candidates/{local_candidate.id}/original"' in response.text
    assert f'<a href="/api/candidates/{local_candidate.id}/original" target="_blank">Открыть полную локальную картинку</a>' in response.text
    assert 'внешний оригинал' not in response.text


@pytest.mark.asyncio
async def test_candidates_ui_marks_yandex_candidate_without_local_original_unavailable(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    candidate = await seed_candidate(
        mistake=mistake,
        source_provider="yandex_search_api",
        storage_key_original=None,
        storage_status="pending",
        image_url="https://blocked.example/full.jpg",
        thumbnail_url="http://avatars.mds.yandex.net/i?id=thumb",
        image_url_hash="yandex-no-local-original",
    )

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert f'href="https://blocked.example/full.jpg"' not in response.text
    assert 'src="http://avatars.mds.yandex.net/i?id=thumb"' in response.text
    assert 'полная картинка не скачана' in response.text
    assert 'Нет полной картинки: доступен только маленький thumbnail, ручная оценка невозможна' in response.text
    assert f'data-candidate-id="{candidate.id}"' in response.text


@pytest.mark.asyncio
async def test_admin_root_and_alias_list_videos(client, seed_video):
    video = await seed_video(title="Small kitchen review", slug="small-kitchen-review")

    ui_response = await client.get("/ui")
    admin_response = await client.get("/admin")

    for response, prefix in [(ui_response, "/ui"), (admin_response, "/admin")]:
        assert response.status_code == 200
        assert "Kitchen Image Pipeline Admin" in response.text
        assert "Small kitchen review" in response.text
        assert "small-kitchen-review" in response.text
        assert f'href="{prefix}/videos/{video.id}/mistakes"' in response.text
        assert 'href="#">Видео</a>' not in response.text


@pytest.mark.asyncio
async def test_videos_ui_lists_videos_and_links_to_mistakes(client, seed_video):
    video = await seed_video(title="Video UI Test", slug="video-ui-test")

    response = await client.get("/ui/videos")

    assert response.status_code == 200
    assert "Video UI Test" in response.text
    assert "video-ui-test" in response.text
    assert f'href="/ui/videos/{video.id}/mistakes"' in response.text
    assert 'class="form-grid edit-video-form"' in response.text
    assert f'data-video-id="{video.id}"' in response.text
    assert 'class="secondary-button delete-video-button"' in response.text
    assert f'/api/videos/${{form.dataset.videoId}}?actor=admin-ui' in response.text
    assert f'/api/videos/${{button.dataset.videoId}}?actor=admin-ui' in response.text
    assert "method: 'PATCH'" in response.text
    assert "method: 'DELETE'" in response.text


@pytest.mark.asyncio
async def test_video_mistakes_ui_renders_workflow_actions(client, seed_video, seed_mistake):
    video = await seed_video(title="Workflow Video", slug="workflow-video")
    mistake = await seed_mistake(
        video=video,
        order_index=2,
        title="Bad workflow lighting",
        short_title="Bad lighting",
        wrong_visual_prompt="dark countertop",
        right_visual_prompt="lit countertop",
    )

    response = await client.get(f"/ui/videos/{video.id}/mistakes")

    assert response.status_code == 200
    assert "Workflow Video" in response.text
    assert "Bad workflow lighting" in response.text
    assert "dark countertop" in response.text
    assert "lit countertop" in response.text
    assert f'href="/ui/videos/{video.id}/mistakes/{mistake.id}"' in response.text
    assert 'class="secondary-button search-candidates-button"' in response.text
    assert f'data-mistake-id="{mistake.id}"' in response.text
    assert "/api/mistakes/${button.dataset.mistakeId}/search" in response.text
    assert "body: JSON.stringify(defaultSearchPayload())" in response.text
    assert 'class="button export-video-button"' in response.text
    assert f'data-video-id="{video.id}"' in response.text
    assert 'disabled title="Export заблокирован: нет export-ready final assets"' in response.text
    assert "/api/videos/${button.dataset.videoId}/export" in response.text
    assert f'href="/api/videos/{video.id}/export-readiness"' in response.text
    assert "Готовность к export" in response.text
    assert "blocked: нет export-ready final assets" in response.text
    assert "missing_final_assets" in response.text
    assert f'href="/api/videos/{video.id}/manifest"' in response.text
    assert f'href="/api/videos/{video.id}/assets-csv"' in response.text
    assert 'class="form-grid edit-mistake-form"' in response.text
    assert f'data-mistake-id="{mistake.id}"' in response.text
    assert 'class="secondary-button delete-mistake-button"' in response.text
    assert f'/api/mistakes/${{form.dataset.mistakeId}}?actor=admin-ui' in response.text
    assert f'/api/mistakes/${{button.dataset.mistakeId}}?actor=admin-ui' in response.text
    assert "method: 'PATCH'" in response.text
    assert "method: 'DELETE'" in response.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_search_and_upload_final_contract(client, seed_mistake):
    mistake = await seed_mistake()

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert f'href="/ui/videos/{mistake.video_id}/mistakes"' in response.text
    assert 'class="search-button"' in response.text
    assert 'class="manual-search-query-form"' in response.text
    assert 'name="query_text"' in response.text
    assert 'name="source_provider"' in response.text
    assert 'name="results_count"' in response.text
    assert f'data-mistake-id="{mistake.id}"' in response.text
    assert "/api/mistakes/${button.dataset.mistakeId}/generate-search-queries" in response.text
    assert "/api/mistakes/${button.dataset.mistakeId}/search" in response.text
    assert "/api/mistakes/${form.dataset.mistakeId}/search-queries" in response.text
    assert "SearchQuery еще не созданы" in response.text
    assert 'class="upload-final-form"' in response.text
    assert f'data-mistake-id="{mistake.id}"' in response.text
    assert 'data-side="wrong"' in response.text
    assert 'data-side="right"' in response.text
    assert 'name="file"' in response.text
    assert 'name="license_note"' in response.text
    assert 'name="author_name"' in response.text
    assert 'name="license_document_ref"' in response.text
    assert "/api/mistakes/${form.dataset.mistakeId}/upload-final-asset" in response.text


@pytest.mark.asyncio
async def test_candidates_ui_supports_spec_filters_sort_and_pagination(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    first = await seed_candidate(
        mistake=mistake,
        side="wrong",
        status="review",
        rights_status="manual_licensed",
        source_provider="mock_search",
        review_score=0.4,
        image_url="https://example.com/first.jpg",
        image_url_hash="first-filtered",
    )
    best = await seed_candidate(
        mistake=mistake,
        side="wrong",
        status="review",
        rights_status="manual_licensed",
        source_provider="mock_search",
        review_score=0.9,
        image_url="https://example.com/best.jpg",
        image_url_hash="best-filtered",
    )
    filtered_out = await seed_candidate(
        mistake=mistake,
        side="right",
        status="new",
        rights_status="unknown",
        source_provider="other",
        review_score=1.0,
        image_url="https://example.com/filtered-out.jpg",
        image_url_hash="filtered-out",
    )

    response = await client.get(
        f"/ui/mistakes/{mistake.id}/candidates"
        "?side=wrong&status=review&rights_status=manual_licensed&source_provider=mock_search"
        "&sort=-review_score&limit=1&offset=0"
    )

    assert response.status_code == 200
    assert 'class="candidate-filter-form"' in response.text
    assert 'name="side"' in response.text
    assert 'name="status"' in response.text
    assert 'name="rights_status"' in response.text
    assert 'name="source_provider"' in response.text
    assert 'name="sort"' in response.text
    assert 'name="limit"' in response.text
    assert f'href="/ui/mistakes/{mistake.id}/candidates?side=wrong&amp;status=review&amp;rights_status=manual_licensed&amp;source_provider=mock_search&amp;sort=-review_score&amp;limit=1&amp;offset=1"' in response.text
    assert "1-1 / 2" in response.text
    assert f'data-candidate-id="{best.id}"' in response.text
    assert f'data-candidate-id="{first.id}"' not in response.text
    assert f'data-candidate-id="{filtered_out.id}"' not in response.text
    assert "status: review" in response.text
    assert "rights: manual_licensed" in response.text
    assert "review: 0.90" in response.text

    page_two = await client.get(
        f"/ui/mistakes/{mistake.id}/candidates"
        "?side=wrong&status=review&rights_status=manual_licensed&source_provider=mock_search"
        "&sort=-review_score&limit=1&offset=1"
    )
    assert page_two.status_code == 200
    assert f'data-candidate-id="{first.id}"' in page_two.text
    assert f'data-candidate-id="{best.id}"' not in page_two.text
    assert "2-2 / 2" in page_two.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_candidate_source_license_and_query(client, db_session, seed_mistake, seed_candidate):
    from app.models.candidate import SearchQuery

    mistake = await seed_mistake()
    query = SearchQuery(
        mistake_id=mistake.id,
        side="wrong",
        source_provider="mock_search",
        query_text="specific cabinet query",
        status="completed",
        results_count=20,
    )
    db_session.add(query)
    await db_session.commit()
    await db_session.refresh(query)

    await seed_candidate(
        mistake=mistake,
        query_id=query.id,
        source_type="mock",
        source_provider="mock_search",
        license_label="mock_unknown",
        image_url="https://example.com/source-license-query.jpg",
        image_url_hash="source-license-query",
    )

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert "source: mock" in response.text
    assert "license: mock_unknown" in response.text
    assert "query: specific cabinet query" in response.text


@pytest.mark.asyncio
async def test_candidates_ui_selected_view_filters_before_pagination(client, db_session, seed_mistake, seed_candidate):
    from app.models.asset import FinalAsset

    mistake = await seed_mistake()
    unselected = await seed_candidate(
        mistake=mistake,
        side="wrong",
        status="review",
        image_url="https://example.com/unselected-first.jpg",
        image_url_hash="unselected-first",
    )
    selected = await seed_candidate(
        mistake=mistake,
        side="wrong",
        status="review",
        image_url="https://example.com/selected-second.jpg",
        image_url_hash="selected-second",
        may_use_directly=True,
        rights_status="manual_licensed",
    )
    db_session.add(
        FinalAsset(
            video_id=mistake.video_id,
            mistake_id=mistake.id,
            side="wrong",
            candidate_id=selected.id,
            source_type="search",
            source_url=selected.image_url,
            rights_status="manual_licensed",
            may_use_directly=True,
            status="approved",
            storage_status="ok",
        )
    )
    await db_session.commit()

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates?view=selected&limit=1&offset=0")

    assert response.status_code == 200
    assert "1-1 / 1" in response.text
    assert f'data-candidate-id="{selected.id}"' in response.text
    assert f'data-candidate-id="{unselected.id}"' not in response.text
    assert 'href="/ui/mistakes/' not in response.text or "offset=1" not in response.text


@pytest.mark.asyncio
async def test_candidates_ui_status_filter_uses_spec_values_and_aliases(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    spec_candidate = await seed_candidate(mistake=mistake, status="approved_reference", image_url_hash="ui-spec-reference")
    legacy_candidate = await seed_candidate(mistake=mistake, status="reference_only", image_url_hash="ui-legacy-reference")
    filtered_out = await seed_candidate(mistake=mistake, status="rejected", image_url_hash="ui-rejected")

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates?status=approved_reference&sort=id")

    assert response.status_code == 200
    assert 'value="approved_reference" selected' in response.text
    assert 'value="approved_final"' in response.text
    assert f'data-candidate-id="{spec_candidate.id}"' in response.text
    assert f'data-candidate-id="{legacy_candidate.id}"' in response.text
    assert f'data-candidate-id="{filtered_out.id}"' not in response.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_existing_search_queries(client, db_session, seed_mistake):
    from app.models.candidate import SearchQuery

    mistake = await seed_mistake()
    query = SearchQuery(
        mistake_id=mistake.id,
        side="wrong",
        source_provider="mock_search",
        query_text="dark narrow kitchen",
        status="pending",
        results_count=20,
    )
    db_session.add(query)
    await db_session.commit()

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert 'class="search-query-panel"' in response.text
    assert "mock_search" in response.text
    assert "dark narrow kitchen" in response.text
    assert "pending" in response.text
    assert 'class="edit-search-query-button"' in response.text
    assert 'class="delete-search-query-button"' in response.text
    assert f'data-query-id="{query.id}"' in response.text
    assert "/api/mistakes/${button.dataset.mistakeId}/search-queries/${button.dataset.queryId}" in response.text


@pytest.mark.asyncio
async def test_candidates_ui_warns_about_legacy_broken_final_asset(client, db_session, seed_mistake):
    from app.models.asset import FinalAsset

    mistake = await seed_mistake()
    asset = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="wrong",
        source_type="legacy",
        rights_status="unknown",
        may_use_directly=False,
        status="approved",
        storage_status="ok",
    )
    db_session.add(asset)
    await db_session.commit()

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert "wrong =" in response.text
    assert 'class="final-warning"' in response.text
    assert "rights_not_exportable" in response.text
    assert "missing_storage_keys" in response.text
    assert "missing_processed_asset" in response.text
    assert 'class="final-rights-button"' in response.text
    assert f'data-asset-id="{asset.id}"' in response.text
    assert f'/api/final-assets/${{assetId}}/confirm-rights' in response.text


@pytest.mark.asyncio
async def test_candidates_ui_warns_about_missing_physical_final_asset_files(client, db_session, seed_mistake):
    from app.models.asset import FinalAsset

    mistake = await seed_mistake()
    asset = FinalAsset(
        video_id=mistake.video_id,
        mistake_id=mistake.id,
        side="right",
        source_type="own_upload",
        rights_status="own",
        may_use_directly=True,
        status="approved",
        storage_status="ok",
        storage_key_original="projects/1/final_assets/99/original.jpg",
        storage_key_thumbnail="projects/1/final_assets/99/thumb.jpg",
        storage_key_processed="projects/1/final_assets/99/processed_1920x1080.jpg",
    )
    db_session.add(asset)
    await db_session.commit()

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert "right =" in response.text
    assert 'class="final-warning"' in response.text
    assert "missing_storage_files" in response.text
    assert "missing_storage_keys" not in response.text
    assert f'/api/final-assets/{asset.id}/thumbnail' not in response.text
    assert f'/api/final-assets/{asset.id}/processed' not in response.text


@pytest.mark.asyncio
async def test_videos_ui_renders_create_video_form_contract(client):
    response = await client.get("/ui/videos")

    assert response.status_code == 200
    assert 'class="form-grid create-video-form"' in response.text
    assert 'name="title"' in response.text
    assert 'name="slug"' in response.text
    assert 'name="transcript"' in response.text
    assert "fetch('/api/videos'" in response.text
    assert "method: 'POST'" in response.text
    assert "JSON.stringify(data)" in response.text
    assert "window.location.href" in response.text


@pytest.mark.asyncio
async def test_video_mistakes_ui_renders_create_mistake_form_contract(client, seed_video):
    video = await seed_video(title="Manual Mistake Video", slug="manual-mistake-video")

    response = await client.get(f"/ui/videos/{video.id}/mistakes")

    assert response.status_code == 200
    assert 'class="form-grid create-mistake-form"' in response.text
    assert f'data-video-id="{video.id}"' in response.text
    for field in [
        'name="order_index"',
        'name="title"',
        'name="short_title"',
        'name="explanation"',
        'name="wrong_visual_prompt"',
        'name="right_visual_prompt"',
        'name="negative_criteria"',
        'name="time_start"',
        'name="time_end"',
    ]:
        assert field in response.text
    assert "/api/videos/${form.dataset.videoId}/mistakes" in response.text
    assert "JSON.stringify(data)" in response.text
    assert "window.location.reload" in response.text


@pytest.mark.asyncio
async def test_jobs_ui_renders_dashboard_and_cleanup_actions(client, db_session):
    from app.models.job import Job

    job = Job(type="cleanup_storage", status="pending", payload={"dry_run": True}, attempts=0, max_attempts=3)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)

    response = await client.get("/ui/jobs")
    admin_response = await client.get("/admin/jobs")

    for page in [response, admin_response]:
        assert page.status_code == 200
        assert "Jobs" in page.text
        assert "/api/jobs" in page.text
        assert 'class="table-list job-list"' in page.text
        assert "status:" in page.text
        assert "type:" in page.text
        assert "payload:" in page.text
        assert "created_at:" in page.text
        assert "attempts" in page.text
        assert "cleanup_storage" in page.text
        assert "pending" in page.text
        assert "dry_run" in page.text
        assert 'class="secondary-button cleanup-button cleanup-dry-run-button"' in page.text
        assert 'class="secondary-button cleanup-button cleanup-delete-button"' in page.text
        assert "/api/jobs/cleanup?dry_run=${button.dataset.dryRun}" in page.text
        assert "method: 'POST'" in page.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_reject_candidate_action_contract(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    candidate = await seed_candidate(mistake=mistake)

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert 'class="reject-candidate-form"' in response.text
    assert f'data-candidate-id="{candidate.id}"' in response.text
    assert 'name="reason"' in response.text
    assert 'value="bad_quality"' in response.text
    assert 'value="irrelevant"' in response.text
    assert 'value="rights_risk"' in response.text
    assert 'class="action-status" aria-live="polite"' in response.text
    assert "/api/candidates/${candidateId}/review" in response.text
    assert "/api/jobs/${jobId}" in response.text
    assert "waitForJobCompletion(job.id, form, 'Отклонение')" in response.text
    assert "renderRejectedCard(form, reason)" in response.text
    assert "setCardPending(form, true)" in response.text
    assert "action: 'reject'" in response.text
    assert "JSON.stringify" in response.text
    assert "method: 'POST'" in response.text
    assert "await response.json()" in response.text


@pytest.mark.asyncio
async def test_candidates_ui_reference_and_rights_actions_do_not_prompt_for_comments(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    await seed_candidate(mistake=mistake, may_use_directly=False, rights_status="unknown")

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert "где получена лицензия или почему можно использовать" not in response.text
    assert "Комментарий для reference-only" not in response.text
    assert "debugRightsComment" in response.text
    assert "license_note: debugRightsComment" in response.text
    assert "comment: debugRightsComment" in response.text
    assert "body: JSON.stringify({ mark_high_value: true, comment: null })" in response.text
    assert "renderReferenceCard(button)" in response.text
    assert "brief:" not in response.text
    assert "reference-brief-panel" not in response.text
    assert "reference-brief-button" not in response.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_reference_candidate_as_terminal_state(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    reference = await seed_candidate(
        mistake=mistake,
        status="approved_reference",
        usage_role="reference_only",
        reference_priority_score=1.0,
        may_use_directly=False,
        rights_status="unknown",
        image_url_hash="ui-reference-terminal",
    )

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert f'data-candidate-id="{reference.id}"' not in response.text
    assert 'card-reference' in response.text
    assert 'decision-banner decision-reference' in response.text
    assert 'В референсе: priority 1.00' in response.text
    assert 'Final недоступен: права не подтверждены' not in response.text
    assert 'Нужен комментарий о лицензии/разрешении.' not in response.text


@pytest.mark.asyncio
async def test_candidates_ui_renders_rejected_candidate_as_terminal_state(client, seed_mistake, seed_candidate):
    mistake = await seed_mistake()
    rejected = await seed_candidate(
        mistake=mistake,
        status="rejected",
        reject_reason="bad_quality",
        image_url_hash="ui-rejected-terminal",
    )

    response = await client.get(f"/ui/mistakes/{mistake.id}/candidates")

    assert response.status_code == 200
    assert f'data-candidate-id="{rejected.id}"' not in response.text
    assert 'card-rejected' in response.text
    assert 'decision-banner decision-rejected' in response.text
    assert 'Отклонено: bad_quality' in response.text


@pytest.mark.asyncio
async def test_admin_nested_candidates_alias_renders_candidate_page(client, seed_video, seed_mistake, seed_candidate):
    video = await seed_video()
    mistake = await seed_mistake(video=video)
    candidate = await seed_candidate(mistake=mistake)

    response = await client.get(f"/admin/videos/{video.id}/mistakes/{mistake.id}")

    assert response.status_code == 200
    assert mistake.title in response.text
    assert f'data-candidate-id="{candidate.id}"' in response.text
    assert f'href="/admin/videos/{video.id}/mistakes"' in response.text
