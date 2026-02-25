-- pqrun Database Schema
-- PostgreSQL job queue tables and indexes

-- Job status enum
DO $$ BEGIN
  CREATE TYPE job_status AS ENUM (
    'READY',      -- Waiting to be picked up
    'RUNNING',    -- Currently being processed
    'DONE',       -- Successfully completed
    'FAILED',     -- Failed after max_attempts
    'CANCELLED'   -- Manually cancelled
  );
EXCEPTION WHEN duplicate_object THEN
  NULL; -- Type already exists, skip
END $$;

-- Jobs table
CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY,

  -- Job identification
  job_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Execution control
  status job_status NOT NULL DEFAULT 'READY',
  priority int NOT NULL DEFAULT 0,

  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),

  -- Per-job timeout for stale recovery (seconds)
  -- If NULL, worker uses default_stale_after
  timeout_seconds int,

  -- Lock tracking
  locked_at timestamptz,
  locked_by text,

  -- Deduplication
  -- Unique among active (READY/RUNNING) jobs
  dedupe_key text,

  -- Error tracking
  last_error text,

  -- Observability
  finished_at timestamptz,
  duration_ms int,
  result jsonb,

  -- Metadata
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Index for efficient job pickup
-- Covers: status, run_after, priority (DESC), id
CREATE INDEX IF NOT EXISTS idx_jobs_pick
  ON jobs (status, run_after, priority DESC, id);

-- Index for filtering by job type
CREATE INDEX IF NOT EXISTS idx_jobs_type
  ON jobs (job_type);

-- Partial unique index for deduplication
-- Only active jobs (READY or RUNNING) with non-null dedupe_key
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_dedupe
  ON jobs (dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING');

-- Optional: Index for observability queries
-- CREATE INDEX IF NOT EXISTS idx_jobs_finished
--   ON jobs (status, finished_at DESC)
--   WHERE status IN ('DONE', 'FAILED', 'CANCELLED');
