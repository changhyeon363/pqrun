<h1>
  <img src="https://raw.githubusercontent.com/changhyeon363/pqrun/main/docs/assets/images/logo.png" alt="pqrun logo" width="36" style="vertical-align: middle; margin-right: 8px;" />
  pqrun
</h1>

**PostgreSQL-backed job queue for async Python.**

A simple, reliable job queue built on PostgreSQL for FastAPI and other async applications. No additional infrastructure needed—just use the PostgreSQL database you already have.

---

## Features

- **Simple**: Minimal dependencies, pure SQL-based implementation
- **Safe**: Multi-worker concurrency using PostgreSQL's `SKIP LOCKED`
- **Flexible**: Three enqueue patterns (app code, pg_cron, handler chains)
- **Observable**: Built-in tracking of execution time, attempts, and results
- **Production-ready**: Retry policies, stale job recovery, bounded shutdown

---

## Installation

```bash
pip install pqrun
```

**Requirements**:
- Python 3.10+
- PostgreSQL 12+
- asyncpg

---

## Quick Start

### 1. Apply Database Schema

```bash
psql $DATABASE_URL < src/pqrun/ddl.sql
```

Or in Python:

```python
import asyncpg

async with asyncpg.connect(dsn) as conn:
    with open("src/pqrun/ddl.sql") as f:
        await conn.execute(f.read())
```

### 2. Define Handlers

```python
from pqrun import JobContext

async def summarize_handler(ctx: JobContext) -> dict:
    conversation_id = ctx.job.payload["conversation_id"]

    # Do work...
    summary = "..."

    # Optional: Chain next job
    await ctx.store.enqueue("embed", {"text": summary})

    # Return result (stored in jobs.result)
    return {"summary_id": 123, "tokens": 456}
```

### 3. Setup Worker

```python
from fastapi import FastAPI
from pqrun import PgJobStore, Worker

store = PgJobStore(dsn="postgresql://user:pass@host/db")

worker = Worker(
    store=store,
    handlers={
        "summarize": summarize_handler,
        # ... more handlers
    }
)

app = FastAPI(lifespan=worker.lifespan)
```

### 4. Enqueue Jobs

```python
# From application code
@app.post("/summarize/{conversation_id}")
async def create_summary(conversation_id: int):
    job_id = await store.enqueue(
        job_type="summarize",
        payload={"conversation_id": conversation_id},
        dedupe_key=f"summarize:conv:{conversation_id}"  # Prevent duplicates
    )
    return {"job_id": job_id}
```

---

## Usage Patterns

### Pattern 1: Application Enqueue

Enqueue jobs directly from your application code:

```python
await store.enqueue(
    job_type="send_email",
    payload={"user_id": 123, "template": "welcome"},
    priority=10,  # Higher = sooner
    run_after=datetime.now() + timedelta(hours=1)  # Delay execution
)
```

### Pattern 2: Scheduled Enqueue (pg_cron)

Use pg_cron to periodically create jobs:

```sql
-- Install pg_cron extension
CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Schedule: every 5 minutes, enqueue summary jobs
SELECT cron.schedule(
  'enqueue-summaries',
  '*/5 * * * *',
  $$
  INSERT INTO jobs (job_type, payload, dedupe_key)
  SELECT
    'summarize',
    jsonb_build_object('conversation_id', c.id),
    'summarize:conv:' || c.id
  FROM conversations c
  WHERE c.needs_summary = true
  ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
  DO NOTHING
  $$
);
```

### Pattern 3: Handler Chains

Create follow-up jobs from within handlers:

```python
async def summarize_handler(ctx: JobContext) -> dict:
    # ... do summarization ...

    # Chain embedding job
    await ctx.store.enqueue(
        job_type="embed",
        payload={"summary_id": summary_id}
    )

    return {"summary_id": summary_id}
```

---

## Configuration

### Environment Variables

```bash
# Enable/disable worker
WORKER_ENABLED=true

# Concurrent jobs per worker instance
WORKER_CONCURRENCY=1

# Reaper interval (seconds)
WORKER_REAP_INTERVAL=60

# Default stale timeout for RUNNING jobs (seconds)
WORKER_STALE_TIMEOUT=1200
```

### Code Configuration

