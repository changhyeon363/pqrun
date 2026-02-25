"""
FastAPI example with pqrun integration.

This example shows:
- Basic setup with FastAPI
- Handler definition
- Enqueue endpoint
- Handler chaining
- Using store.transaction() for DB operations
"""

import os

from fastapi import FastAPI

from pqrun import JobContext, PgJobStore, Worker

# ============================================================================
# Configuration
# ============================================================================

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/mydb")

# ============================================================================
# Job Handlers
# ============================================================================


async def summarize_handler(ctx: JobContext) -> dict:
    """
    Summarize a conversation.

    This handler:
    1. Fetches conversation messages from DB
    2. Calls LLM API (simulated)
    3. Saves summary to DB
    4. Enqueues follow-up embedding job
    """
    conversation_id = ctx.job.payload["conversation_id"]

    # Simulate: Fetch messages
    # In real code, use ctx.store.connection() to query your app tables
    async with ctx.store.connection() as conn:
        messages = await conn.fetch(
            "SELECT content FROM messages WHERE conversation_id = $1 ORDER BY created_at", conversation_id
        )

    # Simulate: Call LLM
    text = "\n".join([msg["content"] for msg in messages])
    summary = f"Summary of {len(messages)} messages: {text[:100]}..."

    # Save summary to DB
    async with ctx.store.transaction() as conn:
        result = await conn.fetchrow(
            """
            INSERT INTO summaries (conversation_id, content, created_at)
            VALUES ($1, $2, now())
            RETURNING id
            """,
            conversation_id,
            summary,
        )
        summary_id = result["id"]

    # Chain: Enqueue embedding job
    await ctx.store.enqueue(
        job_type="embed",
        payload={"summary_id": summary_id},
        dedupe_key=f"embed:summary:{summary_id}",
    )

    return {"summary_id": summary_id, "length": len(summary)}


async def embed_handler(ctx: JobContext) -> dict:
    """
    Generate embeddings for a summary.

    This handler:
    1. Fetches summary text
    2. Calls embedding API (simulated)
    3. Stores vector in DB
    """
    summary_id = ctx.job.payload["summary_id"]

    # Fetch summary
    async with ctx.store.connection() as conn:
        row = await conn.fetchrow("SELECT content FROM summaries WHERE id = $1", summary_id)
        if not row:
            raise ValueError(f"Summary {summary_id} not found")
        content = row["content"]

    # Simulate: Call embedding API
    embedding = [0.1] * 1536  # Fake embedding vector

    # Store embedding
    async with ctx.store.transaction() as conn:
        await conn.execute(
            "UPDATE summaries SET embedding = $1, updated_at = now() WHERE id = $2", embedding, summary_id
        )

    return {"summary_id": summary_id, "dimensions": len(embedding)}


async def cleanup_handler(ctx: JobContext) -> dict:
    """
    Clean up old completed jobs.

    This can be enqueued periodically (e.g., by pg_cron).
    """
    retention_days = ctx.job.payload.get("retention_days", 7)

    async with ctx.store.connection() as conn:
        result = await conn.execute(
            """
            DELETE FROM jobs
            WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
              AND finished_at < now() - $1::interval
            """,
            f"{retention_days} days",
        )
        # Parse "DELETE N" result
        count = int(result.split()[-1]) if result.startswith("DELETE") else 0

    return {"deleted": count, "retention_days": retention_days}


# ============================================================================
# Setup
# ============================================================================

# Initialize store
store = PgJobStore(dsn=DATABASE_URL)

# Register handlers
handlers = {
    "summarize": summarize_handler,
    "embed": embed_handler,
    "cleanup": cleanup_handler,
}

# Create worker
worker = Worker(
    store=store,
    handlers=handlers,
    concurrency=1,  # Start with 1, increase after separating worker
    enabled=True,  # Can be disabled via WORKER_ENABLED=false
)

# Create FastAPI app
app = FastAPI(
    title="pqrun Example",
    lifespan=worker.lifespan,  # This is the key integration!
)


# ============================================================================
# API Endpoints
# ============================================================================


@app.get("/")
async def root():
    return {"message": "pqrun FastAPI example"}


@app.post("/enqueue/summarize/{conversation_id}")
async def enqueue_summarize(conversation_id: int):
    """
    Enqueue a summarization job.

    Example:
        POST /enqueue/summarize/123
    """
    job_id = await store.enqueue(
        job_type="summarize",
        payload={"conversation_id": conversation_id},
        dedupe_key=f"summarize:conv:{conversation_id}",
        priority=0,
    )

    return {"job_id": job_id, "conversation_id": conversation_id}


@app.post("/enqueue/cleanup")
async def enqueue_cleanup(retention_days: int = 7):
    """
    Enqueue a cleanup job.

    Example:
        POST /enqueue/cleanup?retention_days=30
    """
    job_id = await store.enqueue(
        job_type="cleanup",
        payload={"retention_days": retention_days},
        dedupe_key="cleanup:daily",  # Only one cleanup job at a time
    )

    return {"job_id": job_id, "retention_days": retention_days}


@app.get("/jobs/status")
async def job_status():
    """
    Get job queue status.

    Example:
        GET /jobs/status
    """
    async with store.connection() as conn:
        rows = await conn.fetch(
            """
            SELECT status, count(*) as count
            FROM jobs
            GROUP BY status
            ORDER BY status
            """
        )

    return {row["status"]: row["count"] for row in rows}


# ============================================================================
# Running
# ============================================================================

# To run:
#   1. Apply schema: psql $DATABASE_URL < src/pqrun/ddl.sql
#   2. Install deps: pip install fastapi uvicorn pqrun
#   3. Start server: uvicorn examples.fastapi_example:app --reload
#   4. Test: curl -X POST http://localhost:8000/enqueue/summarize/123

# To disable worker (API-only mode):
#   WORKER_ENABLED=false uvicorn examples.fastapi_example:app

# To run separate worker process:
#   # Terminal 1 (API only)
#   WORKER_ENABLED=false uvicorn examples.fastapi_example:app
#
#   # Terminal 2 (Worker only)
#   WORKER_ENABLED=true WORKER_CONCURRENCY=4 python -m examples.worker_only
