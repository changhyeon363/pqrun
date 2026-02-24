from __future__ import annotations

import pytest

from pgjobq.store_asyncpg import PgJobStore


class _AcquireCM:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    async def fetchrow(self, *args, **kwargs):
        return {"id": 1}


class _FakePool:
    def __init__(self):
        self._conn = _FakeConn()

    def acquire(self):
        return _AcquireCM(self._conn)


@pytest.mark.asyncio
async def test_enqueue_payload_must_be_json_object():
    store = PgJobStore(pool=_FakePool())
    with pytest.raises(ValueError, match="JSON objects"):
        await store.enqueue(
            job_type="x",
            payload=["not", "an", "object"],  # type: ignore[arg-type]
        )
