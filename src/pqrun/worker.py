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

logger = logging.getLogger("pqrun.worker")


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
    - enable_reaper: Whether to run stale reaper loop (default: True)
    - worker_id: Unique worker identifier (default: hostname-pid)
    """

    store: PgJobStore
    handlers: Handlers

    concurrency: int = 1
    enabled: bool = True
    enable_reaper: bool = True

    idle_policy: IdlePollPolicy = field(default_factory=IdlePollPolicy)
    backoff: BackoffPolicy = field(default_factory=BackoffPolicy)
    loop_error_policy: LoopErrorPolicy = field(default_factory=LoopErrorPolicy)

    reap_stale_every_seconds: int = 60
    default_stale_after: timedelta = field(default_factory=lambda: timedelta(minutes=20))

    shutdown_grace: timedelta = field(default_factory=lambda: timedelta(seconds=10))
    shutdown_timeout: timedelta = field(default_factory=lambda: timedelta(seconds=30))

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

        env_reaper_enabled = os.getenv("WORKER_REAPER_ENABLED")
        if env_reaper_enabled is not None:
            self.enable_reaper = env_reaper_enabled.lower() in ("1", "true", "yes", "on")
            logger.info(f"WORKER_REAPER_ENABLED={self.enable_reaper} (from env)")

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

        env_shutdown_grace = os.getenv("WORKER_SHUTDOWN_GRACE")
        if env_shutdown_grace is not None:
            try:
                grace_seconds = max(0, int(env_shutdown_grace))
                self.shutdown_grace = timedelta(seconds=grace_seconds)
                logger.info(f"WORKER_SHUTDOWN_GRACE={grace_seconds}s (from env)")
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid WORKER_SHUTDOWN_GRACE=%r; using %ss",
                    env_shutdown_grace,
                    int(self.shutdown_grace.total_seconds()),
                )

        env_shutdown_timeout = os.getenv("WORKER_SHUTDOWN_TIMEOUT")
        if env_shutdown_timeout is not None:
            try:
                timeout_seconds = max(1, int(env_shutdown_timeout))
                self.shutdown_timeout = timedelta(seconds=timeout_seconds)
                logger.info(f"WORKER_SHUTDOWN_TIMEOUT={timeout_seconds}s (from env)")
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid WORKER_SHUTDOWN_TIMEOUT=%r; using %ss",
                    env_shutdown_timeout,
                    int(self.shutdown_timeout.total_seconds()),
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
        worker_tasks: list[asyncio.Task] = []
        reaper_task: asyncio.Task | None = None

        if self.enabled:
            # Spawn worker loops
            for i in range(self.concurrency):
                task = asyncio.create_task(self._run_loop(stop_event), name=f"worker-{i}")
                worker_tasks.append(task)

            if self.enable_reaper:
                reaper_task = asyncio.create_task(self._reaper_loop(stop_event), name="reaper")
                logger.info(f"Started {self.concurrency} worker loops + 1 reaper")
            else:
                logger.info(f"Started {self.concurrency} worker loops (reaper disabled)")

        try:
            yield
        finally:
            await self._shutdown_tasks(stop_event=stop_event, worker_tasks=worker_tasks, reaper_task=reaper_task)

            await self.store.close()
            logger.info("Worker stopped")

    async def _shutdown_tasks(
        self,
        *,
        stop_event: asyncio.Event,
        worker_tasks: list[asyncio.Task],
        reaper_task: asyncio.Task | None,
    ) -> None:
        logger.info("Worker shutting down...")
        stop_event.set()

        shutdown_started = time.monotonic()
        hard_timeout_s, grace_s = self._shutdown_timeouts_seconds()

        if reaper_task is not None:
            reaper_task.cancel()

        if worker_tasks and grace_s > 0:
            logger.info("Worker shutdown: waiting up to %.1fs for in-flight jobs to finish", grace_s)
            grace_completed = await self._gather_with_timeout(tasks=worker_tasks, timeout_s=grace_s)
            if not grace_completed:
                logger.warning("Worker shutdown grace period elapsed; cancelling remaining tasks")

        remaining_tasks = self._collect_remaining_tasks(worker_tasks=worker_tasks, reaper_task=reaper_task)
        if not remaining_tasks:
            return

        for task in remaining_tasks:
            task.cancel()

        remaining_timeout_s = max(0.0, hard_timeout_s - (time.monotonic() - shutdown_started))
        if remaining_timeout_s <= 0:
            logger.warning("Worker shutdown timed out after %.1fs", hard_timeout_s)
            return

        hard_completed = await self._gather_with_timeout(tasks=remaining_tasks, timeout_s=remaining_timeout_s)
        if not hard_completed:
            logger.warning("Worker shutdown timed out after %.1fs", hard_timeout_s)

    def _shutdown_timeouts_seconds(self) -> tuple[float, float]:
        hard_timeout_s = float(self.shutdown_timeout.total_seconds())
        grace_s = float(self.shutdown_grace.total_seconds())
        return hard_timeout_s, max(0.0, min(grace_s, hard_timeout_s))

    @staticmethod
    def _collect_remaining_tasks(
        *, worker_tasks: list[asyncio.Task], reaper_task: asyncio.Task | None
    ) -> list[asyncio.Task]:
        remaining_tasks = [task for task in worker_tasks if not task.done()]
        if reaper_task is not None and not reaper_task.done():
            remaining_tasks.append(reaper_task)
        return remaining_tasks

    @staticmethod
    async def _gather_with_timeout(*, tasks: list[asyncio.Task], timeout_s: float) -> bool:
        try:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=timeout_s)
            return True
        except asyncio.TimeoutError:
            return False

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
