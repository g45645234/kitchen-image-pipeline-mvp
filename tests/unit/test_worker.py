import asyncio

import pytest

from app.config import settings
from app.services.job_runner import allowed_worker_job_types
from app.worker import run_worker


@pytest.mark.asyncio
async def test_run_worker_processes_jobs_until_stopped(monkeypatch):
    calls = 0
    stop_event = asyncio.Event()

    async def fake_fetch_and_run_jobs():
        nonlocal calls
        calls += 1
        stop_event.set()

    monkeypatch.setattr("app.worker.fetch_and_run_jobs", fake_fetch_and_run_jobs)
    monkeypatch.setattr(settings, "worker_poll_interval_seconds", 0.01)

    await run_worker(stop_event)

    assert calls == 1


def test_allowed_worker_job_types_parses_csv(monkeypatch):
    monkeypatch.setattr(settings, "worker_job_types", " search_all_queries, export_final_assets ,, ")

    assert allowed_worker_job_types() == ["search_all_queries", "export_final_assets"]


def test_allowed_worker_job_types_all_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "worker_job_types", None)

    assert allowed_worker_job_types() is None
