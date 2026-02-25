-- Job Cleanup Examples
--
-- pqrun does not automatically delete completed jobs.
-- Use these patterns to implement cleanup based on your retention policy.

-- ============================================================================
-- Pattern 1: Simple time-based cleanup
-- ============================================================================
-- Delete jobs completed more than 7 days ago

DELETE FROM jobs
WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
  AND finished_at < now() - interval '7 days';


-- ============================================================================
-- Pattern 2: Keep recent jobs, delete old by type
-- ============================================================================
-- Different retention per job type

DELETE FROM jobs
WHERE status IN ('DONE', 'FAILED')
  AND (
    (job_type = 'cleanup' AND finished_at < now() - interval '1 day')
    OR (job_type = 'summarize' AND finished_at < now() - interval '30 days')
    OR (job_type NOT IN ('cleanup', 'summarize') AND finished_at < now() - interval '7 days')
  );


-- ============================================================================
-- Pattern 3: Keep only failed jobs for debugging
-- ============================================================================
-- Delete successful jobs, keep failed ones longer

DELETE FROM jobs
WHERE status = 'DONE'
  AND finished_at < now() - interval '7 days';

DELETE FROM jobs
WHERE status = 'FAILED'
  AND finished_at < now() - interval '30 days';


-- ============================================================================
-- Pattern 4: Cleanup as a pqrun job
-- ============================================================================
-- Create a handler that runs cleanup, then enqueue it periodically

-- In your application:
--
-- async def cleanup_handler(ctx: JobContext) -> dict:
--     async with ctx.store.connection() as conn:
--         result = await conn.execute("""
--             DELETE FROM jobs
--             WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
--               AND finished_at < now() - interval '7 days'
--         """)
--         count = int(result.split()[-1])
--     return {"deleted": count}
--
-- # Register handler
-- handlers = {"cleanup": cleanup_handler, ...}
--
-- # Enqueue with pg_cron or manually
-- await store.enqueue("cleanup", {}, dedupe_key="cleanup:daily")


-- ============================================================================
-- Pattern 5: pg_cron scheduled cleanup
-- ============================================================================
-- Run cleanup every day at 3am

-- First, install pg_cron extension:
-- CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Schedule daily cleanup:
-- SELECT cron.schedule(
--   'cleanup-old-jobs',
--   '0 3 * * *',  -- Every day at 3am
--   $$
--   DELETE FROM jobs
--   WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
--     AND finished_at < now() - interval '7 days'
--   $$
-- );


-- ============================================================================
-- Pattern 6: Archive instead of delete
-- ============================================================================
-- Move old jobs to archive table for historical analysis

-- Create archive table (once):
-- CREATE TABLE jobs_archive (LIKE jobs INCLUDING ALL);

-- Archive old jobs:
WITH archived AS (
  DELETE FROM jobs
  WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
    AND finished_at < now() - interval '7 days'
  RETURNING *
)
INSERT INTO jobs_archive
SELECT * FROM archived;


-- ============================================================================
-- Monitoring Query: Check cleanup candidates
-- ============================================================================

SELECT
  status,
  count(*) as count,
  min(finished_at) as oldest,
  max(finished_at) as newest
FROM jobs
WHERE status IN ('DONE', 'FAILED', 'CANCELLED')
  AND finished_at IS NOT NULL
GROUP BY status
ORDER BY status;
