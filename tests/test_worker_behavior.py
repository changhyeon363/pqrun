from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI

from pqrun.backoff import IdlePollPolicy, LoopErrorPolicy
from pqrun.models import Job, JobStatus
from pqrun.worker import Worker


class _StoreUnknownHandler:
    def __init__(self, stop_event: asyncio.Event):
        self._stop_event = stop_event
        self.mark_done_called = False
        self.mark_error_calls: list[dict] = []
        self._picked = False

    async def start(self):
        return None

    async def close(self):
        return None

    async def pickup(self, *, worker_id: str):
        if self._picked:
            await asyncio.sleep(0.01)
            return None
        self._picked = True
        now = datetime.now(timezone.utc)
        return Job(
            id=1,
            job_type="missing.handler",
            payload={},
            status=JobStatus.RUNNING,
            priority=0,
            attempts=1,
            max_attempts=5,
            run_after=now,
            timeout_seconds=None,
            locked_at=now,
            locked_by=worker_id,
            dedupe_key=None,
            last_error=None,
            finished_at=None,
            duration_ms=None,
            result=None,
            created_at=now,
            updated_at=now,
        )

    async def mark_done(self, *args, **kwargs):
        self.mark_done_called = True

    async def mark_error(self, job_id: int, error: str, **kwargs):
        self.mark_error_calls.append({"job_id": job_id, "error": error, **kwargs})
        self._stop_event.set()


class _DummyStore:
    async def start(self):
        return None

    async def close(self):
        return None


class _StoreOneJob:
    def __init__(self):
        self.mark_done_called = False
        self._picked = False

    async def start(self):
        return None

    async def close(self):
        return None

    async def pickup(self, *, worker_id: str):
        if self._picked:
            await asyncio.sleep(0.01)
            return None
        self._picked = True
        now = datetime.now(timezone.utc)
        return Job(
            id=1,
            job_type="one.job",
            payload={},
            status=JobStatus.RUNNING,
            priority=0,
            attempts=1,
            max_attempts=5,
            run_after=now,
            timeout_seconds=None,
            locked_at=now,
            locked_by=worker_id,
            dedupe_key=None,
            last_error=None,
            finished_at=None,
            duration_ms=None,
            result=None,
            created_at=now,
            updated_at=now,
        )

    async def mark_done(self, *args, **kwargs):
        self.mark_done_called = True

    async def mark_error(self, *args, **kwargs):
        raise AssertionError("unexpected mark_error call")


class _StoreFlakyPickup:
    def __init__(self, stop_event: asyncio.Event):
        self._stop_event = stop_event
        self.pickup_calls = 0

    async def start(self):
        return None

    async def close(self):
        return None

    async def pickup(self, *, worker_id: str):
        self.pickup_calls += 1
        if self.pickup_calls == 1:
            raise RuntimeError("temporary infra issue")
        self._stop_event.set()
        return None


class _RecordingLoopErrorPolicy(LoopErrorPolicy):
    def __init__(self):
        self.calls: list[int] = []

    def next_sleep(self, consecutive_errors: int) -> float:
        self.calls.append(consecutive_errors)
        return 0.0


@pytest.mark.asyncio
async def test_unknown_handler_stays_failed_and_not_done():
    stop_event = asyncio.Event()
    store = _StoreUnknownHandler(stop_event)
    worker = Worker(store=store, handlers={})

    await worker._run_loop(stop_event)

    assert store.mark_done_called is False
    assert len(store.mark_error_calls) == 1
    assert store.mark_error_calls[0]["terminal"] is True
    assert "No handler registered" in store.mark_error_calls[0]["error"]


@pytest.mark.asyncio
async def test_worker_loop_infra_error_retries_with_custom_policy():
    stop_event = asyncio.Event()
    store = _StoreFlakyPickup(stop_event)
    policy = _RecordingLoopErrorPolicy()
    worker = Worker(
        store=store,
        handlers={},
        loop_error_policy=policy,
        idle_policy=IdlePollPolicy(base_seconds=0.01, max_seconds=0.01),
    )

    await asyncio.wait_for(worker._run_loop(stop_event), timeout=1.0)

    assert store.pickup_calls >= 2
    assert policy.calls == [1]