```python
from datetime import timedelta
from pqrun import Worker, BackoffPolicy, IdlePollPolicy

worker = Worker(
    store=store,
    handlers=handlers,

    # Concurrency
    concurrency=1,  # Jobs running simultaneously

    # Retry policy (defaults shown)
    backoff=BackoffPolicy(),  # 1m, 5m, 30m, 2h, 6h

    # Idle polling (when no jobs available)
    idle_policy=IdlePollPolicy(base_seconds=1.0, max_seconds=10.0),

    # Stale job recovery
    reap_stale_every_seconds=60,  # Check every 60s
    default_stale_after=timedelta(minutes=20),  # Job timeout

    # Worker identification
    worker_id="custom-worker-1"  # Default: hostname-pid
)
```

---

## Deployment Patterns

### Pattern A: API + Worker (Single Container)

Simple deployment where API and worker run together:

```python
# main.py
app = FastAPI(lifespan=worker.lifespan)
```

```bash
uvicorn main:app
```

### Pattern B: Separated API and Worker

Scale API and workers independently:

```bash
# Terminal 1: API only
WORKER_ENABLED=false uvicorn main:app

# Terminal 2: Worker only (multiple instances)
WORKER_ENABLED=true WORKER_CONCURRENCY=4 python -m examples.worker_only
```

No code changes needed—just environment variables!

Current behavior:
- On shutdown, worker loops are cancelled with a bounded wait (30s).
- Any interrupted RUNNING jobs are recovered by the reaper.

---

## Advanced Features

### Database Transactions

Use `store.transaction()` for atomic operations:

```python
async def my_handler(ctx: JobContext) -> dict:
    async with ctx.store.transaction() as conn:
        # All queries in this block are transactional
        await conn.execute("UPDATE users SET ... WHERE id = $1", user_id)
        await conn.execute("INSERT INTO audit_log ...")
    # Auto-commit on success, rollback on exception

    return {"status": "ok"}
```

### Per-Job Timeout

Override default stale timeout for long-running jobs:

```python
await store.enqueue(
    job_type="generate_report",
    payload={"report_id": 123},
    timeout_seconds=3600  # 1 hour (instead of default 20 minutes)
)
```

### Job Deduplication

Prevent duplicate active jobs:

```python
await store.enqueue(
    job_type="process_order",
    payload={"order_id": 456},
    dedupe_key="process:order:456"  # Only one active job per order
)
```

Current behavior:
- `store.enqueue(..., dedupe_key=...)` uses `ON CONFLICT ... DO UPDATE ... RETURNING id`.
- If an active duplicate exists, the existing job `id` is returned (not `0`).

### Custom Retry Policy

```python
from datetime import timedelta
from pqrun import BackoffPolicy, LoopErrorPolicy

class CustomBackoff(BackoffPolicy):
    def retry_delay(self, attempts: int) -> timedelta:
        # Custom exponential backoff
        return timedelta(seconds=2 ** attempts)

class CustomLoopErrorPolicy(LoopErrorPolicy):
    def next_sleep(self, consecutive_errors: int) -> float:
        # Infra error retry delay in worker loop (pickup/mark_* failures)
        return min(0.5 * consecutive_errors, 5.0)

worker = Worker(
    store=store,
    handlers=handlers,
    backoff=CustomBackoff(),
    loop_error_policy=CustomLoopErrorPolicy(),
)
```

---

## Monitoring

### Query Job Status

```sql
-- Jobs by status
SELECT status, count(*) FROM jobs GROUP BY status;

-- Failed jobs (recent)
SELECT id, job_type, last_error, attempts
FROM jobs
WHERE status = 'FAILED'
  AND finished_at > now() - interval '1 hour'
ORDER BY finished_at DESC;

-- Average execution time by type
SELECT job_type, avg(duration_ms) as avg_ms, count(*)
FROM jobs
WHERE status = 'DONE'
GROUP BY job_type;

-- Stale job candidates
SELECT id, job_type, locked_at, locked_by
FROM jobs
WHERE status = 'RUNNING'
  AND locked_at < now() - interval '20 minutes';
```

### Logging

pqrun uses Python's standard `logging` module:

```python
import logging

# Set log level
logging.getLogger("pqrun").setLevel(logging.INFO)

# Customize format
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.getLogger("pqrun").addHandler(handler)
```

---

## Job Cleanup

