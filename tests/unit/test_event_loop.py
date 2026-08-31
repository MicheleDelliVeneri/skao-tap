"""Regression checks for synchronous work escaping the ASGI event loop."""

import asyncio
import threading
from typing import cast

from fastapi import Request


def test_concurrent_phase_reads_leave_the_event_loop(monkeypatch):
    from egernia_api.queries import jobs

    started = 0
    lock = threading.Lock()
    all_started = threading.Event()

    def fetch_job(job_id):
        nonlocal started
        with lock:
            started += 1
            if started == 10:
                all_started.set()
        assert all_started.wait(1), "phase reads ran serially on the event loop"
        return {"job_id": job_id, "phase": "COMPLETED"}

    monkeypatch.setattr(jobs, "fetch_job", fetch_job)

    async def read_all():
        await asyncio.gather(*(jobs.wait_for_phase(str(i), 0) for i in range(10)))

    asyncio.run(read_all())


def test_remote_upload_resolution_leaves_the_event_loop(monkeypatch):
    from egernia_api.queries import uploads

    event_loop_thread = threading.get_ident()
    resolver_thread = None

    async def gather_upload_files(request):
        return {}

    def resolve_upload_sources(upload_param, files):
        nonlocal resolver_thread
        resolver_thread = threading.get_ident()
        return {}

    monkeypatch.setattr(uploads, "gather_upload_files", gather_upload_files)
    monkeypatch.setattr(uploads, "resolve_upload_sources", resolve_upload_sources)
    asyncio.run(uploads.gather_upload_sources(cast(Request, object()), {}))

    assert resolver_thread != event_loop_thread
