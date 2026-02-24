from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_enqueue_pickup_done(store_with_pool):
    store = store_with_pool

    job_id = await store.enqueue(
        job_type="it.enqueue_done",
        payload={"x": 1},
        priority=10,
        dedupe_key=None,
    )

    job = await store.pickup(worker_id="itest-worker")
    assert job is not None
    assert job.id == job_id
    assert job.job_type == "it.enqueue_done"

    await store.mark_done(job.id, result={"ok": True}, duration_ms=12)

    async with store.connection() as conn:
        row = await conn.fetchrow("SELECT status, result, duration_ms FROM jobs WHERE id=$1", job.id)

    assert str(row["status"]) == "DONE"
    result = row["result"] if isinstance(row["result"], dict) else json.loads(row["result"])
    assert result == {"ok": True}
    assert row["duration_ms"] == 12


@pytest.mark.integration
@pytest.mark.asyncio
async def test_store_retry_then_fail(store_with_pool):
    store = store_with_pool

    job_id = await store.enqueue(
        job_type="it.retry_fail",
        payload={"x": 2},
        max_attempts=2,
    )

    first = await store.pickup(worker_id="itest-worker")
    assert first is not None
    assert first.id == job_id
    assert first.attempts == 1

    await store.mark_error(
        job_id,
        "first failure",
        retry_after=timedelta(seconds=0),
        terminal=False,
    )

    second = None
    for _ in range(20):
        second = await store.pickup(worker_id="itest-worker")
        if second is not None:
            break
        await asyncio.sleep(0.05)

    assert second is not None
    assert second.id == job_id
    assert second.attempts == 2

    await store.mark_error(
        job_id,
        "second failure",
        retry_after=timedelta(seconds=0),
        terminal=False,
    )

    async with store.connection() as conn:
        row = await conn.fetchrow("SELECT status, attempts, max_attempts, last_error FROM jobs WHERE id=$1", job_id)

    assert str(row["status"]) == "FAILED"
    assert row["attempts"] == 2
    assert row["max_attempts"] == 2
    assert "second failure" in row["last_error"]
