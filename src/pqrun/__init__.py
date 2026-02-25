"""
pqrun - PostgreSQL-backed job queue for async Python.

A simple, reliable job queue built on PostgreSQL for FastAPI and async applications.

Key Features:
- Safe concurrent job processing (SKIP LOCKED)
- At-least-once delivery with configurable retries
- FastAPI native integration (lifespan)
- No additional infrastructure (uses PostgreSQL you already have)
- Flexible enqueue patterns (app, pg_cron, handler chains)

Basic Usage:
    from pqrun import PgJobStore, Worker, JobContext

    # Define handler
    async def summarize(ctx: JobContext) -> dict:
        conversation_id = ctx.job.payload["conversation_id"]
        # ... do work ...
        return {"summary_id": 123}

    # Setup
    store = PgJobStore(dsn="postgresql://...")
    worker = Worker(store=store, handlers={"summarize": summarize})

    # FastAPI integration
    app = FastAPI(lifespan=worker.lifespan)

    # Enqueue jobs
    @app.post("/enqueue")
    async def enqueue():
        await store.enqueue("summarize", {"conversation_id": 456})
"""

from .backoff import BackoffPolicy, IdlePollPolicy, LoopErrorPolicy
from .models import Handler, Handlers, Job, JobContext, JobStatus
from .store_asyncpg import PgJobStore
from .worker import Worker

__version__ = "0.0.1"

__all__ = [
    # Core
    "PgJobStore",
    "Worker",
    # Models
    "Job",
    "JobStatus",
    "JobContext",
    "Handler",
    "Handlers",
    # Policies
    "BackoffPolicy",
    "IdlePollPolicy",
    "LoopErrorPolicy",
]