pqrun does not automatically delete completed jobs. Implement cleanup based on your retention policy:

```sql
-- Simple: Delete jobs older than 7 days
DELETE FROM jobs
WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
  AND finished_at < now() - interval '7 days';
```

See [examples/cleanup.sql](examples/cleanup.sql) for more patterns.

---

## Examples

- **[FastAPI Integration](examples/fastapi_example.py)**: Complete example with handlers, enqueue endpoints, and chaining
- **[SQL Enqueue Patterns](examples/enqueue_patterns.sql)**: Batch enqueue, triggers, pg_cron
- **[Cleanup Strategies](examples/cleanup.sql)**: Job retention and archival patterns

---

## Architecture

```
┌─────────────────────────────────────────────┐
│           Application Layer                 │
│  ┌────────────┐  ┌────────────────────┐    │
│  │  FastAPI   │  │  Job Handlers      │    │
│  └─────┬──────┘  └─────┬──────────────┘    │
└────────┼───────────────┼────────────────────┘
         │               │
         │       ┌───────▼───────┐
         │       │    Worker     │
         │       │  - Poll loop  │
         │       │  - Dispatch   │
         │       │  - Retry      │
         │       └───────┬───────┘
         │               │
         └───────────────▼───────────┐
                 │   PgJobStore      │
                 │  - enqueue()      │
                 │  - pickup()       │
                 │  - mark_*()       │
                 └───────┬───────────┘
                         │
                 ┌───────▼───────┐
                 │  PostgreSQL   │
                 │  jobs table   │
                 └───────────────┘
```

**Key Mechanisms**:
- **SKIP LOCKED**: Safe concurrent job pickup across multiple workers
- **At-least-once delivery**: Jobs may execute multiple times (design handlers to be idempotent)
- **Retry with backoff**: Automatic exponential backoff on failures
- **Stale recovery**: Background reaper detects crashed workers and resets stuck jobs

---

## Design Decisions

For detailed rationale, see [Design Document](docs/developer/design.md) and [Implementation Decisions](docs/developer/decisions.md).

**Key choices**:
- **asyncpg only** (no ORM): Maximum compatibility, minimal dependencies
- **Handler returns dict**: Result stored in `jobs.result` for observability
- **On-demand transactions**: `store.transaction()` instead of injecting connection
- **Shutdown strategy**: Stop pickup, cancel worker tasks, recover interrupted jobs via reaper
- **SQL-based migrations**: No framework lock-in

---

## Comparison with Alternatives

| Feature         | pqrun       | Celery       | TaskIQ       | RQ           |
|-----------------|--------------|--------------|--------------|--------------|
| Backend         | PostgreSQL   | Redis/Rabbit | Redis/etc    | Redis        |
| Async/Await     | ✅ Native    | ⚠️ Limited   | ✅ Native    | ❌           |
| FastAPI         | ✅ Lifespan  | ⚠️ Separate  | ✅ Lifespan  | ⚠️ Separate  |
| Extra Infra     | ❌ None      | ✅ Required  | ✅ Required  | ✅ Required  |
| Complexity      | Low          | High         | Medium       | Low          |

**Choose pqrun if**:
- You already use PostgreSQL
- You want simplicity over complex features
- You need native FastAPI async integration
- You prefer SQL-based job management

---

## Contributing

Contributions are welcome! Please see [Design Document](docs/developer/design.md) for architecture details.

### Development Setup

```bash
# Clone repository
git clone https://github.com/changhyeon363/pqrun.git
cd pqrun

# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest

# Type checking
mypy src/pqrun

# Linting
ruff check src/pqrun
```

---

## License

MIT

---

## Documentation

- **[Library User Docs](docs/user/index.md)**: Integration guides and quick start
- **[Developer Docs](docs/developer/index.md)**: Architecture, design, and decisions
- **[Examples](examples/)**: FastAPI integration, SQL patterns, cleanup strategies

### Docs Site (Zensical)

```bash
# Install docs dependencies
pip install -e ".[docs]" --upgrade

# Run local docs server
zensical serve

# Build static docs
zensical build
```

---

## Support

- **Issues**: [GitHub Issues](https://github.com/changhyeon363/pqrun/issues)
- **Discussions**: [GitHub Discussions](https://github.com/changhyeon363/pqrun/discussions)

---

**Built with ❤️ for the async Python community.**
