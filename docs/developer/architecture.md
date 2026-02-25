---
icon: lucide/network
---

# Architecture & Data Flow

PostgreSQL 하나만으로 동작하는 **async Python 잡 큐** 라이브러리.
FastAPI `lifespan`에 붙여 쓰는 것을 주요 패턴으로 설계되었으며, 별도의 Redis/RabbitMQ 등 추가 인프라가 필요 없다.

---

## 1. 패키지 구조

```
src/pqrun/
├── __init__.py        # 공개 API 노출 (re-export)
├── models.py          # 데이터 모델 (Job, JobContext, Handler 타입)
├── store_asyncpg.py   # DB 연산 레이어 (PgJobStore)
├── worker.py          # 잡 소비 루프 (Worker)
├── backoff.py         # 재시도/폴링 정책
└── ddl.sql            # DB 스키마 (jobs 테이블 + 인덱스)
```

---

## 2. 컴포넌트 관계

각 컴포넌트의 역할과 의존 방향을 나타낸다.

```mermaid
graph LR
    APP["FastAPI App"]
    W["Worker"]
    S["PgJobStore"]
    BP["Backoff<br/>Policies"]
    M["Job / JobContext"]
    DB[("PostgreSQL")]

    APP -->|lifespan| W
    W -->|"pickup / mark_*<br/>reap_stale"| S
    W -->|delay 계산| BP
    S -->|asyncpg| DB
    S -->|Job| M
    W -->|JobContext| M
```

---

## 3. job lifecycle

```mermaid
flowchart LR
    NEW([start])
    READY[READY]
    RUNNING[RUNNING]
    DONE([DONE ✓])
    FAILED([FAILED ✗])
    CANCELLED([CANCELLED])
    END([end])

    NEW     -->|enqueue| READY
    READY   -->|pickup| RUNNING

    RUNNING -->|success| DONE
    RUNNING -->|"retry exhausted<br/>(attempts >= max)"| FAILED
    RUNNING -->|cancel| CANCELLED
    READY   -->|cancel| CANCELLED

    RUNNING -->|"retry possible<br/>(attempts < max)"| READY
    RUNNING -. "timeout<br/>(reap_stale)" .-> READY

    DONE      --> END
    FAILED    --> END
    CANCELLED --> END
```

---

## 4. job enqueue

`dedupe_key`가 있으면 READY/RUNNING 중 중복 삽입을 DB 레벨에서 방지한다.

```mermaid
sequenceDiagram
    participant P as Producer
    participant S as PgJobStore
    participant DB as PostgreSQL

    P->>S: enqueue(job_type, payload, dedupe_key?)
    S->>DB: INSERT ... ON CONFLICT DO UPDATE
    DB-->>S: id
    S-->>P: job_id
```

---

## 5. job execution (pickup → handler → complete)

```mermaid
sequenceDiagram
    participant W as Worker
    participant S as PgJobStore
    participant DB as PostgreSQL
    participant H as Handler

    W->>S: pickup(worker_id)
    S->>DB: SELECT FOR UPDATE SKIP LOCKED<br/>→ UPDATE status=RUNNING
    DB-->>S: job row
    S-->>W: Job

    W->>H: handler(JobContext)
    H-->>W: result / exception

    alt 성공
        W->>S: mark_done(result)
        S->>DB: status=DONE
    else 재시도 가능
        W->>S: mark_error(retry_after)
        S->>DB: status=READY, run_after+=delay
    else 재시도 소진
        W->>S: mark_error(terminal=True)
        S->>DB: status=FAILED
    end
```

---

## 6. worker loop structure

### 6-1. lifespan

```mermaid
flowchart LR
    A([시작]) --> B[store.start]
    B --> C["_run_loop × N"]
    B --> D["_reaper_loop × 1"]
    C --> Y([앱 실행 중])
    D --> Y
    Y --> E([stop])
    E --> F["태스크 cancel<br/>30s 대기"]
    F --> G[store.close]
```

### 6-2. _run_loop

```mermaid
flowchart TD
    ST([시작]) --> P[pickup]
    P --> Q{job?}
    Q -- No  --> I["idle sleep<br/>1→2→5→10s"]
    I --> P
    Q -- Yes --> R[_dispatch]
    R --> V{성공?}
    V -- Yes --> D[mark_done]
    V -- No  --> E["mark_error<br/>+ backoff delay"]
    D --> P
    E --> P
```

### 6-3. _reaper_loop

```mermaid
flowchart TD
    ST([시작]) --> W["sleep 60s"]
    W --> R["reap_stale()<br/>RUNNING + 타임아웃 초과"]
    R --> U[→ READY 복귀]
    U --> W
```

---

## 7. multi-worker concurrency (SKIP LOCKED)

```mermaid
sequenceDiagram
    participant W1 as Worker-1
    participant W2 as Worker-2
    participant DB as PostgreSQL

    Note over DB: id=1,2,3 READY

    par
        W1->>DB: pickup (SKIP LOCKED)
    and
        W2->>DB: pickup (SKIP LOCKED)
    end

    DB-->>W1: id=1 → RUNNING
    DB-->>W2: id=2 → RUNNING (id=1 skip)

    W1->>DB: mark_done(id=1)
    W2->>DB: mark_done(id=2)
```

---

## 8. jobs 테이블 주요 컬럼

| 컬럼 | 타입 | 역할 |
|---|---|---|
| `job_type` | text | 핸들러 라우팅 키 |
| `payload` | jsonb | 입력 데이터 |
| `status` | job_status | 상태 머신 |
| `priority` | int | 높을수록 먼저 실행 |
| `run_after` | timestamptz | 실행 가능 최소 시각 (지연/재시도) |
| `dedupe_key` | text | READY/RUNNING 중 유일 (중복 방지) |
| `locked_by` / `locked_at` | text / timestamptz | 현재 점유 중인 워커 |
| `attempts` / `max_attempts` | int | 재시도 카운터 |
| `last_error` | text | 마지막 실패 traceback |
| `result` | jsonb | 핸들러 반환값 |
| `duration_ms` | int | 실행 소요 시간 |

---

## 9. 주요 설계 결정

| 결정 | 이유 |
|---|---|
| `FOR UPDATE SKIP LOCKED` | 워커 간 경쟁 없이 원자적 픽업 보장 |
| `ON CONFLICT DO UPDATE` | DB 레벨 dedupe — 애플리케이션 코드 불필요 |
| `attempts >= max_attempts` 체크를 DB에서 | 워커 재시작 등 경쟁 상황에서도 정확한 카운트 |
| asyncpg (ORM 없음) | 바이너리 프로토콜, asyncio 네이티브, 의존성 최소화 |
| Pool 외부 주입 지원 | FastAPI 앱과 커넥션 풀 공유 가능 |
| reaper loop 분리 | 워커 크래시로 인한 좀비 잡을 별도 루프에서 복구 |