@pytest.mark.asyncio
async def test_shutdown_grace_waits_for_inflight_job_completion():
    store = _StoreOneJob()
    handler_started = asyncio.Event()
    handler_can_finish = asyncio.Event()
    handler_cancelled = False

    async def handler(ctx: JobContext) -> dict:
        nonlocal handler_cancelled
        handler_started.set()
        try:
            await handler_can_finish.wait()
        except asyncio.CancelledError:
            handler_cancelled = True
            raise
        return {"ok": True}

    worker = Worker(
        store=store,
        handlers={"one.job": handler},
        concurrency=1,
        idle_policy=IdlePollPolicy(base_seconds=0.01, max_seconds=0.01),
        reap_stale_every_seconds=3600,
        shutdown_grace=timedelta(seconds=1),
        shutdown_timeout=timedelta(seconds=2),
    )

    lifespan_cm = contextlib.asynccontextmanager(worker.lifespan)
    cm = lifespan_cm(None)
    await cm.__aenter__()
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)

    shutdown_task = asyncio.create_task(cm.__aexit__(None, None, None))
    await asyncio.sleep(0.05)
    handler_can_finish.set()

    await asyncio.wait_for(shutdown_task, timeout=1.0)
    assert handler_cancelled is False
    assert store.mark_done_called is True


@pytest.mark.asyncio
async def test_shutdown_grace_expires_and_cancels_inflight_job():
    store = _StoreOneJob()
    handler_started = asyncio.Event()
    handler_cancelled = False

    async def handler(ctx) -> dict:
        nonlocal handler_cancelled
        handler_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            handler_cancelled = True
            raise

    worker = Worker(
        store=store,
        handlers={"one.job": handler},
        concurrency=1,
        idle_policy=IdlePollPolicy(base_seconds=0.01, max_seconds=0.01),
        reap_stale_every_seconds=3600,
        shutdown_grace=timedelta(seconds=0.2),
        shutdown_timeout=timedelta(seconds=1),
    )

    lifespan_cm = contextlib.asynccontextmanager(worker.lifespan)
    cm = lifespan_cm(None)
    await cm.__aenter__()
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)

    started = time.monotonic()
    await asyncio.wait_for(cm.__aexit__(None, None, None), timeout=2.0)
    elapsed = time.monotonic() - started

    assert handler_cancelled is True
    assert store.mark_done_called is False
    assert elapsed >= 0.18


@pytest.mark.asyncio
async def test_fastapi_lifespan_shutdown_waits_for_graceful_completion():
    store = _StoreOneJob()
    handler_started = asyncio.Event()
    handler_can_finish = asyncio.Event()
    handler_cancelled = False

    async def handler(ctx) -> dict:
        nonlocal handler_cancelled
        handler_started.set()
        try:
            await handler_can_finish.wait()
        except asyncio.CancelledError:
            handler_cancelled = True
            raise
        return {"ok": True}

    worker = Worker(
        store=store,
        handlers={"one.job": handler},
        concurrency=1,
        idle_policy=IdlePollPolicy(base_seconds=0.01, max_seconds=0.01),
        reap_stale_every_seconds=3600,
        shutdown_grace=timedelta(seconds=0.5),
        shutdown_timeout=timedelta(seconds=1),
    )
    app = FastAPI(lifespan=worker.lifespan)

    cm = app.router.lifespan_context(app)
    await cm.__aenter__()
    await asyncio.wait_for(handler_started.wait(), timeout=1.0)

    started = time.monotonic()
    shutdown_task = asyncio.create_task(cm.__aexit__(None, None, None))
    await asyncio.sleep(0.15)
    handler_can_finish.set()
    await asyncio.wait_for(shutdown_task, timeout=2.0)
    elapsed = time.monotonic() - started

    assert handler_cancelled is False
    assert store.mark_done_called is True
    assert elapsed >= 0.15
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_reaper_loop_not_started_when_disabled():
    class _StoreNoJobs:
        async def start(self):
            return None

        async def close(self):
            return None

        async def pickup(self, *, worker_id: str):
            return None

    store = _StoreNoJobs()
    started = asyncio.Event()

    async def fake_reaper(stop_event):
        started.set()
        await asyncio.sleep(0)

    worker = Worker(
        store=store,
        handlers={},
        enabled=True,
        enable_reaper=False,
        idle_policy=IdlePollPolicy(base_seconds=0.01, max_seconds=0.01),
        shutdown_grace=timedelta(seconds=0),
        shutdown_timeout=timedelta(seconds=1),
    )
    worker._reaper_loop = fake_reaper  # type: ignore[method-assign]

    lifespan_cm = contextlib.asynccontextmanager(worker.lifespan)
    async with lifespan_cm(None):
        await asyncio.sleep(0.05)

    assert started.is_set() is False


def test_worker_invalid_env_falls_back_to_defaults(monkeypatch):
    monkeypatch.setenv("WORKER_CONCURRENCY", "not-a-number")
    monkeypatch.setenv("WORKER_REAP_INTERVAL", "bad")
    monkeypatch.setenv("WORKER_STALE_TIMEOUT", "oops")

    worker = Worker(
        store=_DummyStore(),
        handlers={},
    )

    assert worker.concurrency == 1
    assert worker.reap_stale_every_seconds == 60
    assert worker.default_stale_after == timedelta(minutes=20)
