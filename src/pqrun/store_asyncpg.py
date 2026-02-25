"""PostgreSQL job store implementation using asyncpg."""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit, urlunsplit

import asyncpg

from .models import Job, JobStatus

logger = logging.getLogger("pqrun.store")


def _utcnow() -> datetime:
    """Get current UTC timestamp."""
    return datetime.now(timezone.utc)


def _jsonb_param(value: dict[str, Any] | None) -> str | None:
    """Encode a Python dict to a JSON string for jsonb query params."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("jsonb fields must be JSON objects (dict)")
    return json.dumps(value)


def _jsonb_dict(value: Any) -> dict[str, Any] | None:
    """Decode a jsonb column value into a dict when possible."""
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        decoded = json.loads(value)
        if isinstance(decoded, dict):
            return decoded
    raise TypeError(f"Expected JSON object for jsonb field, got {type(value).__name__}")


def _mask_dsn(dsn: str) -> str:
    """Mask password in DSN before logging."""
    parsed = urlsplit(dsn)
    if "@" not in parsed.netloc:
        return dsn
    userinfo, hostinfo = parsed.netloc.rsplit("@", 1)
    if ":" in userinfo:
        user, _ = userinfo.split(":", 1)
        masked_netloc = f"{user}:***@{hostinfo}"
    else:
        masked_netloc = parsed.netloc
    return urlunsplit((parsed.scheme, masked_netloc, parsed.path, parsed.query, parsed.fragment))


@dataclass
class PgJobStore:
    """
    PostgreSQL-backed job queue store.

    Supports two initialization modes:
    1. Provide dsn → store creates and manages its own connection pool
    2. Provide pool → store uses existing connection pool

    Usage:
        # Auto pool creation
        store = PgJobStore(dsn="postgresql://user:pass@host/db")
        await store.start()

        # Use existing pool
        store = PgJobStore(pool=my_pool)
        await store.start()  # no-op if pool already provided

    Thread-safety: asyncpg pools are thread-safe, but handlers should use
    asyncio primitives (not thread-based).
    """

    dsn: Optional[str] = None
    pool: Optional[asyncpg.Pool] = None
    _owns_pool: bool = False

    def _require_pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("call start() first")
        return self.pool

    async def start(self) -> None:
        """
        Initialize the connection pool if needed.

        If pool is already provided, this is a no-op.
        If dsn is provided, creates a new pool (min_size=1, max_size=10).
        """
        if self.pool is not None:
            logger.debug("PgJobStore using existing pool")
            return

        if not self.dsn:
            raise ValueError("PgJobStore requires either dsn or pool")

        logger.info(f"Creating connection pool for {_mask_dsn(self.dsn)}")
        self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)
        self._owns_pool = True

    async def close(self) -> None:
        """
        Close the connection pool if it was created by this store.

        If an external pool was provided, this is a no-op.
        """
        if self.pool is not None and self._owns_pool:
            logger.info("Closing connection pool")
            await self.pool.close()
            self.pool = None

    # ---- Connection helpers ----

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        """
        Get a connection from the pool.

        Usage:
            async with store.connection() as conn:
                await conn.execute("SELECT ...")
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        """
        Get a transactional connection.

        Usage:
            async with store.transaction() as conn:
                await conn.execute("UPDATE ...")
                await conn.execute("INSERT ...")
            # Auto-commit on success, rollback on exception
        """
        pool = self._require_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ---- Job operations ----

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        priority: int = 0,
        max_attempts: int = 5,
        timeout_seconds: int | None = None,
    ) -> int:
        """
        Enqueue a job.

        Args:
            job_type: Handler routing key
            payload: Job input data (must be JSON-serializable)
            dedupe_key: Optional deduplication key (unique among READY/RUNNING jobs)
            run_after: Earliest execution time (default: now)
            priority: Higher values are picked first (default: 0)
            max_attempts: Maximum retry limit (default: 5)
            timeout_seconds: Per-job stale timeout override

        Returns:
            job_id of inserted job, or existing job_id if dedupe_key matched

        Note:
            If dedupe_key conflicts with an active job, the existing job's
            updated_at is touched and its ID is returned.
        """
        pool = self._require_pool()
        ra = run_after or _utcnow()

        sql = """
        INSERT INTO jobs (job_type, payload, dedupe_key, run_after, priority, max_attempts, timeout_seconds)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7)
        ON CONFLICT (dedupe_key)
        WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
        DO UPDATE SET updated_at = now()
        RETURNING id;
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                sql, job_type, _jsonb_param(payload), dedupe_key, ra, priority, max_attempts, timeout_seconds
            )
            job_id = int(row["id"]) if row else 0
            if job_id:
                logger.debug(f"Enqueued job {job_id} type={job_type} dedupe={dedupe_key}")
            return job_id

    async def pickup(self, *, worker_id: str) -> Job | None:
        """
        Atomically pick a READY job and mark it RUNNING.

        Uses FOR UPDATE SKIP LOCKED for safe multi-worker concurrency.

        Args:
            worker_id: Identifier of the worker picking this job

        Returns:
            Job instance if one was available, None otherwise

        Note:
            - Increments attempts
            - Sets locked_at/locked_by
            - Clears finished_at/duration_ms from previous runs
        """
        pool = self._require_pool()

        sql = """
        WITH picked AS (
          SELECT id
          FROM jobs
          WHERE status='READY'
            AND run_after <= now()
          ORDER BY priority DESC, id
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        UPDATE jobs j
        SET status='RUNNING',
            locked_at=now(),
            locked_by=$1,
            attempts = attempts + 1,
            updated_at=now(),
            finished_at=NULL,
            duration_ms=NULL
        FROM picked
        WHERE j.id = picked.id
        RETURNING
          j.id, j.job_type, j.payload, j.status, j.priority, j.attempts, j.max_attempts,
          j.run_after, j.timeout_seconds, j.locked_at, j.locked_by, j.dedupe_key, j.last_error,
          j.finished_at, j.duration_ms, j.result, j.created_at, j.updated_at;
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, worker_id)
            if not row:
                return None
            job = _row_to_job(row)
            logger.debug(f"Picked job {job.id} type={job.job_type} attempt={job.attempts}")
            return job

    async def mark_done(
        self, job_id: int, *, result: dict[str, Any] | None = None, duration_ms: int | None = None
    ) -> None:
        """
        Mark a job as successfully completed.

        Args:
            job_id: Job to mark done
            result: Optional handler return value (stored in jobs.result)
            duration_ms: Execution duration in milliseconds
        """
        pool = self._require_pool()
        sql = """
        UPDATE jobs
        SET status='DONE',
            finished_at=now(),
            duration_ms=COALESCE($2, duration_ms),
            result=COALESCE($3::jsonb, result),
            updated_at=now(),
            locked_at=NULL,
            locked_by=NULL
        WHERE id=$1;
        """
        async with pool.acquire() as conn:
            await conn.execute(sql, job_id, duration_ms, _jsonb_param(result))
            logger.info(f"Job {job_id} completed (duration={duration_ms}ms)")

    async def mark_error(
        self,
        job_id: int,
        error: str,
        *,
        retry_after: timedelta | None = None,
        terminal: bool = False,
        duration_ms: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        """
        Mark a job as failed.

        Behavior:
        - If terminal=True → status=FAILED immediately
        - Elif attempts >= max_attempts → status=FAILED
        - Else → status=READY, run_after=now()+retry_after

        Args:
            job_id: Job to mark failed
            error: Error message (traceback recommended)
            retry_after: Delay before retry (default: 1 minute)
            terminal: If True, skip retry logic and fail immediately
            duration_ms: Execution duration before error
            result: Optional partial result
        """
        pool = self._require_pool()
        delay = retry_after if retry_after is not None else timedelta(minutes=1)

        sql = """
        UPDATE jobs
        SET
          last_error = $2,
          updated_at = now(),
          duration_ms = COALESCE($5, duration_ms),
          result = COALESCE($6::jsonb, result),

          status = CASE
            WHEN $3::bool THEN 'FAILED'::job_status
            WHEN attempts >= max_attempts THEN 'FAILED'::job_status
            ELSE 'READY'::job_status
          END,

          finished_at = CASE
            WHEN $3::bool THEN now()
            WHEN attempts >= max_attempts THEN now()
            ELSE NULL
          END,

          run_after = CASE
            WHEN $3::bool THEN run_after
            WHEN attempts >= max_attempts THEN run_after
            ELSE now() + $4::interval
          END,

          locked_at = NULL,
          locked_by = NULL
        WHERE id = $1
        RETURNING status, attempts, max_attempts;
        """
        async with pool.acquire() as conn:
            row = await conn.fetchrow(sql, job_id, error, terminal, delay, duration_ms, _jsonb_param(result))
            if row:
                logger.warning(
                    f"Job {job_id} failed (terminal={terminal}, "
                    f"will_retry={not terminal and row['attempts'] < row['max_attempts']})"
                )

    async def cancel(self, job_id: int) -> None:
        """
        Cancel a job (sets status=CANCELLED).

        Args:
            job_id: Job to cancel
        """
        pool = self._require_pool()
        sql = "UPDATE jobs SET status='CANCELLED', finished_at=now(), updated_at=now() WHERE id=$1;"
        async with pool.acquire() as conn:
            await conn.execute(sql, job_id)
            logger.info(f"Job {job_id} cancelled")

    async def reap_stale(self, *, default_stale_after: timedelta) -> int:
        """
        Reset stale RUNNING jobs back to READY.

        A job is considered stale if:
          locked_at < now() - timeout

        Where timeout is:
          - jobs.timeout_seconds (if set), OR
          - default_stale_after (worker config)

        Args:
            default_stale_after: Default stale timeout for jobs without timeout_seconds

        Returns:
            Number of jobs reaped
        """
        pool = self._require_pool()
        default_seconds = int(default_stale_after.total_seconds())

        sql = f"""
        UPDATE jobs
        SET status='READY',
            locked_at=NULL,
            locked_by=NULL,
            updated_at=now(),
            run_after = GREATEST(run_after, now())
        WHERE status='RUNNING'
          AND locked_at IS NOT NULL
          AND locked_at < now() - make_interval(secs => COALESCE(timeout_seconds, {default_seconds}))
        RETURNING id;
        """
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql)
            count = len(rows)
            if count > 0:
                logger.warning(f"Reaped {count} stale jobs: {[r['id'] for r in rows]}")
            return count


def _row_to_job(row: asyncpg.Record) -> Job:
    """Convert a database row to a Job instance."""
    return Job(
        id=int(row["id"]),
        job_type=str(row["job_type"]),
        payload=_jsonb_dict(row["payload"]) or {},
        status=JobStatus(str(row["status"])),
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        run_after=row["run_after"],
        timeout_seconds=row["timeout_seconds"],
        locked_at=row["locked_at"],
        locked_by=row["locked_by"],
        dedupe_key=row["dedupe_key"],
        last_error=row["last_error"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        result=_jsonb_dict(row["result"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
