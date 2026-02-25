from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

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
