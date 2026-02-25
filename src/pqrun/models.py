"""Core data models for pqrun."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Optional


class JobStatus(str, Enum):
    """Job lifecycle states."""

    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True)
class Job:
    """
    Represents a job in the queue.

    Fields:
        id: Unique job identifier
        job_type: Handler routing key
        payload: Job input data (JSON-serializable dict)
        status: Current lifecycle state
        priority: Higher values are picked first
        attempts: Number of execution attempts (including current)
        max_attempts: Maximum retry limit
        run_after: Earliest execution time (for scheduling/backoff)
        timeout_seconds: Per-job stale timeout (overrides worker default)
        locked_at: When the job was picked by a worker
        locked_by: Worker ID that owns this job
        dedupe_key: Deduplication key (unique among active jobs)
        last_error: Last error message (traceback)
        finished_at: When the job completed (DONE/FAILED)
        duration_ms: Execution duration in milliseconds
        result: Handler return value (optional)
        created_at: Job creation timestamp
        updated_at: Last modification timestamp
    """

    id: int
    job_type: str
    payload: dict[str, Any]
    status: JobStatus

    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime

    timeout_seconds: Optional[int]

    locked_at: Optional[datetime]
    locked_by: Optional[str]

    dedupe_key: Optional[str]
    last_error: Optional[str]

    finished_at: Optional[datetime]
    duration_ms: Optional[int]
    result: Optional[dict[str, Any]]

    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JobContext:
    """
    Context passed to job handlers.

    Provides access to:
    - job: The job being executed
    - store: JobStore for enqueuing follow-up jobs or accessing DB
    - worker_id: Identifier of the worker executing this job
    """

    job: Job
    store: Any  # PgJobStore, avoid circular import
    worker_id: str


# Type aliases
Handler = Callable[[JobContext], Awaitable[dict[str, Any] | None]]
"""
Handler function signature.

Handlers should:
- Accept a JobContext
- Return a dict (stored in jobs.result) or None
- Raise exceptions for errors (Worker handles retry logic)
- Be idempotent when possible (at-least-once delivery)

Example:
    async def my_handler(ctx: JobContext) -> dict | None:
        conversation_id = ctx.job.payload["conversation_id"]
        # ... do work ...
        return {"summary_id": 123, "tokens": 456}
"""

Handlers = Mapping[str, Handler]
"""Registry of job_type -> handler mappings."""
