---
icon: lucide/pen-tool
---

# Design Document

**Version:** 0.1.0  
**Date:** 2026-02-24  
**Status:** Final  

---

## 1. Overview

### 1.1 Purpose
`pgjobq`는 PostgreSQL을 백엔드로 사용하는 경량 비동기 작업 큐 라이브러리입니다. FastAPI와 같은 async Python 애플리케이션에서 백그라운드 작업을 안전하고 효율적으로 처리하기 위해 설계되었습니다.

### 1.2 Design Goals
- **Simple**: ORM 비의존, 순수 SQL 기반으로 최소 의존성
- **Safe**: SKIP LOCKED를 통한 안전한 동시성 제어
- **Flexible**: 3가지 enqueue 방식 모두 지원
- **Observable**: 작업 상태, 실행 시간, 결과 추적 가능
- **Production-ready**: 재시도, timeout, stale recovery 등 운영 필수 기능 내장

### 1.3 Non-Goals
- 분산 트랜잭션 지원 (2PC, saga 등)
- Exactly-once 보장 (at-least-once 전제)
- 복잡한 워크플로우 엔진 (DAG, branching 등)
- 다른 DB 백엔드 지원 (PostgreSQL 전용)

---

## 2. Architecture

### 2.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Application Layer                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐      │
│  │   FastAPI    │  │   Handler    │  │   Handler    │      │
│  │     App      │  │  (summarize) │  │   (embed)    │      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘      │
│         │                 │                 │               │
└─────────┼─────────────────┼─────────────────┼───────────────┘
          │                 │                 │
          │         ┌───────▼─────────────────▼───────┐
          │         │      Worker (pgjobq)            │
          │         │  - Poll loop                    │
          │         │  - Concurrency control          │
          │         │  - Handler dispatch             │
          │         │  - Retry/backoff                │
          │         └───────┬─────────────────────────┘
          │                 │
          └─────────────────▼─────────────────────────┐
                    │   PgJobStore (pgjobq)           │
                    │  - enqueue()                    │
                    │  - pickup() [SKIP LOCKED]       │
                    │  - mark_done/error()            │
                    │  - reap_stale()                 │
                    └───────┬─────────────────────────┘
                            │
                    ┌───────▼─────────┐
                    │   PostgreSQL    │
                    │   jobs table    │
                    └─────────────────┘
```

### 2.2 Component Responsibilities

#### PgJobStore
- **책임**: PostgreSQL과의 모든 상호작용
- **제공 메서드**:
  - `enqueue()`: 작업 등록
  - `pickup()`: 작업 획득 (SKIP LOCKED)
  - `mark_done()`: 완료 처리
  - `mark_error()`: 실패 처리 및 재시도 스케줄링
  - `reap_stale()`: 장애 복구 (stale RUNNING → READY)
  - `connection()` / `transaction()`: 핸들러용 DB 접근

#### Worker
- **책임**: 작업 소비 및 실행 제어
- **주요 기능**:
  - Poll loop (idle backoff 포함)
  - Concurrency 제어 (worker loop 개수 기반)
  - Handler routing 및 실행
  - 실행 시간 측정
  - 재시도 정책 적용
  - Stale job reaper (백그라운드 루프)

#### Handler
- **책임**: 실제 비즈니스 로직 구현
- **인터페이스**: `async def handler(ctx: JobContext) -> dict | None`
  - 반환값: 작업 결과 (jobs.result에 저장), None이면 저장 안 함
- **제약**: Idempotent 구현 권장 (at-least-once 보장)

---

## 3. Data Model

### 3.1 Database Schema

```sql
-- Job lifecycle states
CREATE TYPE job_status AS ENUM (
  'READY',      -- 실행 대기 중
  'RUNNING',    -- 현재 실행 중
  'DONE',       -- 성공 완료
  'FAILED',     -- 최종 실패 (재시도 소진)
  'CANCELLED'   -- 사용자 취소
);

