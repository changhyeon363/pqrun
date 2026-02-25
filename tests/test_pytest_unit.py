from __future__ import annotations

from datetime import timedelta

from pqrun.backoff import BackoffPolicy, IdlePollPolicy
from pqrun.store_asyncpg import PgJobStore


class _AcquireCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self, row):
        self._row = row
        self.called = False

    async def fetchrow(self, *args, **kwargs):
        self.called = True
        return self._row


class _FakePool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCM(self._conn)


def test_backoff_policy_defaults():
    policy = BackoffPolicy()
    assert policy.retry_delay(1) == timedelta(minutes=1)
    assert policy.retry_delay(2) == timedelta(minutes=5)
    assert policy.retry_delay(3) == timedelta(minutes=30)
    assert policy.retry_delay(4) == timedelta(hours=2)
    assert policy.retry_delay(99) == timedelta(hours=6)


def test_idle_poll_policy_defaults():
    policy = IdlePollPolicy(base_seconds=1.0, max_seconds=10.0)
    assert policy.next_sleep(0) == 1.0
    assert policy.next_sleep(1) == 2.0
    assert policy.next_sleep(2) == 5.0
    assert policy.next_sleep(3) == 10.0


async def test_mark_error_uses_fetchrow_result():
    conn = _FakeConn({"attempts": 1, "max_attempts": 5})
    store = PgJobStore(pool=_FakePool(conn))

    await store.mark_error(
        1,
        "boom",
        retry_after=timedelta(seconds=1),
        terminal=False,
    )

    assert conn.called is True
