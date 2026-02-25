from __future__ import annotations

import asyncio
import contextlib
import json
import time

import httpx
import pytest
from fastapi import FastAPI

from pqrun import IdlePollPolicy, JobContext, Worker


@pytest.mark.integration
@pytest.mark.fastapi
@pytest.mark.asyncio
async def test_fastapi_worker_lifespan_smoke(store_with_pool):
    store = store_with_pool

    async def demo_handler(ctx: JobContext) -> dict:
        return {"handled": True, "job_id": ctx.job.id}

    worker = Worker(
        store=store,
        handlers={"smoke.fastapi": demo_handler},
        concurrency=1,
        idle_policy=IdlePollPolicy(base_seconds=0.01, max_seconds=0.05),
        reap_stale_every_seconds=3600,
    )

    app = FastAPI(lifespan=worker.lifespan)

    @app.post("/enqueue")
    async def enqueue() -> dict:
        job_id = await store.enqueue("smoke.fastapi", {"message": "hello"})
        return {"job_id": job_id}

    @app.get("/jobs/{job_id}")
    async def get_job(job_id: int) -> dict:
        async with store.connection() as conn:
            row = await conn.fetchrow("SELECT status, result FROM jobs WHERE id=$1", job_id)

        if not row:
            return {"status": "MISSING"}

        if row["result"] is None:
            result = None
        elif isinstance(row["result"], dict):
            result = row["result"]
        else:
            result = json.loads(row["result"])
        return {"status": str(row["status"]), "result": result}

    transport = httpx.ASGITransport(app=app)
    lifespan_cm = contextlib.asynccontextmanager(worker.lifespan)

    async with lifespan_cm(app):
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post("/enqueue")
            assert response.status_code == 200
            job_id = response.json()["job_id"]

            deadline = time.time() + 10
            while time.time() < deadline:
                job_resp = await client.get(f"/jobs/{job_id}")
                body = job_resp.json()
                if body.get("status") == "DONE":
                    assert body["result"]["handled"] is True
                    assert body["result"]["job_id"] == job_id
                    return
                await asyncio.sleep(0.05)

    raise AssertionError("job was not completed within timeout")
