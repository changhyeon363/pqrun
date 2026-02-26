---
icon: lucide/scroll-text
---

# Release Notes

## 0.0.1

- Initial public package version (`pyproject.toml` version `0.0.1`).
- Core queue lifecycle shipped: `enqueue`, `pickup`, `mark_done`, `mark_error`, `reap_stale` on PostgreSQL.
- FastAPI lifespan integration pattern documented (`app = FastAPI(lifespan=worker.lifespan)`).
- Baseline operational model documented:
  - at-least-once delivery
  - stale RUNNING recovery via reaper
  - dedupe for active jobs (`READY`/`RUNNING`)

## 0.0.2

### Worker lifecycle

- Added configurable graceful shutdown with two knobs:
  - `shutdown_grace` / `WORKER_SHUTDOWN_GRACE` (default: 10s)
  - `shutdown_timeout` / `WORKER_SHUTDOWN_TIMEOUT` (default: 30s)
- Shutdown flow now uses wait-then-cancel:
  - stop new pickup first
  - wait for in-flight jobs up to grace
  - cancel remaining tasks
  - bound total shutdown by hard timeout
- Refactored shutdown internals in `Worker` for readability:
  - `_shutdown_tasks`
  - `_shutdown_timeouts_seconds`
  - `_collect_remaining_tasks`
  - `_gather_with_timeout`

### Reaper control

- Added `enable_reaper` option on `Worker` (default: `True`).
- Added `WORKER_REAPER_ENABLED` environment variable.
- Worker startup now logs either:
  - `Started N worker loops + 1 reaper`
  - `Started N worker loops (reaper disabled)`

### Tests

- Added tests for shutdown behavior:
  - waits for in-flight completion within grace
  - cancels in-flight work after grace expires
  - same graceful behavior when used via FastAPI lifespan
- Added tests for reaper configuration:
  - env parsing for `WORKER_REAPER_ENABLED`
  - reaper loop is not started when disabled

### Docs

- Updated `README.md` and user docs with:
  - new shutdown settings
  - new reaper enable/disable setting
  - updated shutdown behavior description
- Updated developer decisions doc to reflect current shutdown strategy defaults.
