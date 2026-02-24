"""
Run pgjobq worker loops without serving FastAPI routes.

Usage:
    WORKER_ENABLED=true WORKER_CONCURRENCY=4 python -m examples.worker_only
"""

from __future__ import annotations

import asyncio

from .fastapi_example import worker


async def main() -> None:
    # Reuse the same lifespan path as FastAPI startup/shutdown.
    async with worker.lifespan(None):
        await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