-- Jobs table
CREATE TABLE jobs (
  id bigserial PRIMARY KEY,

  -- Job identification
  job_type text NOT NULL,           -- Handler routing key
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- Execution control
  status job_status NOT NULL DEFAULT 'READY',
  priority int NOT NULL DEFAULT 0,  -- Higher = earlier

  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),
  timeout_seconds int,               -- Per-job stale threshold

  -- Lock tracking
  locked_at timestamptz,
  locked_by text,                    -- worker_id

  -- Deduplication
  dedupe_key text,                   -- Unique within active jobs

  -- Error tracking
  last_error text,

  -- Observability
  finished_at timestamptz,
  duration_ms int,
  result jsonb,                      -- Optional handler output

  -- Metadata
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX idx_jobs_pick
  ON jobs (status, run_after, priority DESC, id);

CREATE INDEX idx_jobs_type
  ON jobs (job_type);

CREATE UNIQUE INDEX uq_jobs_active_dedupe
  ON jobs (dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('READY','RUNNING');
```

### 3.2 Job Lifecycle

```
  [CREATED]
      │
      ▼
   READY ◄──────────────┐
      │                 │
      │ pickup()        │ retry (attempts < max)
      ▼                 │
  RUNNING ──────────────┤
      │                 │
      ├─ success ───────▼── DONE
      │
      ├─ error (terminal) ──► FAILED
      │
      ├─ error (retryable) ──► READY (delayed)
      │
      └─ stale (timeout) ──► READY (reaped)
```

### 3.3 Python Models

```python
@dataclass(frozen=True)
class Job:
    id: int
    job_type: str
    payload: dict[str, Any]
    status: JobStatus
    priority: int
    attempts: int
    max_attempts: int
    run_after: datetime
    timeout_seconds: Optional[int]
    locked_at: Optional[datetime]
    locked_by: Optional[str]
    dedupe_key: Optional[str]
    last_error: Optional[str]
    finished_at: Optional[datetime]
    duration_ms: Optional[int]
    result: Optional[dict[str, Any]]
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class JobContext:
    job: Job
    store: PgJobStore
    worker_id: str
```

---

## 4. Core Mechanisms

### 4.1 Enqueue Patterns

#### Pattern 1: Application Enqueue
애플리케이션 코드에서 직접 호출:
```python
await store.enqueue(
    job_type="summarize",
    payload={"conversation_id": 123},
    dedupe_key="summarize:conv:123",
    priority=10
)
```

#### Pattern 2: Scheduled Enqueue (pg_cron)
주기적 배치 작업:
```sql
-- Function called by pg_cron
INSERT INTO jobs (job_type, payload, dedupe_key)
SELECT
  'cleanup',
  jsonb_build_object('target_date', current_date - 30),
  'cleanup:' || (current_date - 30)::text
ON CONFLICT DO NOTHING;
```

#### Pattern 3: Handler Chain
핸들러 내부에서 후속 작업 생성:
```python
async def summarize_handler(ctx: JobContext) -> dict:
    # ... do summarization ...
    summary_id = ...

    # Chain next job
    await ctx.store.enqueue(
        job_type="embed",
        payload={"summary_id": summary_id}
    )

    # Return result for observability
    return {"summary_id": summary_id, "tokens": 1234}
```

### 4.2 Safe Pickup (SKIP LOCKED)

```sql
WITH picked AS (
  SELECT id
  FROM jobs
  WHERE status='READY' AND run_after <= now()
  ORDER BY priority DESC, id
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
UPDATE jobs SET
  status='RUNNING',
  locked_at=now(),
  locked_by=$1,
  attempts = attempts + 1
FROM picked
WHERE jobs.id = picked.id
RETURNING *;
```

**특징**:
- 다중 워커/레플리카 간 경합 없음
- Row-level lock으로 안전한 소비 보장
- Deadlock 발생 없음

### 4.3 Retry & Backoff

#### Retry Policy (BackoffPolicy)
```
Attempt 1 → 1분 후 재시도
Attempt 2 → 5분 후
Attempt 3 → 30분 후
Attempt 4 → 2시간 후
Attempt 5+ → 6시간 후
```

#### Loop Infra Error Policy (LoopErrorPolicy)
`pickup/mark_*` 같은 인프라 레벨 예외 시 worker loop 재시도 지연을 제어:
- 기본: 즉시 재시도 (`0s`)
- 커스텀 정책 주입 가능 (`Worker(..., loop_error_policy=...)`)

#### Idle Poll Backoff (IdlePollPolicy)
작업이 없을 때 점진적 sleep:
```
Empty streak 0 → 1초
Empty streak 1 → 2초
Empty streak 2 → 5초
Empty streak 3+ → 10초 (max)
```

### 4.4 Stale Job Recovery

**문제**: Worker가 crash하면 RUNNING 상태로 고착
**해결**: Reaper가 주기적으로 stale job을 READY로 복구

```python
# Default: 20분 이상 RUNNING인 작업은 stale로 간주
await store.reap_stale(default_stale_after=timedelta(minutes=20))

# Per-job timeout 지원:
# timeout_seconds가 설정된 작업은 개별 기준 적용
```

---

## 5. Concurrency & Scalability

### 5.1 Deployment Patterns

#### Pattern A: API + Worker 겸업 (초기)
```
┌──────────────────────┐
│   FastAPI Container  │
│  - API endpoints     │
│  - Worker (conc=1)   │
└──────────────────────┘
```
**장점**: 단순, 배포 편리
**단점**: 작업이 API 성능에 영향

#### Pattern B: 분리 배포 (확장)
```
┌──────────────────┐      ┌──────────────────┐
│  API Container   │      │ Worker Container │
│  WORKER_ENABLED= │      │ WORKER_ENABLED=  │
│       false      │      │      true        │
│                  │      │  CONCURRENCY=4   │
└──────────────────┘      └──────────────────┘
           │                       │
           └───────┬───────────────┘
                   ▼
            [PostgreSQL]
```
**장점**: 독립 스케일링, 격리
**전환**: 환경변수만 변경, 코드 수정 불필요

### 5.2 Concurrency Control

```python
Worker(
    store=store,
    handlers=handlers,
    concurrency=1,  # Per-replica concurrency
    enabled=True    # WORKER_ENABLED env override
)
```

**설계 원칙**:
- 레플리카당 concurrency=1로 시작 (API 보호)
- Worker 분리 후 concurrency 증가
- Worker loop 개수로 동시 실행 제한

---

## 6. Observability

### 6.1 Job Metrics

모든 작업에 자동 기록:
- `finished_at`: 완료 시각
- `duration_ms`: 실행 시간 (밀리초)
- `attempts`: 시도 횟수
- `last_error`: 마지막 에러 메시지

### 6.2 Monitoring Queries

```sql
-- 실행 중인 작업
SELECT * FROM jobs WHERE status='RUNNING';

-- 실패 작업 (최근 1시간)
SELECT * FROM jobs
WHERE status='FAILED'
  AND finished_at > now() - interval '1 hour';

-- 평균 실행 시간 (job_type별)
SELECT job_type, avg(duration_ms), count(*)
FROM jobs
WHERE status='DONE'
GROUP BY job_type;

-- Stale 후보 (20분 이상 RUNNING)
SELECT * FROM jobs
WHERE status='RUNNING'
  AND locked_at < now() - interval '20 minutes';
```

---

## 7. Error Handling

### 7.1 Error Categories

#### Retryable Errors
- Transient failures (network, DB connection)
- External service timeout
- Rate limit

**처리**: `mark_error(terminal=False)` → READY로 백오프

#### Terminal Errors
- Invalid payload
- Business logic violation
- No handler registered

**처리**: `mark_error(terminal=True)` → FAILED로 즉시 종료

### 7.2 Handler Error Contract

```python
async def my_handler(ctx: JobContext):
    try:
        # Business logic
        ...
    except TemporaryError:
        raise  # → Worker가 retryable로 처리
    except PermanentError:
        raise  # → Worker가 max_attempts 체크
    # 정상 완료 → mark_done()
```

**Worker가 보장하는 것**:
- 모든 exception 캐치
- duration_ms 기록
- 재시도 정책 적용

---

## 8. Configuration

### 8.1 Environment Variables

```bash
# Worker 활성화 여부
WORKER_ENABLED=true|false

# 동시 실행 작업 수 (레플리카당)
WORKER_CONCURRENCY=1

# Reaper 실행 주기(초)
WORKER_REAP_INTERVAL=60

# 기본 stale timeout(초)
WORKER_STALE_TIMEOUT=1200
```

### 8.2 Code Configuration

```python
store = PgJobStore(
    dsn="postgresql://user:pass@host/db",
    # 또는 pool=existing_pool
)

worker = Worker(
    store=store,
    handlers={
        "summarize": summarize_handler,
        "embed": embed_handler,
    },
    concurrency=1,
    enabled=True,
    idle_policy=IdlePollPolicy(base_seconds=1.0, max_seconds=10.0),
    backoff=BackoffPolicy(),  # 또는 custom
    reap_stale_every_seconds=60,
    default_stale_after=timedelta(minutes=20),
    worker_id="custom-id"  # 기본: hostname-pid
)
```

---

## 9. Implementation Decisions

구현에 적용된 핵심 결정사항입니다. (상세 근거는 [Decisions](decisions.md) 참조)

### 9.1 Connection Pool Management
- **결정**: DSN 및 기존 Pool 모두 지원
- **이유**: 유연성 최대화 - 간단한 사용과 효율적 공유 모두 가능
```python
# 옵션 A: 자동 pool 생성
store = PgJobStore(dsn="postgresql://...")

# 옵션 B: 기존 pool 재사용
store = PgJobStore(pool=existing_pool)
```

### 9.2 Handler Result Handling
- **결정**: Handler가 dict 반환 가능
- **이유**: 관측성 향상, 결과 추적 용이
```python
Handler = Callable[[JobContext], Awaitable[dict | None]]

# None 반환 시 jobs.result는 NULL
# dict 반환 시 자동으로 jobs.result에 저장
```

### 9.3 Transaction Provision
- **결정**: On-demand 방식 (store.transaction())
- **이유**: 필요할 때만 사용, Context 오염 방지
```python
async def handler(ctx: JobContext):
    async with ctx.store.transaction() as conn:
        await conn.execute("UPDATE ...")
```

### 9.4 Logging Strategy
- **결정**: 라이브러리 자체 logger 제공
- **이유**: 즉시 사용 가능, 표준 logging 모듈 활용
```python
import logging
logger = logging.getLogger("pgjobq")
# 앱에서 설정 가능: logging.getLogger("pgjobq").setLevel(...)
```

### 9.5 Shutdown Strategy
- **결정**: bounded cancellation + reaper recovery
- **이유**: 종료 지연을 제한하면서 장애 복구 가능
- **구현**: lifespan finally에서 stop_event 설정 후 task cancel, 최대 30초 대기

### 9.6 Dedupe Conflict Behavior
- **결정**: 기존 job_id 반환
- **이유**: 중복 감지 및 추적 가능
```sql
-- ON CONFLICT (dedupe_key) DO UPDATE SET updated_at=now()
-- RETURNING id;  -- 기존 또는 새 job_id 반환
```

### 9.7 Job Cleanup Strategy
- **결정**: 예시 SQL 제공, 구현은 사용자 책임
- **이유**: 정책이 앱마다 다름 (보관 기간, 조건 등)
- **제공**: `examples/cleanup.sql`

### 9.8 Error Serialization
- **결정**: Traceback 포함 (길이 제한 10KB)
- **이유**: 디버깅 편의성
```python
last_error = traceback.format_exc()[:10000]
```

### 9.9 Additional Decisions
- **Type Hints**: Moderate (mypy 기본 모드)
- **Python Version**: 3.10+
- **Testing**: pytest + pytest-asyncio
- **Documentation**: README.md (초기), 필요시 확장

---

## 10. Security Considerations

### 10.1 Payload Security
- `payload`는 jsonb로 저장 → SQL injection 안전
- 민감정보 저장 금지 (암호화 미지원)
- 필요시 애플리케이션 레벨에서 암호화 후 저장

### 10.2 Handler Isolation
- Handler는 trust boundary 내부
- Payload validation은 handler 책임
- Untrusted input은 enqueue 전에 검증

### 10.3 Database Permissions
```sql
-- Worker/API 계정에 필요한 최소 권한
GRANT SELECT, INSERT, UPDATE ON jobs TO app_user;
GRANT USAGE ON SEQUENCE jobs_id_seq TO app_user;
```

---

## 11. Testing Strategy

### 11.1 Unit Tests
- `PgJobStore` 각 메서드 (enqueue, pickup, mark_*)
- `BackoffPolicy` / `IdlePollPolicy` 로직
- `Worker` lifecycle (start, stop, cancel)

### 11.2 Integration Tests
- 다중 워커 동시성 (SKIP LOCKED 검증)
- Retry/backoff 플로우
- Stale recovery
- Handler chain

### 11.3 Load Tests
- N개 레플리카 × M concurrency
- Pickup throughput
- Dedupe 효과 확인

---

## 12. Migration Path

### 12.1 Initial Setup
```sql
-- Run migrations/001_create_jobs.sql
\i src/pgjobq/ddl.sql
```

### 12.2 Future Schema Changes
```sql
-- migrations/002_add_new_field.sql (example)
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS new_field text;
```

**원칙**:

- SQL 파일 기반 (ORM migration 비의존)
- Backward-compatible 변경 우선
- 프로덕션 적용 전 staging 검증

---

## 13. Performance Characteristics

### 13.1 Throughput
- **Pickup**: O(1) with index (idx_jobs_pick)
- **Enqueue**: O(1) insert
- **Dedupe check**: O(1) unique index lookup

### 13.2 Scaling Limits
- **단일 DB 처리량**: ~1000 jobs/sec (pickup + mark)
- **Bottleneck**: PostgreSQL connection pool
- **권장**: Worker 분리 후 connection pool 조정

### 13.3 Storage Growth
- 완료된 작업 정리 필요 (별도 cleanup job)
- Partition by created_at (선택)

---

## 14. Comparison with Alternatives

| Feature | pgjobq | Celery | TaskIQ | RQ |
|---------|--------|--------|--------|-----|
| Backend | PostgreSQL | Redis/RabbitMQ | Redis/etc | Redis |
| Async/Await | ✅ | ⚠️ (제한적) | ✅ | ❌ |
| FastAPI 통합 | ✅ Native | ⚠️ 별도 프로세스 | ✅ | ⚠️ |
| At-least-once | ✅ | ✅ | ✅ | ✅ |
| Exactly-once | ❌ | ❌ | ❌ | ❌ |
| Complexity | Low | High | Medium | Low |
| 추가 인프라 | ❌ | ✅ | ✅ | ✅ |

**pgjobq 선택 이유**:
- 이미 PostgreSQL 사용 중
- 단순성 > 복잡한 기능
- FastAPI async 네이티브 통합

---

## 15. Future Enhancements (Out of Scope)

### Phase 2 (고려 중)
- [ ] Job priority queue per type
- [ ] Dead letter queue (DLQ)
- [ ] Job cancellation (soft/hard)
- [ ] Periodic jobs (cron-like, without pg_cron)

### Phase 3 (연구 필요)
- [ ] Workflow engine (DAG)
- [ ] Job dependencies
- [ ] Distributed tracing integration
- [ ] Prometheus metrics exporter

---

## 16. References

- [PostgreSQL SKIP LOCKED](https://www.postgresql.org/docs/current/sql-select.html#SQL-FOR-UPDATE-SHARE)
- [FastAPI Lifespan Events](https://fastapi.tiangolo.com/advanced/events/)
- [asyncpg Documentation](https://magicstack.github.io/asyncpg/current/)