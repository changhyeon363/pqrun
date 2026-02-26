---
icon: lucide/sliders-horizontal
---

# Configuration & Customization

## 1. Environment Variables

```bash
# Worker on/off
WORKER_ENABLED=true

# Reaper loop on/off
WORKER_REAPER_ENABLED=true

# Concurrent worker loops per process
WORKER_CONCURRENCY=1

# Reaper interval (seconds)
WORKER_REAP_INTERVAL=60

# Default stale timeout (seconds)
WORKER_STALE_TIMEOUT=1200

# Shutdown behavior
# - Gracefully wait for in-flight jobs up to this many seconds
WORKER_SHUTDOWN_GRACE=10
# - Hard cap for worker shutdown (seconds)
WORKER_SHUTDOWN_TIMEOUT=30
```

Notes:

- 잘못된 값(예: `abc`)은 경고 후 기본값으로 fallback 됩니다.
- `WORKER_REAPER_ENABLED=false`면 stale RUNNING 복구는 수행되지 않습니다.
- `WORKER_STALE_TIMEOUT`은 job에 `timeout_seconds`가 없을 때만 적용됩니다.

## 2. Worker Constructor Options

```python
from datetime import timedelta
from pqrun import Worker, BackoffPolicy, IdlePollPolicy, LoopErrorPolicy

worker = Worker(
    store=store,
    handlers=handlers,
    concurrency=1,
    enabled=True,
    enable_reaper=True,
    backoff=BackoffPolicy(),
    idle_policy=IdlePollPolicy(base_seconds=1.0, max_seconds=10.0),
    loop_error_policy=LoopErrorPolicy(),
    reap_stale_every_seconds=60,
    default_stale_after=timedelta(minutes=20),
    shutdown_grace=timedelta(seconds=10),
    shutdown_timeout=timedelta(seconds=30),
    worker_id="custom-worker-id",
)
```

## 3. Policy Customization

정책 3개는 각각 “어떤 상황에서 sleep/delay를 계산하는지”가 다릅니다.

### 3.0 Which Policy Does What?

1. `BackoffPolicy`
    - 시점: **핸들러 실행이 실패했을 때**
    - 결정: 다음 재시도까지의 지연 시간 (`retry_after`)
    - 적용 지점: `mark_error(..., retry_after=...)`

2. `IdlePollPolicy`
    - 시점: **큐에서 가져올 작업이 없을 때** (`pickup() -> None`)
    - 결정: 다음 polling까지 sleep 시간
    - 목적: 빈 큐 상태에서 DB polling 부하 완화

3. `LoopErrorPolicy`
    - 시점: **워커 루프 인프라 오류**가 날 때
        - 예: `pickup`, `mark_done`, `mark_error` 호출 중 예외
    - 결정: 다음 루프 재시도까지 sleep 시간
    - 기본값: `0.0` (즉시 재시도)

### 3.1 Handler failure retry (`BackoffPolicy`)

```python
from datetime import timedelta
from pqrun import BackoffPolicy

class CustomBackoff(BackoffPolicy):
    def retry_delay(self, attempts: int) -> timedelta:
        return timedelta(seconds=min(2 ** attempts, 3600))
```

### 3.2 Empty queue polling (`IdlePollPolicy`)

```python
from pqrun import IdlePollPolicy

class FastIdlePolicy(IdlePollPolicy):
    def next_sleep(self, empty_streak: int) -> float:
        return min(0.2 * (empty_streak + 1), 2.0)
```

### 3.3 Infra loop errors (`LoopErrorPolicy`)

```python
from pqrun import LoopErrorPolicy

class InfraLoopPolicy(LoopErrorPolicy):
    def next_sleep(self, consecutive_errors: int) -> float:
        # Default is 0.0 (immediate retry)
        return min(0.1 * consecutive_errors, 3.0)
```

## 4. Job-level Controls (`enqueue`)

```python
job_id = await store.enqueue(
    job_type="send_email",
    payload={"user_id": 123, "template": "welcome"},
    dedupe_key="welcome:user:123",
    run_after=run_at,
    priority=10,
    max_attempts=5,
    timeout_seconds=300,
)
```

Notes:
- `payload` / `result`는 JSON object(`dict`)만 지원합니다.
- `dedupe_key` 중복 시 기존 active job `id`를 반환합니다.
