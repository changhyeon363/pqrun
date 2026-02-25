-- Enqueue Pattern Examples
--
-- pqrun supports multiple ways to enqueue jobs:
-- 1. From application code (Python)
-- 2. From SQL (pg_cron, triggers, etc.)
-- 3. From job handlers (chaining)

-- ============================================================================
-- Pattern 1: Single job enqueue with deduplication
-- ============================================================================

INSERT INTO jobs (job_type, payload, dedupe_key, run_after, priority)
VALUES (
  'summarize',
  jsonb_build_object('conversation_id', 123),
  'summarize:conv:123',  -- Prevents duplicate active jobs
  now(),
  0
)
ON CONFLICT (dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
DO UPDATE SET updated_at = now()
RETURNING id;


-- ============================================================================
-- Pattern 2: Batch enqueue from SELECT
-- ============================================================================
-- Find conversations that need summarization and enqueue jobs

INSERT INTO jobs (job_type, payload, dedupe_key, priority)
SELECT
  'summarize',
  jsonb_build_object('conversation_id', c.id),
  'summarize:conv:' || c.id,
  10  -- Higher priority
FROM conversations c
WHERE c.message_count >= 5
  AND c.last_summarized_at < now() - interval '1 hour'
  AND NOT EXISTS (
    SELECT 1 FROM jobs j
    WHERE j.dedupe_key = 'summarize:conv:' || c.id
      AND j.status IN ('READY', 'RUNNING')
  )
ON CONFLICT (dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
DO NOTHING;


-- ============================================================================
-- Pattern 3: Delayed job (run_after)
-- ============================================================================
-- Schedule a job to run 1 hour from now

INSERT INTO jobs (job_type, payload, run_after)
VALUES (
  'send_reminder',
  jsonb_build_object('user_id', 456, 'message', 'Hello!'),
  now() + interval '1 hour'
);


-- ============================================================================
-- Pattern 4: High priority job
-- ============================================================================
-- Urgent job that should run before others

INSERT INTO jobs (job_type, payload, priority)
VALUES (
  'process_payment',
  jsonb_build_object('payment_id', 789),
  100  -- High priority
);


-- ============================================================================
-- Pattern 5: Job with custom timeout
-- ============================================================================
-- Long-running job that needs more time before being marked stale

INSERT INTO jobs (job_type, payload, timeout_seconds)
VALUES (
  'generate_report',
  jsonb_build_object('report_id', 999),
  3600  -- 1 hour timeout (instead of default 20 minutes)
);


-- ============================================================================
-- Pattern 6: Trigger-based enqueue
-- ============================================================================
-- Automatically enqueue a job when a row is inserted

-- Create trigger function:
CREATE OR REPLACE FUNCTION enqueue_conversation_summary()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.message_count >= 5 THEN
    INSERT INTO jobs (job_type, payload, dedupe_key)
    VALUES (
      'summarize',
      jsonb_build_object('conversation_id', NEW.id),
      'summarize:conv:' || NEW.id
    )
    ON CONFLICT (dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
    DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Attach trigger:
-- CREATE TRIGGER conversation_summary_trigger
--   AFTER INSERT OR UPDATE ON conversations
--   FOR EACH ROW
--   EXECUTE FUNCTION enqueue_conversation_summary();


-- ============================================================================
-- Pattern 7: pg_cron scheduled batch enqueue
-- ============================================================================
-- Use pg_cron to periodically enqueue jobs

-- Install pg_cron:
-- CREATE EXTENSION IF NOT EXISTS pg_cron;

-- Schedule: Every 5 minutes, enqueue summary jobs for active conversations
-- SELECT cron.schedule(
--   'enqueue-summaries',
--   '*/5 * * * *',
--   $$
--   INSERT INTO jobs (job_type, payload, dedupe_key)
--   SELECT
--     'summarize',
--     jsonb_build_object('conversation_id', c.id),
--     'summarize:conv:' || c.id
--   FROM conversations c
--   WHERE c.message_count >= 5
--     AND (c.last_summarized_at IS NULL OR c.last_summarized_at < now() - interval '1 hour')
--   ON CONFLICT (dedupe_key)
--   WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
--   DO NOTHING
--   $$
-- );


-- ============================================================================
-- Pattern 8: Idempotent batch enqueue
-- ============================================================================
-- Safe to run multiple times without creating duplicates

INSERT INTO jobs (job_type, payload, dedupe_key)
SELECT
  'daily_report',
  jsonb_build_object('date', current_date),
  'daily_report:' || current_date
ON CONFLICT (dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('READY', 'RUNNING')
DO NOTHING;


-- ============================================================================
-- Monitoring: Check enqueue rate
-- ============================================================================

SELECT
  date_trunc('hour', created_at) as hour,
  job_type,
  count(*) as enqueued
FROM jobs
WHERE created_at > now() - interval '24 hours'
GROUP BY hour, job_type
ORDER BY hour DESC, job_type;
