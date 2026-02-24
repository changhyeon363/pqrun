---
icon: lucide/rocket
---

# Quick Start Guide

Get up and running with pgjobq in 5 minutes.

---

## Prerequisites

- Python 3.10+
- PostgreSQL 12+
- A PostgreSQL database (can be local or remote)

---

## Step 1: Install

```bash
pip install pgjobq
```

---

## Step 2: Setup Database

Create the `jobs` table and indexes:

```bash
# Using psql
psql $DATABASE_URL < venv/lib/python*/site-packages/pgjobq/ddl.sql

# Or manually
psql $DATABASE_URL -c "$(cat venv/lib/python*/site-packages/pgjobq/ddl.sql)"
```

Or in Python:

```python
import asyncpg
import asyncio

async def setup_db():
    conn = await asyncpg.connect("postgresql://user:pass@localhost/mydb")

    # Read DDL from package
    import pgjobq
    from pathlib import Path
    ddl_path = Path(pgjobq.__file__).parent / "ddl.sql"
    ddl = ddl_path.read_text()

    await conn.execute(ddl)
    await conn.close()
    print("✓ Database schema created")

asyncio.run(setup_db())
```

---

## Step 3: Create Your App

Create `main.py`:

```python
import os
from fastapi import FastAPI
from pgjobq import PgJobStore, Worker, JobContext

# 1. Define your job handler
async def send_email(ctx: JobContext) -> dict:
    user_id = ctx.job.payload["user_id"]
    template = ctx.job.payload["template"]

    # TODO: Implement your email sending logic
    print(f"Sending {template} email to user {user_id}")

    return {"status": "sent", "user_id": user_id}

# 2. Setup store and worker
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://user:pass@localhost/mydb")

store = PgJobStore(dsn=DATABASE_URL)

worker = Worker(
    store=store,
    handlers={
        "send_email": send_email,
    }
)

# 3. Create FastAPI app
app = FastAPI(lifespan=worker.lifespan)

# 4. Add enqueue endpoint
@app.post("/send-email")
async def enqueue_email(user_id: int, template: str = "welcome"):
    job_id = await store.enqueue(
        job_type="send_email",
        payload={"user_id": user_id, "template": template}
    )
    return {"job_id": job_id, "status": "enqueued"}

@app.get("/")
async def root():
    return {"message": "pgjobq is running!"}
```

---

## Step 4: Run

```bash
# Set your database URL
export DATABASE_URL="postgresql://user:pass@localhost/mydb"

# Start the server
uvicorn main:app --reload
```

---

## Step 5: Test

```bash
# Enqueue a job
curl -X POST "http://localhost:8000/send-email?user_id=123&template=welcome"

# Response:
# {"job_id": 1, "status": "enqueued"}

# Check the logs - you should see:
# INFO:pgjobq.worker:Executing job 1 type=send_email (attempt 1)
# Sending welcome email to user 123
# INFO:pgjobq.store:Job 1 completed (duration=...ms)
```

---

## Next Steps

### Add More Handlers

```python
async def process_payment(ctx: JobContext) -> dict:
    payment_id = ctx.job.payload["payment_id"]
    # ... payment processing logic ...
    return {"payment_id": payment_id, "status": "completed"}

worker = Worker(
    store=store,
    handlers={
        "send_email": send_email,
        "process_payment": process_payment,  # Add more handlers
    }
)
```

### Use Deduplication

Prevent duplicate jobs:

```python
@app.post("/send-welcome-email/{user_id}")
async def send_welcome(user_id: int):
    job_id = await store.enqueue(
        job_type="send_email",
        payload={"user_id": user_id, "template": "welcome"},
        dedupe_key=f"welcome:user:{user_id}"  # Only one welcome email per user
    )
    return {"job_id": job_id}
```

Current behavior:
- With `dedupe_key`, enqueue returns the existing active job `id` when duplicated.

### Schedule Delayed Jobs

```python
from datetime import datetime, timedelta

@app.post("/schedule-reminder/{user_id}")
async def schedule_reminder(user_id: int, hours: int = 24):
    run_at = datetime.now() + timedelta(hours=hours)

    job_id = await store.enqueue(
        job_type="send_email",
        payload={"user_id": user_id, "template": "reminder"},
        run_after=run_at  # Job will run after specified time
    )
    return {"job_id": job_id, "scheduled_at": run_at.isoformat()}
```

### Chain Jobs

Create follow-up jobs from handlers:

```python
async def process_order(ctx: JobContext) -> dict:
    order_id = ctx.job.payload["order_id"]

    # Process the order
    # ...

    # Chain: Send confirmation email
    await ctx.store.enqueue(
        job_type="send_email",
        payload={"user_id": user_id, "template": "order_confirmation"}
    )

    return {"order_id": order_id, "status": "processed"}
```

### Monitor Jobs

```python
@app.get("/jobs/stats")
async def job_stats():
    async with store.connection() as conn:
        rows = await conn.fetch("""
            SELECT status, count(*) as count
            FROM jobs
            GROUP BY status
            ORDER BY status
        """)

    return {row["status"]: row["count"] for row in rows}
```

### Separate Worker and API

For production, run workers separately:

```bash
# Terminal 1: API only
WORKER_ENABLED=false uvicorn main:app

# Terminal 2: Worker only (multiple instances)
WORKER_ENABLED=true WORKER_CONCURRENCY=4 python -c "
from main import worker, store
import asyncio

async def run_worker():
    # Simulate FastAPI lifespan
    async with worker.lifespan(None):
        # Keep running
        await asyncio.Event().wait()

asyncio.run(run_worker())
"
```

Or reuse the provided example module:

```bash
WORKER_ENABLED=true WORKER_CONCURRENCY=4 python -m examples.worker_only
```

Current behavior:
- Shutdown uses bounded cancellation (30s wait) and reaper-based recovery for interrupted RUNNING jobs.

---

## Troubleshooting

### "No handler registered for job_type=X"

Make sure the handler is registered in the `handlers` dict:

```python
handlers = {
    "my_job": my_handler,  # Add your handler here
}
```

### Jobs not being picked up

Check:
1. Worker is enabled: `WORKER_ENABLED=true`
2. Database connection is working
3. Jobs table exists: `\dt jobs` in psql
4. Jobs are in READY status: `SELECT status, count(*) FROM jobs GROUP BY status;`

### Jobs stuck in RUNNING

The reaper will automatically recover stale jobs after 20 minutes (default).
Or manually reset:

```sql
UPDATE jobs SET status='READY', locked_at=NULL, locked_by=NULL WHERE status='RUNNING';
```

---

## Learn More

- **[Full Documentation](../README.md)**: Complete feature guide
- **[Design Document](../developer/design.md)**: Architecture and internals
- **[Examples](../examples/)**: More complex patterns
- **[SQL Patterns](../examples/enqueue_patterns.sql)**: Batch jobs, triggers, pg_cron

---

**You're all set! 🎉**
