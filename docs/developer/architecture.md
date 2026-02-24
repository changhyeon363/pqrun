# pgjobq 아키텍처 및 데이터 흐름

PostgreSQL 하나만으로 동작하는 **async Python 잡 큐** 라이브러리.
FastAPI `lifespan`에 붙여 쓰는 것을 주요 패턴으로 설계되었으며, 별도의 Redis/RabbitMQ 등 추가 인프라가 필요 없다.

---

## 1. 패키지 구조

```
src/pgjobq/
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
    APP["FastAPI App<br/>(lifespan)"]
    W["Worker<br/>worker.py"]
    S["PgJobStore<br/>store_asyncpg.py"]
    BP["BackoffPolicy<br/>IdlePollPolicy<br/>backoff.py"]
    M["Job / JobContext<br/>models.py"]
    DB[("PostgreSQL<br/>jobs 테이블<br/>ddl.sql")]

    APP -->|lifespan 연결| W
    W -->|"pickup / mark_done<br/>mark_error / reap_stale"| S
    W -->|retry delay 계산| BP
    W -->|idle sleep 계산| BP
    S -->|asyncpg pool| DB
    S -->|Job 객체 반환| M
    W -->|JobContext 생성| M
```

---

## 3. 잡 생명주기

잡이 거치는 상태와 각 전이의 원인을 나타낸다.

```mermaid
graph TD
    NEW(["[생성]"])
    READY(["READY<br/>대기 중"])
    RUNNING(["RUNNING<br/>실행 중"])
    DONE(["DONE<br/>완료 ✓"])
    FAILED(["FAILED<br/>최종 실패 ✗"])
    CANCELLED(["CANCELLED<br/>취소"])
    END(["[종료]"])

    NEW      -->|"enqueue()"| READY
    READY    -->|"pickup()<br/>SKIP LOCKED"| RUNNING

    RUNNING  -->|"mark_done()"| DONE
    RUNNING  -->|"mark_error()<br/>attempts &lt; max_attempts<br/>run_after 딜레이 후 재시도"| READY
    RUNNING  -->|"mark_error()<br/>attempts &gt;= max_attempts<br/>또는 terminal=True"| FAILED
    RUNNING  -->|"reap_stale()<br/>타임아웃 초과 (워커 크래시)"| READY

    READY    -->|"cancel()"| CANCELLED
    RUNNING  -->|"cancel()"| CANCELLED

    DONE      --> END
    FAILED    --> END
    CANCELLED --> END
```

---

## 4. 잡 등록 (enqueue)

Producer가 잡을 등록하는 흐름. dedupe_key가 있으면 중복 잡을 DB 레벨에서 방지한다.

```mermaid
sequenceDiagram
    participant P as Producer (API Route)
    participant S as PgJobStore
    participant DB as PostgreSQL

    P->>S: enqueue(job_type, payload, dedupe_key?)
    S->>DB: INSERT INTO jobs<br/>ON CONFLICT (dedupe_key) DO UPDATE SET updated_at=now()
    DB-->>S: RETURNING id
    S-->>P: job_id
```

---

## 5. 잡 실행 (pickup → dispatch → complete)

Worker가 잡을 가져와 핸들러를 실행하고 결과를 기록하는 흐름.

```mermaid
sequenceDiagram
    participant W as Worker._run_loop
    participant S as PgJobStore
    participant DB as PostgreSQL
    participant H as Handler

    W->>S: pickup(worker_id)
    S->>DB: SELECT ... FOR UPDATE SKIP LOCKED<br/>UPDATE status=RUNNING, attempts+1
    DB-->>S: job row
    S-->>W: Job

    W->>H: handler(JobContext)
    H-->>W: result dict (또는 exception)

    alt 성공
        W->>S: mark_done(job_id, result, duration_ms)
        S->>DB: UPDATE status=DONE
    else 에러 — 재시도 가능
        W->>S: mark_error(job_id, error, retry_after)
        S->>DB: UPDATE status=READY, run_after=now()+delay
    else 에러 — 재시도 소진
        W->>S: mark_error(job_id, error, terminal=True)
        S->>DB: UPDATE status=FAILED
    end
```

---

## 6. Worker 루프 구조

Worker lifespan 시작/종료와 내부 루프 동작을 분리해서 나타낸다.

### 6-1. Lifespan (시작 및 종료)

```mermaid
flowchart LR
    A([lifespan 진입]) --> B["store.start()<br/>asyncpg Pool 생성"]
    B --> C["_run_loop 태스크<br/>× concurrency 개 생성"]
    B --> D["_reaper_loop 태스크<br/>× 1 생성"]
    C --> Y(["yield<br/>앱 실행 중"])
    D --> Y
    Y --> E([stop_event.set])
    E --> F["모든 태스크 cancel()<br/>최대 30초 대기"]
    F --> G["store.close()"]
```

### 6-2. _run_loop (잡 소비)

```mermaid
flowchart TD
    S([시작]) --> P[pickup]
    P --> Q{job 있음?}
    Q -- No  --> I["empty_streak++<br/>IdlePollPolicy 대기<br/>1s → 2s → 5s → 10s"]
    I --> P
    Q -- Yes --> R["empty_streak = 0<br/>_dispatch(job)"]
    R --> V{성공?}
    V -- Yes --> D[mark_done]
    V -- No  --> E["mark_error<br/>BackoffPolicy로 retry_after 계산"]
    D --> P
    E --> P
```

### 6-3. _reaper_loop (좀비 잡 복구)

```mermaid
flowchart TD
    S([시작]) --> W["sleep(reap_stale_every_seconds)<br/>기본 60초"]
    W --> R["reap_stale()<br/>status=RUNNING이고<br/>locked_at 초과인 잡 탐색"]
    R --> U["해당 잡 → status=READY 복귀<br/>(워커 크래시 복구)"]
    U --> W
```

---

## 7. 멀티 워커 동시성 (SKIP LOCKED)

여러 워커가 동시에 `pickup()`을 호출해도 각자 다른 잡을 가져가는 원리.

```mermaid
sequenceDiagram
    participant W1 as Worker-1
    participant W2 as Worker-2
    participant DB as PostgreSQL

    Note over DB: id=1 READY / id=2 READY / id=3 READY

    par 동시 호출
        W1->>DB: SELECT FOR UPDATE SKIP LOCKED LIMIT 1
    and
        W2->>DB: SELECT FOR UPDATE SKIP LOCKED LIMIT 1
    end

    DB-->>W1: id=1 lock 획득 → RUNNING
    DB-->>W2: id=1 SKIP → id=2 lock 획득 → RUNNING

    Note over W1,W2: 각자 독립적으로 handler 실행

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
