from __future__ import annotations

import os
import uuid
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

ROOT = Path(__file__).resolve().parents[1]
DDL_PATH = ROOT / "src" / "pqrun" / "ddl.sql"


def _load_database_url() -> str:
    if os.getenv("DATABASE_URL"):
        return os.environ["DATABASE_URL"]

    env_path = ROOT / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "DATABASE_URL":
                return value.strip().strip('"').strip("'")

    raise RuntimeError("DATABASE_URL not found in environment or .env")


@pytest.fixture(scope="session")
def database_url() -> str:
    return _load_database_url()


@pytest.fixture(scope="session")
def test_schema_name() -> str:
    return f"pqrun_test_{uuid.uuid4().hex[:8]}"


@pytest_asyncio.fixture(scope="session")
async def test_db_context(database_url: str, test_schema_name: str) -> dict[str, str]:
    conn = await asyncpg.connect(dsn=database_url)
    quoted_schema = f'"{test_schema_name}"'

    await conn.execute(f"CREATE SCHEMA {quoted_schema};")

    ddl_sql = DDL_PATH.read_text(encoding="utf-8")
    await conn.execute(f"SET search_path TO {quoted_schema};")
    await conn.execute(ddl_sql)
    await conn.close()

    print(f"[pqrun-test] created schema: {test_schema_name}")
    print(f"[pqrun-test] cleanup SQL: DROP SCHEMA {quoted_schema} CASCADE;")

    return {"database_url": database_url, "schema": test_schema_name}


@pytest_asyncio.fixture
async def store_with_pool(test_db_context: dict[str, str]):
    from pqrun.store_asyncpg import PgJobStore

    schema = test_db_context["schema"]
    quoted_schema = f'"{schema}"'

    async def setup_connection(conn: asyncpg.Connection) -> None:
        await conn.execute(f"SET search_path TO {quoted_schema};")

    pool = await asyncpg.create_pool(
        dsn=test_db_context["database_url"],
        min_size=1,
        max_size=5,
        setup=setup_connection,
    )

    store = PgJobStore(pool=pool)
    await store.start()
    yield store
    await store.close()
    await pool.close()
