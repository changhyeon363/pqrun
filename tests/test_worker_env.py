from __future__ import annotations

from datetime import timedelta

from pqrun import Worker
from pqrun.store_asyncpg import PgJobStore


def test_worker_env_reap_interval_and_stale_timeout(monkeypatch):
    monkeypatch.setenv("WORKER_REAP_INTERVAL", "15")
    monkeypatch.setenv("WORKER_STALE_TIMEOUT", "90")

    worker = Worker(
        store=PgJobStore(dsn="postgresql://example"),
        handlers={},
    )

    assert worker.reap_stale_every_seconds == 15
    assert worker.default_stale_after == timedelta(seconds=90)


def test_worker_env_reap_interval_and_stale_timeout_clamped(monkeypatch):
    monkeypatch.setenv("WORKER_REAP_INTERVAL", "0")
    monkeypatch.setenv("WORKER_STALE_TIMEOUT", "0")

    worker = Worker(
        store=PgJobStore(dsn="postgresql://example"),
        handlers={},
    )

    assert worker.reap_stale_every_seconds == 1
    assert worker.default_stale_after == timedelta(seconds=1)


def test_worker_env_reaper_enabled_flag(monkeypatch):
    monkeypatch.setenv("WORKER_REAPER_ENABLED", "false")

    worker = Worker(
        store=PgJobStore(dsn="postgresql://example"),
        handlers={},
    )

    assert worker.enable_reaper is False
