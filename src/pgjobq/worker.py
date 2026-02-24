"""Worker for consuming and executing jobs."""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import traceback
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from .backoff import BackoffPolicy, IdlePollPolicy, LoopErrorPolicy
from .models import Handlers, JobContext
from .store_asyncpg import PgJobStore

logger = logging.getLogger("pgjobq.worker")


class TerminalDispatchError(Exception):
    """Raised when dispatch already handled a terminal failure."""


@dataclass
class Worker:
    """
    Job queue worker.

    Responsibilities:
    - Poll for READY jobs
    - Dispatch to registered handlers
    - Track execution time
    - Apply retry policy on errors
    - Reap stale jobs periodically

    Usage:
        store = PgJobStore(dsn="postgresql://...")
        worker = Worker(
            store=store,
            handlers={"summarize": summarize_handler}
        )

        app = FastAPI(lifespan=worker.lifespan)

    Configuration:
    - concurrency: Max concurrent jobs per worker instance (default: 1)
    - enabled: Whether to run worker loop (default: True, overridden by WORKER_ENABLED env)
    - idle_policy: Sleep strategy when no jobs available
    - backoff: Retry delay policy
    - reap_stale_every_seconds: How often to run stale job recovery
    - default_stale_after: Default timeout for jobs without timeout_seconds
    - worker_id: Unique worker identifier (default: hostname-pid)
    """

    store: PgJobStore
    handlers: Handlers

    concurrency: int = 1
    enabled: bool = True

    idle_policy: IdlePollPolicy = field(default_factory=IdlePollPolicy)
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    loop_error_policy: LoopErrorPolicy = field(default_factory=LoopErrorPolicy)

    reap_stale_every_seconds: int = 60
    default_stale_after: timedelta = field(default_factory=lambda: timedelta(minutes=20))

    worker_id: Optional[str] = None

    def __post_init__(self) -> None:
        """Initialize worker_id and apply environment variable overrides."""
        if self.worker_id is None:
            self.worker_id = f"{socket.gethostname()}-{os.getpid()}"

        # Environment variable overrides
        env_enabled = os.getenv("WORKER_ENABLED")
        if env_enabled is not None:
            self.enabled = env_enabled.lower() in ("1", "true", "yes", "on")
            logger.info(f"WORKER_ENABLED={self.enabled} (from env)")

        env_conc = os.getenv("WORKER_CONCURRENCY")
        if env_conc is not None:
            try:
                self.concurrency = max(1, int(env_conc))
                logger.info(f"WORKER_CONCURRENCY={self.concurrency} (from env)")
            except (TypeError, ValueError):
                logger.warning(f"Invalid WORKER_CONCURRENCY={env_conc!r}; using {self.concurrency}")

        env_reap = os.getenv("WORKER_REAP_INTERVAL")
        if env_reap is not None:
            try:
                self.reap_stale_every_seconds = max(1, int(env_reap))
                logger.info(f"WORKER_REAP_INTERVAL={self.reap_stale_every_seconds} (from env)")
            except (TypeError, ValueError):
                logger.warning(f"Invalid WORKER_REAP_INTERVAL={env_reap!r}; using {self.reap_stale_every_seconds}")

        env_stale = os.getenv("WORKER_STALE_TIMEOUT")
        if env_stale is not None:
            try:
                stale_seconds = max(1, int(env_stale))
                self.default_stale_after = timedelta(seconds=stale_seconds)
                logger.info(f"WORKER_STALE_TIMEOUT={stale_seconds}s (from env)")
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid WORKER_STALE_TIMEOUT=%r; using %ss",
                    env_stale,
                    int(self.default_stale_after.total_seconds()),
                )

    async def lifespan(self, app):
        """
        FastAPI lifespan context manager.

        Usage:
            worker = Worker(store, handlers)
            app = FastAPI(lifespan=worker.lifespan)

        Lifecycle:
        1. Startup: Initialize store, start worker loops and reaper
        2. Yield: Application runs
        3. Shutdown: Stop loops gracefully, close store
        """
        logger.info(f"Worker starting (id={self.worker_id}, enabled={self.enabled}, concurrency={self.concurrency})")
        await self.store.start()

        stop_event = asyncio.Event()
        tasks: list[asyncio.Task] = []

        if self.enabled:
            # Spawn worker loops
            for i in range(self.concurrency):
                task = asyncio.create_task(self._run_loop(stop_event), name=f"worker-{i}")
                tasks.append(task)

            # Spawn reaper loop
            reaper_task = asyncio.create_task(self._reaper_loop(stop_event), name="reaper")
            tasks.append(reaper_task)

            logger.info(f"Started {self.concurrency} worker loops + 1 reaper")

        try:
            yield
        finally:
            logger.info("Worker shutting down...")
            stop_event.set()

            # Cancel all tasks
            for t in tasks:
                t.cancel()

            # Wait for graceful shutdown (with timeout)
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30.0)
            except asyncio.TimeoutError:
                logger.warning("Worker shutdown timed out after 30s")

            await self.store.close()
            logger.info("Worker stopped")

    async def _reaper_loop(self, stop_event: asyncio.Event) -> None:
        """
        Background loop to reap stale RUNNING jobs.

        Runs every `reap_stale_every_seconds` and resets jobs that have
        been RUNNING longer than their timeout.
        """
        try:
            while not stop_event.is_set():
                await asyncio.sleep(self.reap_stale_every_seconds)
                try:
                    count = await self.store.reap_stale(default_stale_after=self.default_stale_after)
                    if count > 0:
                        logger.warning(f"Reaper recovered {count} stale jobs")
                except Exception as e:
                    logger.error(f"Reaper error: {e}", exc_info=True)
        except asyncio.CancelledError:
            logger.debug("Reaper loop cancelled")
            return

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        """
        Main worker loop.

        Flow:
        1. pickup() a job
        2. If found: dispatch to handler, measure duration, mark done/error
        3. If not found: increment empty_streak, backoff sleep
        4. Repeat until stop_event is set
        """
        empty_streak = 0
        consecutive_loop_errors = 0
        try:
            while not stop_event.is_set():
                try:
                    job = await self.store.pickup(worker_id=self.worker_id or "worker")

                    if job is None:
                        empty_streak += 1
                        sleep_duration = self.idle_policy.next_sleep(empty_streak)
                        await asyncio.sleep(sleep_duration)
                        consecutive_loop_errors = 0
                        continue

                    # Job found, reset idle backoff
                    empty_streak = 0

                    # Execute and measure
                    started = time.perf_counter()
                    try:
                        result = await self._dispatch(job)
                        duration_ms = int((time.perf_counter() - started) * 1000)
                        await self.store.mark_done(job.id, result=result, duration_ms=duration_ms)
                    except TerminalDispatchError as e:
                        logger.error(f"Job {job.id} terminal dispatch failure: {e}")
                    except Exception as e:
                        duration_ms = int((time.perf_counter() - started) * 1000)
                        error_msg = traceback.format_exc()[:10000]  # Limit to 10KB
                        delay = self.backoff.retry_delay(job.attempts)
                        await self.store.mark_error(
                            job.id, error=error_msg, retry_after=delay, terminal=False, duration_ms=duration_ms
                        )
                        logger.error(f"Job {job.id} failed (attempt {job.attempts}): {e}")

                    consecutive_loop_errors = 0
                    # Small yield to avoid tight loop
                    await asyncio.sleep(0)
                except Exception as e:
                    consecutive_loop_errors += 1
                    sleep_duration = max(0.0, self.loop_error_policy.next_sleep(consecutive_loop_errors))
                    logger.error(
                        "Worker loop infra error (consecutive=%s, retry_in=%.3fs): %s",
                        consecutive_loop_errors,
                        sleep_duration,
                        e,
                        exc_info=True,
                    )
                    await asyncio.sleep(sleep_duration)

        except asyncio.CancelledError:
            logger.debug("Worker loop cancelled")
            return

    async def _dispatch(self, job) -> dict | None:
        """
        Dispatch job to registered handler.

        Args:
            job: Job to execute

        Returns:
            Handler return value (dict or None)

        Raises:
            Exception: Handler raised an error (caller handles retry logic)
        """
        handler = self.handlers.get(job.job_type)
        if handler is None:
            error_msg = f"No handler registered for job_type={job.job_type}"
            logger.error(error_msg)
            await self.store.mark_error(job.id, error=error_msg, terminal=True)
            raise TerminalDispatchError(error_msg)

        ctx = JobContext(job=job, store=self.store, worker_id=self.worker_id or "worker")
        logger.info(f"Executing job {job.id} type={job.job_type} (attempt {job.attempts})")

        result = await handler(ctx)
        return result
