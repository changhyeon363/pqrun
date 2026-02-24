# 🔥 pg_cron으로 할 수 있는 것들

**Date:** 2026-02-24 | **Source:** [chat.openai.com](https://chatgpt.com/c/699d813c-7100-83a8-8aa8-f0a3e0cdf1d7)

---

**You**

지금은 fastapi 가 맞는데 이걸 재사용성을 높이기 위해서 패키지화 할 수 있나? 라이브러리처럼 가져다가 쓰게


---

**ChatGPT**

가능해. 오히려 지금 설계(“DB jobs + SKIP LOCKED + handler routing”)는 패키지로 딱 빼기 좋은 형태야. 핵심은 “앱 코드와 분리되는 공통 레이어”를 만들고, 앱은 핸들러만 등록하게 하는 거야.
아래처럼 설계하면 라이브러리처럼 pip로 가져다 쓰는 구조가 된다.

### 1) 패키지로 뺄 때 목표(인터페이스)

앱에서 하고 싶은 건 이 정도만:

```python
from pgjobq import Worker, JobHandler, PgJobStore

store = PgJobStore(dsn=...)
worker = Worker(store, handlers={
  "summarize": summarize_handler,
  "embed": embed_handler,
})

app = FastAPI(lifespan=worker.lifespan)
```

즉,

- 큐 저장/픽업/재시도/락/백오프 = 라이브러리
- 실제 작업 로직 = 앱(핸들러)

### 2) 패키지 구조 예시

레포 하나를 이렇게:

```코딩
pgjobq/
  pyproject.toml
  src/pgjobq/
    __init__.py
    store.py          # Postgres 접근(픽업/상태업데이트/리퍼)
    worker.py         # 루프/동시성/백오프/라이프사이클
    handlers.py       # handler 프로토콜/registry
    models.py         # Job dataclass / enums
    migrations/
      001_jobs.sql    # DDL
  README.md
```

이렇게 해두면 다른 프로젝트에서 pip install pgjobq로 가져다 씀.

### 3) 라이브러리가 제공해야 하는 핵심 기능(최소)

#### A. Postgres JobStore

- enqueue(job_type, payload, dedupe_key=None, run_after=..., priority=...)
- pickup(worker_id)  (SKIP LOCKED)
- mark_done(job_id)
- mark_failed(job_id, error, next_run_after=None, terminal=False)
- reap_stale_running(stale_after=...)

#### B. Worker

- lifespan(app) 제공 (FastAPI에 바로 붙이기)
- WORKER_ENABLED, CONCURRENCY, POLL_INTERVAL 같은 설정
- handler registry 기반 dispatch

#### C. Handler 인터페이스

- async def handler(ctx: JobContext) -> None
- ctx에 job, logger, store, db(optional) 등을 넣어줌

### 4) 기술적으로 “패키지화”에서 제일 중요한 선택 2개

#### 1) DB 드라이버/ORM 의존성

재사용성을 높이려면 보통 둘 중 하나:
옵션 A (추천): asyncpg/psycopg만 사용 (ORM 독립)

- 라이브러리는 SQL만 실행
- 앱은 SQLAlchemy든 뭐든 자유
- 의존성 최소, 이식성 최고

옵션 B: SQLAlchemy 기반

- 편하지만 앱이 SQLAlchemy를 강제
- 너처럼 이미 SQLAlchemy 쓰면 편할 수 있음

내 추천: 옵션 A(드라이버만). 가장 “라이브러리 같다”.

#### 2) 마이그레이션 제공 방식

- migrations/001_jobs.sql 같이 SQL 파일 제공
- 또는 pgjobq.init_db() 같은 함수 제공

보통은 SQL 파일 + README가 가장 단순하고 튼튼.

### 5) “나중에 워커 분리”도 쉽게 만들 수 있음

패키지에 아래를 넣어두면 좋음:

- worker_enabled 옵션
- 동일 패키지를 api 서비스와 worker 서비스에서 동시에 사용 가능

즉, 현재는:

- API 컨테이너: WORKER_ENABLED=true

나중엔:

- API 컨테이너: false
- Worker 컨테이너: true

코드 변경 거의 0.

### 6) 현실적인 결론

- 패키지화 가능 ✅
- 오히려 지금 단계에서 패키지화하면:
  - “프로젝트마다 복붙”을 막고
  - 실수(락/재시도/복구 로직)를 줄이고
  - 나중에 작업 종류 늘어도 구조가 깔끔해짐

원하면 다음 답변에서 내가 바로 사용할 수 있는 최소 패키지 골격을 만들어줄게:

- PgJobStore (SQL 포함)
- Worker (FastAPI lifespan 포함)
- Job 모델 + handler registry
- 001_jobs.sql

그리고 너 프로젝트에선 handlers/ 폴더만 만들어서 작업 등록하면 끝.
너는 DB 접근을 지금 SQLAlchemy async로 하고 있어? 아니면 psycopg/asyncpg 직접이야? (둘 다 지원 가능하지만, 패키지 기본값을 고르는 데만 필요해.)


---

**You**

psycopg/asyncpg 직접이 라이브러리 관점에서는 좋겠다


---

**ChatGPT**

좋아. **라이브러리 관점에서는 “ORM/프레임워크 비의존 + asyncpg(핵심) + psycopg(옵션)”**이 제일 재사용성 좋아.
아래는 내가 추천하는 **패키지 설계(최소 코어)**야. 이대로 만들면 다른 FastAPI 프로젝트에서 pip install -e .로 가져다 쓰는 형태가 된다.

## 1) 패키지 목표 API (사용자 입장)

```python
from pgjobq import PgJobStore, Worker

store = PgJobStore(dsn=POSTGRES_DSN)
worker = Worker(
    store=store,
    handlers={"summarize": summarize_handler, "embed": embed_handler},
    concurrency=1,
)

app = FastAPI(lifespan=worker.lifespan)
```

- 앱은 핸들러만 작성/등록
- 큐/락/재시도/백오프/리퍼는 패키지가 처리

## 2) 디렉토리 구조 (표준)

```코딩
pgjobq/
  pyproject.toml
  src/pgjobq/
    __init__.py
    models.py        # Job dataclass, status enum
    store_asyncpg.py # Postgres 큐 핵심 (asyncpg)
    worker.py        # poll loop + FastAPI lifespan
    ddl.sql          # jobs 테이블 DDL
  README.md
```

## 3) DB 스키마(범용) — ddl.sql

(요약 특화 X)

```sql
-- jobs (generic)
DO $$ BEGIN
  CREATE TYPE job_status AS ENUM ('READY','RUNNING','DONE','FAILED','CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY,
  job_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  status job_status NOT NULL DEFAULT 'READY',
  priority int NOT NULL DEFAULT 0,

  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),

  locked_at timestamptz,
  locked_by text,

  dedupe_key text,
  last_error text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_pick
ON jobs (status, run_after, priority DESC, id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_dedupe
ON jobs (dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('READY','RUNNING');
```

## 4) asyncpg 기반 JobStore (핵심 아이디어)

- 패키지는 SQL만 실행 (ORM 없음)
- pickup()은 SKIP LOCKED로 안전 소비
- enqueue()는 dedupe optional

(코드는 다음 단계에서 파일 단위로 “그대로 복붙 가능”하게 뽑아줄게. 여기선 형태만.)
필수 메서드:

- enqueue(job_type, payload, dedupe_key=None, run_after=None, priority=0, max_attempts=5)
- pickup(worker_id) → job 1개 가져오기
- mark_done(job_id)
- mark_error(job_id, error, next_run_after, terminal=False)
- reap_stale(stale_after=...)

## 5) Worker (FastAPI lifespan 제공)

Worker가 하는 일:

- WORKER_ENABLED면 background loop 실행
- 루프:
  1. pickup()
  2. handlers[job.job_type](ctx)
  3. 성공/실패 업데이트
- concurrency=1 같은 제한도 여기서

핵심 옵션:

- concurrency (레플리카당 동시 실행 수)
- poll_interval (기본 1~3초)
- idle_backoff (job 없을 때 점점 sleep 늘리기)
- worker_id (hostname/pod id 등)

## 6) psycopg는 어떻게?

라이브러리 코어는 asyncpg 1개만 들고 가는 게 제일 깔끔해.

- 필요하면 나중에 store_psycopg.py를 추가해서 “동일 인터페이스”로 교체 가능
- 하지만 FastAPI async 기준이면 asyncpg만으로 충분한 경우가 대부분

## 7) 이게 “기술부채”냐?

오히려 반대에 가까워.

- 라이브러리로 빼면 “큐 소비/락/재시도” 실수를 한 군데에서만 고치면 돼서 부채가 줄어.
- TaskIQ 같은 외부 프레임워크보다, 네 요구사항(ACA + DB큐 + pg_cron enqueue)에선 더 단순하고 예측 가능함.

### 다음 액션

원하면 다음 답변에서 내가 실제 파일 4개를 바로 줄게(그대로 패키지 생성 가능):

1. models.py
2. store_asyncpg.py (enqueue/pickup/mark/reap 전부)
3. worker.py (FastAPI lifespan + concurrency + backoff)
4. pyproject.toml

그리고 예시로 “요약 job 핸들러”는 프로젝트 쪽에서 어떻게 붙이는지도 짧게.
너 프로젝트는 DB 연결을 이미 asyncpg pool로 쓰고 있어? 아니면 “dsn만 있으면 pool을 라이브러리가 만들어도 됨”이야?
(둘 다 가능하지만, 패키지 기본 설계를 고르는 데만 필요해.)


---

**You**

잠시만 enqueue는 누가 어떻게 하는 거야?


---

**ChatGPT**

좋은 질문이야.
enqueue는 “누가 일을 만들 것인가?”에 대한 책임 분리 문제야.
범용 설계에서는 보통 3가지 방식이 있어.
(그리고 실제로는 이 3가지를 다 쓰게 된다.)

## 1️⃣ 애플리케이션 코드에서 enqueue (가장 흔함)

#### 상황

- 유저가 어떤 액션을 했다
- 이벤트가 발생했다
- 특정 조건이 충족됐다

👉 그 순간 바로 enqueue

#### 예시

```python
await store.enqueue(
    job_type="summarize_conversation",
    payload={"conversation_id": 123},
    dedupe_key="summarize:conversation:123",
)
```

#### 언제 쓰냐?

- 요청 후 비동기 처리
- 이벤트 기반 작업
- 즉시 처리 예약

## 2️⃣ 스케줄러(pg_cron)에서 enqueue (주기 작업)

#### 상황

- 매 5분마다 점검
- 오래된 데이터 정리
- 배치 생성

👉 DB 함수가 jobs 테이블에 insert

```sql
INSERT INTO jobs (job_type, payload, dedupe_key)
SELECT ...
ON CONFLICT DO NOTHING;
```

#### 특징

- 처리 자체는 안 함
- “할 일만 생성”
- Worker가 실제 실행

## 3️⃣ 다른 Job이 enqueue (워크플로우 체인)

#### 상황

- A 작업이 끝난 뒤 B 작업 필요
- 분산 단계 처리

예:

```python
async def summarize_handler(ctx):
    ...
    await ctx.store.enqueue(
        job_type="embed_summary",
        payload={"conversation_id": cid},
    )
```

이걸로 간단한 workflow도 구성 가능.

## 🔥 그래서 누가 하는 게 맞냐?

너 시스템에서는 보통 이렇게 될 거야:

#### 🔹 요약 같은 배치성 작업

→ pg_cron이 enqueue

#### 🔹 사용자 액션 기반 작업

→ API 코드에서 enqueue

#### 🔹 후속 처리

→ handler 안에서 enqueue

## 📌 중요한 설계 원칙

#### enqueue는 항상 "INSERT ONLY"

- 실행하지 않는다
- 계산하지 않는다
- 큐에 넣기만 한다

#### Worker만 실행한다

- pickup
- handler 실행
- 상태 업데이트

이 원칙을 지키면 구조가 절대 꼬이지 않아.

## 💡 지금 너 케이스에 적용하면

#### 대화 요약은:

- pg_cron이 주기적으로 “요약 필요 대화”를 찾아 enqueue

#### 나중에:

- 사용자가 “리포트 생성” 버튼 누르면 → API가 enqueue
- 요약 완료되면 → handler가 embed 작업 enqueue

## 🎯 결론

enqueue는:
상황누가 함이벤트 기반API 코드주기 작업pg_cron후속 처리handler

원하면 다음 단계로:
👉 “enqueue를 라이브러리 안에서 어떻게 안전하게 구현할지”
👉 “pg_cron enqueue 함수는 범용적으로 어떻게 설계할지”
👉 “REST endpoint로 enqueue API를 노출할지 여부”
어디부터 더 파볼까?


---

**You**

아 그러면 3개다 지원하면 되겠다. 자 차근차근 라이브러리 설계해보자


---

**ChatGPT**

좋아. enqueue 3가지(앱/스케줄러/핸들러 체인) 전부 지원하는 “DB Job Queue 라이브러리”를 차근차근 설계하자.
아래는 1) 요구사항 → 2) public API → 3) 내부 구성 → 4) DB 스키마/SQL 계약 순서로 정리한 1차 설계안이야.

## 1) 라이브러리 목표와 원칙

### 목표

- Postgres 기반 범용 작업 큐
- FastAPI에서 API 컨테이너가 워커 겸업 가능
- 나중에 워커 컨테이너 분리 쉬움
- enqueue는 3가지 모두 지원
  1. 앱 코드에서 enqueue
  2. pg_cron이 DB 함수/SQL로 enqueue
  3. 핸들러에서 후속 작업 enqueue

### 원칙

- at-least-once (중복 실행 가능) 전제 → handler는 가능하면 idempotent 권장
- 락/경합은 DB가 해결: FOR UPDATE SKIP LOCKED
- 스케줄러는 “처리” 금지, INSERT ONLY

## 2) Public API (사용자가 보게 될 인터페이스)

### 2-1) core: PgJobStore

- DB와 직접 통신하는 계층

```python
class PgJobStore:
    async def enqueue(self, job_type: str, payload: dict,
                      *, dedupe_key: str | None = None,
                      run_after: datetime | None = None,
                      priority: int = 0,
                      max_attempts: int = 5) -> int: ...

    async def pickup(self, *, worker_id: str) -> Job | None: ...

    async def mark_done(self, job_id: int) -> None: ...

    async def mark_error(self, job_id: int, error: str,
                         *, retry_after: timedelta | None = None,
                         terminal: bool = False) -> None: ...

    async def reap_stale(self, *, stale_after: timedelta) -> int: ...
```

### 2-2) worker: Worker

- FastAPI lifespan 붙일 수 있게

```python
class Worker:
    def __init__(..., handlers: dict[str, Handler], concurrency: int = 1, ...):
        ...

    async def lifespan(self, app): ...
```

### 2-3) handler 인터페이스 + context

```python
Handler = Callable[[JobContext], Awaitable[None]]

@dataclass
class JobContext:
    job: Job
    store: PgJobStore
    worker_id: str
    # logger, metrics 등도 옵션으로
```

✅ 이렇게 하면 **핸들러 내부에서 ctx.store.enqueue()**로 체인도 자연스럽게 됨.

## 3) 내부 구성(모듈)

```코딩
pgjobq/
  models.py        # Job dataclass, Status enum
  store_asyncpg.py # SQL 실행 + 트랜잭션
  worker.py        # poll loop + semaphore + backoff
  backoff.py       # 재시도 지연 계산(기본 제공)
  ddl.sql          # DB 생성 스크립트
```

#### 왜 backoff.py를 따로?

- 앱들이 매번 재시도 정책을 다시 짜지 않게
- 기본 정책 제공 + 오버라이드 가능

## 4) DB 스키마 계약(범용)

필드 의미를 “계약”으로 고정해야 라이브러리가 안정적이야.

- job_type: 라우팅 키 (string)
- payload: jsonb (입력)
- status: READY/RUNNING/DONE/FAILED/CANCELLED
- attempts/max_attempts: 재시도 제어
- run_after: 스케줄/지연/백오프
- locked_at/locked_by: 워커가 집어간 표시
- dedupe_key: 활성 job 중복 방지 옵션

## 5) SQL 계약(핵심 3개)

### 5-1) enqueue (dedupe optional)

- dedupe_key가 있으면 활성 중복 방지 인덱스에 걸림
- 없으면 그냥 무한히 쌓일 수 있음

### 5-2) pickup (SKIP LOCKED)

- 레플리카 수와 무관하게 안전 소비

### 5-3) mark_error

- attempts 기준으로 READY로 돌리거나 FAILED로 종결

## 6) 라이브러리가 지원해야 하는 “enqueue 3종”

#### A) 앱 enqueue

store.enqueue(...)

#### B) pg_cron enqueue

- 라이브러리는 SQL 파일/문서로 안내
- 또는 pgjobq.ddl.sql에 “예시 함수 템플릿” 제공(옵션)

#### C) handler 체인 enqueue

ctx.store.enqueue(...) (동일)

## 7) 다음 단계(이제 구현으로 들어갈 차례)

다음 메시지에서 내가 바로 “실제 코드 골격”을 줄게:

1. models.py (Job dataclass)
2. ddl.sql
3. store_asyncpg.py (enqueue/pickup/mark/reap)
4. worker.py (FastAPI lifespan + concurrency + backoff)

### 시작을 위해 딱 3개만 기본값을 정하자(네가 안 정해도 내가 디폴트로 박을 수 있음)

1. max_attempts 기본: 5로 갈까?
2. concurrency 기본: API 겸업이니 1로 갈까?
3. “job 없을 때” poll 전략: 1초 → 2 → 5초로 늘리는 backoff 넣을까?

원하면 지금 바로 **디폴트(5,1,backoff)**로 확정하고 코드 골격을 이어서 줄게.


---

**You**

디폴트로 확정하고 코드 골격 이어서 줘


---

**ChatGPT**

좋아. 디폴트( max_attempts=5, concurrency=1, idle poll backoff )로 확정하고, 바로 “패키지 골격 + 핵심 코드”를 이어서 줄게.
아래 파일들 그대로 만들면 pgjobq를 라이브러리처럼 가져다 쓸 수 있어.

### 0) 폴더 구조

```plain text
pgjobq/
  pyproject.toml
  README.md
  src/pgjobq/
    __init__.py
    models.py
    backoff.py
    ddl.sql
    store_asyncpg.py
    worker.py
```

### 1) pyproject.toml

```toml
[project]
name = "pgjobq"
version = "0.1.0"
description = "PostgreSQL-backed job queue for async Python (FastAPI-friendly)"
requires-python = ">=3.10"
dependencies = [
  "asyncpg>=0.29.0",
]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
package-dir = {"" = "src"}

[tool.setuptools.package-data]
pgjobq = ["ddl.sql"]
```

### 2) src/pgjobq/__init__.py

```python
from .models import Job, JobStatus, JobContext
from .store_asyncpg import PgJobStore
from .worker import Worker

__all__ = ["Job", "JobStatus", "JobContext", "PgJobStore", "Worker"]
```

### 3) src/pgjobq/models.py

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Awaitable, Mapping, Optional

class JobStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

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
    locked_at: Optional[datetime]
    locked_by: Optional[str]
    dedupe_key: Optional[str]
    last_error: Optional[str]
    created_at: datetime
    updated_at: datetime

@dataclass(frozen=True)
class JobContext:
    job: Job
    store: Any  # PgJobStore (avoid circular import for skeleton)
    worker_id: str

Handler = Callable[[JobContext], Awaitable[None]]
Handlers = Mapping[str, Handler]
```

### 4) src/pgjobq/backoff.py

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

@dataclass(frozen=True)
class BackoffPolicy:
    """
    Default retry delays:
      attempt 1 -> 1m
      attempt 2 -> 5m
      attempt 3 -> 30m
      attempt 4 -> 2h
      attempt 5+ -> 6h
    """
    def retry_delay(self, attempts: int) -> timedelta:
        if attempts <= 1:
            return timedelta(minutes=1)
        if attempts == 2:
            return timedelta(minutes=5)
        if attempts == 3:
            return timedelta(minutes=30)
        if attempts == 4:
            return timedelta(hours=2)
        return timedelta(hours=6)

@dataclass(frozen=True)
class IdlePollPolicy:
    """
    When no jobs are found, increase sleep up to max.
    Default sequence: 1s -> 2s -> 5s -> 10s (cap)
    """
    base_seconds: float = 1.0
    max_seconds: float = 10.0

    def next_sleep(self, empty_streak: int) -> float:
        if empty_streak <= 0:
            return self.base_seconds
        if empty_streak == 1:
            return min(2.0, self.max_seconds)
        if empty_streak == 2:
            return min(5.0, self.max_seconds)
        return self.max_seconds
```

### 5) src/pgjobq/ddl.sql

```sql
DO $$ BEGIN
  CREATE TYPE job_status AS ENUM ('READY','RUNNING','DONE','FAILED','CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY,

  job_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  status job_status NOT NULL DEFAULT 'READY',
  priority int NOT NULL DEFAULT 0,

  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),

  locked_at timestamptz,
  locked_by text,

  dedupe_key text,
  last_error text,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_pick
ON jobs (status, run_after, priority DESC, id);

CREATE INDEX IF NOT EXISTS idx_jobs_type
ON jobs (job_type);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_dedupe
ON jobs (dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('READY','RUNNING');
```

### 6) src/pgjobq/store_asyncpg.py

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import asyncpg

from .models import Job, JobStatus

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class PgJobStore:
    """
    asyncpg-based job store.

    Notes:
    - You can either pass a DSN and let store manage a pool, or pass an existing pool.
    - All operations are SQL-based (ORM-free) for reusability.
    """
    dsn: Optional[str] = None
    pool: Optional[asyncpg.Pool] = None

    async def start(self) -> None:
        if self.pool is None:
            if not self.dsn:
                raise ValueError("PgJobStore requires either dsn or pool")
            self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        priority: int = 0,
        max_attempts: int = 5,
    ) -> int:
        """
        Insert a job. If dedupe_key is provided, active duplicates are prevented by partial unique index.
        Returns job id of the inserted row, or 0 if skipped by ON CONFLICT (deduped).
        """
        assert self.pool is not None, "call start() first"
        ra = run_after or _utcnow()

        sql = """
        INSERT INTO jobs (job_type, payload, dedupe_key, run_after, priority, max_attempts)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6)
        ON CONFLICT DO NOTHING
        RETURNING id;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, job_type, payload, dedupe_key, ra, priority, max_attempts)
            return int(row["id"]) if row else 0

    async def pickup(self, *, worker_id: str) -> Job | None:
        """
        Atomically pick a single READY job using SKIP LOCKED and mark it RUNNING.
        Safe with multiple replicas.
        """
        assert self.pool is not None, "call start() first"

        sql = """
        WITH picked AS (
          SELECT id
          FROM jobs
          WHERE status='READY'
            AND run_after <= now()
          ORDER BY priority DESC, id
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        UPDATE jobs j
        SET status='RUNNING',
            locked_at=now(),
            locked_by=$1,
            attempts = attempts + 1,
            updated_at=now()
        FROM picked
        WHERE j.id = picked.id
        RETURNING
          j.id, j.job_type, j.payload, j.status, j.priority, j.attempts, j.max_attempts,
          j.run_after, j.locked_at, j.locked_by, j.dedupe_key, j.last_error, j.created_at, j.updated_at;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, worker_id)
            if not row:
                return None
            return _row_to_job(row)

    async def mark_done(self, job_id: int) -> None:
        assert self.pool is not None, "call start() first"
        sql = "UPDATE jobs SET status='DONE', updated_at=now() WHERE id=$1;"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id)

    async def mark_error(
        self,
        job_id: int,
        error: str,
        *,
        retry_after: timedelta | None = None,
        terminal: bool = False,
    ) -> None:
        """
        If terminal=True -> FAILED immediately.
        Else -> READY with run_after bumped (if attempts < max_attempts), or FAILED if attempts exceeded.
        """
        assert self.pool is not None, "call start() first"
        delay = retry_after or timedelta(minutes=1)

        sql = """
        UPDATE jobs
        SET
          last_error = $2,
          updated_at = now(),
          status = CASE
            WHEN $3::bool THEN 'FAILED'
            WHEN attempts >= max_attempts THEN 'FAILED'
            ELSE 'READY'
          END,
          run_after = CASE
            WHEN $3::bool THEN run_after
            WHEN attempts >= max_attempts THEN run_after
            ELSE now() + $4::interval
          END,
          locked_at = NULL,
          locked_by = NULL
        WHERE id = $1;
        """
        # asyncpg doesn't accept timedelta as interval directly; pass seconds and cast in SQL.
        interval_str = f"{int(delay.total_seconds())} seconds"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id, error, terminal, interval_str)

    async def cancel(self, job_id: int) -> None:
        assert self.pool is not None, "call start() first"
        sql = "UPDATE jobs SET status='CANCELLED', updated_at=now() WHERE id=$1;"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id)

    async def reap_stale(self, *, stale_after: timedelta) -> int:
        """
        Reset RUNNING jobs older than stale_after back to READY (so they can be retried).
        Returns number of rows affected.
        """
        assert self.pool is not None, "call start() first"
        seconds = int(stale_after.total_seconds())
        sql = f"""
        UPDATE jobs
        SET status='READY',
            locked_at=NULL,
            locked_by=NULL,
            updated_at=now(),
            run_after = GREATEST(run_after, now())
        WHERE status='RUNNING'
          AND locked_at IS NOT NULL
          AND locked_at < now() - interval '{seconds} seconds';
        """
        async with self.pool.acquire() as conn:
            res = await conn.execute(sql)
            # "UPDATE <n>"
            return int(res.split()[-1])

def _row_to_job(row: asyncpg.Record) -> Job:
    return Job(
        id=int(row["id"]),
        job_type=str(row["job_type"]),
        payload=dict(row["payload"]) if row["payload"] is not None else {},
        status=JobStatus(str(row["status"])),
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        run_after=row["run_after"],
        locked_at=row["locked_at"],
        locked_by=row["locked_by"],
        dedupe_key=row["dedupe_key"],
        last_error=row["last_error"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
```

### 7) src/pgjobq/worker.py (FastAPI lifespan + concurrency=1 + idle backoff)

```python
from __future__ import annotations

import asyncio
import os
import socket
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from .backoff import BackoffPolicy, IdlePollPolicy
from .models import Handlers, JobContext
from .store_asyncpg import PgJobStore

@dataclass
class Worker:
    store: PgJobStore
    handlers: Handlers

    concurrency: int = 1
    enabled: bool = True

    # polling behavior
    idle_policy: IdlePollPolicy = IdlePollPolicy()
    # retry behavior
    backoff: BackoffPolicy = BackoffPolicy()

    # stale RUNNING reap (optional)
    reap_stale_every_seconds: int = 60
    stale_after: timedelta = timedelta(minutes=20)

    worker_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.worker_id is None:
            self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        # env override (easy operational switch)
        env_enabled = os.getenv("WORKER_ENABLED")
        if env_enabled is not None:
            self.enabled = env_enabled.lower() in ("1", "true", "yes", "on")
        env_conc = os.getenv("WORKER_CONCURRENCY")
        if env_conc is not None:
            self.concurrency = max(1, int(env_conc))

    async def lifespan(self, app):
        """
        FastAPI lifespan hook. Usage:
          worker = Worker(store, handlers)
          app = FastAPI(lifespan=worker.lifespan)
        """
        await self.store.start()

        stop_event = asyncio.Event()
        tasks: list[asyncio.Task] = []

        if self.enabled:
            # worker loops
            sem = asyncio.Semaphore(self.concurrency)
            for _ in range(self.concurrency):
                tasks.append(asyncio.create_task(self._run_loop(stop_event, sem)))

            # reaper loop (optional)
            tasks.append(asyncio.create_task(self._reaper_loop(stop_event)))

        try:
            yield
        finally:
            stop_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.store.close()

    async def _reaper_loop(self, stop_event: asyncio.Event) -> None:
        # Reset stale RUNNING jobs periodically
        try:
            while not stop_event.is_set():
                await asyncio.sleep(self.reap_stale_every_seconds)
                try:
                    await self.store.reap_stale(stale_after=self.stale_after)
                except Exception:
                    # keep reaper from crashing the whole worker
                    pass
        except asyncio.CancelledError:
            return

    async def _run_loop(self, stop_event: asyncio.Event, sem: asyncio.Semaphore) -> None:
        empty_streak = 0
        try:
            while not stop_event.is_set():
                async with sem:
                    job = await self.store.pickup(worker_id=self.worker_id or "worker")
                    if job is None:
                        empty_streak += 1
                    else:
                        empty_streak = 0
                        await self._dispatch(job)

                # sleep based on emptiness
                sleep_s = self.idle_policy.next_sleep(empty_streak)
                await asyncio.sleep(sleep_s)
        except asyncio.CancelledError:
            return

    async def _dispatch(self, job) -> None:
        handler = self.handlers.get(job.job_type)
        if handler is None:
            await self.store.mark_error(
                job.id,
                f"No handler registered for job_type={job.job_type}",
                terminal=True,
            )
            return

        ctx = JobContext(job=job, store=self.store, worker_id=self.worker_id or "worker")

        try:
            await handler(ctx)
            await self.store.mark_done(job.id)
        except Exception as e:
            # compute retry delay based on attempts *after pickup() (attempts already incremented)
            delay = self.backoff.retry_delay(job.attempts)
            await self.store.mark_error(job.id, repr(e), retry_after=delay, terminal=False)
```

### 8) “사용 예시”(프로젝트 쪽)

```python
from fastapi import FastAPI
from pgjobq import PgJobStore, Worker, JobContext

async def summarize(ctx: JobContext) -> None:
    cid = ctx.job.payload["conversation_id"]
    # ... LLM 호출/DB 저장 ...
    # chain enqueue도 가능
    await ctx.store.enqueue("embed", {"conversation_id": cid}, dedupe_key=f"embed:conv:{cid}")

store = PgJobStore(dsn="postgresql://...")
worker = Worker(store=store, handlers={"summarize": summarize})

app = FastAPI(lifespan=worker.lifespan)

@app.post("/enqueue/summarize/{cid}")
async def enqueue(cid: int):
    job_id = await store.enqueue(
        "summarize",
        {"conversation_id": cid},
        dedupe_key=f"summarize:conv:{cid}",
    )
    return {"job_id": job_id}
```

### 다음 단계 (바로 이어서)

이제 “골격”은 완성됐고, 다음은 실전에 필요한 3가지 개선을 붙이면 끝이야:

1. 트랜잭션 지원(핸들러가 DB 작업할 때 conn 전달 옵션)
2. job 결과/메타(output jsonb, duration_ms 같은 컬럼)
3. job_type별 동시성 제한(예: summarize는 1개, embed는 4개)

원하면 다음 답변에서 1)부터 바로 “깔끔한 확장 설계”로 이어갈게.
지금 너는 jobs DDL을 마이그레이션 도구로 적용할 거야, 아니면 일단 SQL 직접 실행할 거야?


---

**You**

어떤 도구들이 있어?


---

**ChatGPT**

좋아 👍
지금 질문은 “jobs DDL을 어떤 마이그레이션 도구로 관리할 수 있냐?” 이거지?
FastAPI + Postgres + asyncpg 직접 사용하는 환경 기준으로 현실적인 선택지를 정리해줄게.

## 🥇 1. Alembic (가장 많이 쓰임)

#### 특징

- SQLAlchemy 진영의 표준 마이그레이션 도구
- Python 코드 기반 migration
- revision 관리 체계적
- 프로덕션에서 가장 많이 쓰임

#### 장점

- 업계 표준
- 버전 관리 확실
- downgrade 지원

#### 단점

- SQLAlchemy 기반이라 설정이 약간 무거움
- asyncpg 직접 쓰는 구조에서는 약간 결합이 생김

📌 추천 상황
→ 이미 SQLAlchemy를 쓰고 있다면 무조건 Alembic.

## 🥈 2. Flyway (Java계열이지만 DB 독립적)

#### 특징

- SQL 파일 기반
- 언어 독립적
- Docker 환경에서 많이 씀

#### 장점

- SQL만 있으면 됨
- 단순하고 안정적

#### 단점

- Python 생태계랑 직접적 연계는 없음

📌 추천 상황
→ 멀티 서비스 / 언어 혼합 환경

## 🥉 3. Liquibase

Flyway와 비슷하지만 더 무거움.
대기업/엔터프라이즈에서 많이 씀.
→ 지금 너한테는 오버스펙.

## 🥇🥇 4. simple SQL migration tool (내가 사실 추천하는 방식)

너가 지금 만드는 건:

- ORM 없음
- asyncpg 직접 사용
- DB 중심 구조

이 경우 가장 깔끔한 방식은:

#### ✅ SQL 파일 + 간단한 버전 테이블 관리

예:

```코딩
migrations/
  001_create_jobs.sql
  002_add_output_column.sql
```

그리고 DB에:

```코딩
schema_migrations
```

테이블 하나 두고, 적용된 파일 이름 기록.
이건 직접 50줄짜리 Python 코드로도 만들 수 있음.

## 🎯 현실적인 추천 (너 상황 기준)

상황추천SQLAlchemy 안 씀✅ SQL 파일 방식인프라 단순 유지✅ SQL 파일 방식대규모 팀 협업Alembic멀티 언어 서비스Flyway

## 🔥 내 추천 (너한테 가장 깔끔한 방법)

#### pgjobq 패키지에

- ddl.sql 제공
- README에 “이 SQL을 migration에 포함하세요” 명시

#### 프로젝트에서는

- migrations/001_pgjobq.sql로 복사해서 관리

이게 제일 단순하고 안정적이야.

## 💬 정리

Alembic 쓰면 무겁고
Flyway는 과하고
Liquibase는 더 과하고
지금 구조에선

SQL 파일 기반 migration이 가장 깔끔하고 기술부채 적음

원하면 내가:
1️⃣ 50줄짜리 “초경량 migration runner” 만들어줄까?
2️⃣ 아니면 Alembic으로 가는 구조로 정리해줄까?
너가 앞으로 서비스 확장 방향이 어떤지에 따라 추천이 달라질 수 있어.


---

**You**

그래 너 말대로 단순하게 가자. 이것 외에 더 작업해야 할 부분이 있을까? 아니면 일단 이정도면 충분할까?


---

**ChatGPT**

지금 단계 기준으로는 **“충분히 출발 가능”**해.
다만 실제 운영에서 바로 부딪히는 필수 5개만 더 얹으면, “나중에 손볼 일(기술부채)”이 확 줄어.
아래는 우선순위 순이야.

### 꼭(1) jobs에 finished_at, duration_ms 같은 관측 필드

지금은 DONE/FAILED만 남아서 “얼마나 걸렸는지/언제 끝났는지”가 안 보여.

- started_at(=locked_at로 대체 가능), finished_at
- duration_ms
- (선택) result jsonb / output jsonb

최소는 finished_at 하나만 있어도 운영이 편해져.

### 꼭(2) “긴 작업”을 위한 stale 복구 기준을 job별로

지금 reaper가 20분 고정인데,

- 요약은 5분, 다른 작업은 2시간 걸릴 수도 있어.

해결:

- jobs에 timeout_seconds 추가
- reaper가 locked_at + timeout_seconds 기준으로 복구

### 꼭(3) 워커가 API 성능을 망치지 않게 하는 안전장치

API 컨테이너가 워커를 겸업하니까:

- 레플리카당 concurrency=1 유지
- 그리고 “요청 트래픽이 많을 때 워커 쉬기” 같은 옵션(선택)

최소로는:

- WORKER_ENABLED 스위치
- WORKER_CONCURRENCY=1
이 두 개만 확실히.

### 꼭(4) 핸들러 실행 시 “DB 커넥션/트랜잭션” 전달 옵션

지금 핸들러는 store만 받는데,
대부분 작업은 DB 읽고/쓰고/업서트/삭제가 들어가.
그래서 라이브러리에 이 패턴을 추가하면 좋아:

- store.with_conn(fn) 또는
- ctx.conn 제공(선택)

이거 없으면 앱마다 pool/conn 관리가 중구난방이 되기 쉬움.

### 꼭(5) pg_cron 없이도 enqueue 가능한 “SQL 템플릿” 문서화

너는 단순 SQL migration 방식으로 간다 했으니,

- examples/enqueue.sql 같은 템플릿을 패키지에 넣어두면
다른 프로젝트에서 바로 재사용 가능해.

## 결론

- 지금 코드 골격만으로도 당장 동작은 해 ✅
- 하지만 “운영/확장”까지 생각하면 위 5개 중 1~3번은 거의 필수야.

원하면 내가 다음 메시지에서 최소 변경으로:

- (1) 관측 필드 추가 DDL + store/worker 업데이트
- (2) timeout_seconds 반영한 reaper 로직
- (4) 핸들러에 conn 전달 옵션(안 쓰면 영향 없음)

이 3개를 “패치 형태”로 딱 만들어줄게.


---

**You**

1~5 다 필요할 것 같아! 이것만 하고 마무리하자


---

**ChatGPT**

좋아. 1~5 전부 반영해서 “이 라이브러리(골격)”를 운영 가능한 수준으로 마무리하자.
아래는 DDL 업데이트 + 코드 패치(파일 단위로 교체/추가) + SQL 템플릿까지 포함이야.

## 1) DDL 보강 (관측 + 결과 + timeout + 범용성)

src/pgjobq/ddl.sql 교체:

```sql
DO $$ BEGIN
  CREATE TYPE job_status AS ENUM ('READY','RUNNING','DONE','FAILED','CANCELLED');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE TABLE IF NOT EXISTS jobs (
  id bigserial PRIMARY KEY,

  job_type text NOT NULL,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,

  status job_status NOT NULL DEFAULT 'READY',
  priority int NOT NULL DEFAULT 0,

  attempts int NOT NULL DEFAULT 0,
  max_attempts int NOT NULL DEFAULT 5,
  run_after timestamptz NOT NULL DEFAULT now(),

  -- timeout for stale recovery (seconds). If NULL, reaper uses its default.
  timeout_seconds int,

  locked_at timestamptz,
  locked_by text,

  dedupe_key text,
  last_error text,

  -- observability
  finished_at timestamptz,
  duration_ms int,

  -- optional output/result (generic)
  result jsonb,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_jobs_pick
ON jobs (status, run_after, priority DESC, id);

CREATE INDEX IF NOT EXISTS idx_jobs_type
ON jobs (job_type);

CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_active_dedupe
ON jobs (dedupe_key)
WHERE dedupe_key IS NOT NULL AND status IN ('READY','RUNNING');
```

이미 기존 테이블이 운영 중이면 ALTER TABLE ADD COLUMN IF NOT EXISTS ... 형태로 마이그레이션 파일을 따로 만드는 게 안전해.

## 2) Store: 완료/실패에 finished_at/duration/result 기록 + timeout 지원 + conn/tx 제공

src/pgjobq/store_asyncpg.py 핵심 패치 버전(교체 권장)

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, AsyncIterator
from contextlib import asynccontextmanager

import asyncpg

from .models import Job, JobStatus

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@dataclass
class PgJobStore:
    dsn: Optional[str] = None
    pool: Optional[asyncpg.Pool] = None

    async def start(self) -> None:
        if self.pool is None:
            if not self.dsn:
                raise ValueError("PgJobStore requires either dsn or pool")
            self.pool = await asyncpg.create_pool(dsn=self.dsn, min_size=1, max_size=10)

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    # ---- connection helpers (4번 요구사항) ----
    @asynccontextmanager
    async def connection(self) -> AsyncIterator[asyncpg.Connection]:
        assert self.pool is not None, "call start() first"
        async with self.pool.acquire() as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[asyncpg.Connection]:
        assert self.pool is not None, "call start() first"
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                yield conn

    # ---- enqueue (1/2/3 지원) ----
    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        run_after: datetime | None = None,
        priority: int = 0,
        max_attempts: int = 5,
        timeout_seconds: int | None = None,
    ) -> int:
        assert self.pool is not None, "call start() first"
        ra = run_after or _utcnow()

        sql = """
        INSERT INTO jobs (job_type, payload, dedupe_key, run_after, priority, max_attempts, timeout_seconds)
        VALUES ($1, $2::jsonb, $3, $4, $5, $6, $7)
        ON CONFLICT DO NOTHING
        RETURNING id;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, job_type, payload, dedupe_key, ra, priority, max_attempts, timeout_seconds)
            return int(row["id"]) if row else 0

    # ---- pickup ----
    async def pickup(self, *, worker_id: str) -> Job | None:
        assert self.pool is not None, "call start() first"

        sql = """
        WITH picked AS (
          SELECT id
          FROM jobs
          WHERE status='READY'
            AND run_after <= now()
          ORDER BY priority DESC, id
          FOR UPDATE SKIP LOCKED
          LIMIT 1
        )
        UPDATE jobs j
        SET status='RUNNING',
            locked_at=now(),
            locked_by=$1,
            attempts = attempts + 1,
            updated_at=now(),
            -- clear any previous terminal fields for re-runs
            finished_at=NULL,
            duration_ms=NULL
        FROM picked
        WHERE j.id = picked.id
        RETURNING
          j.id, j.job_type, j.payload, j.status, j.priority, j.attempts, j.max_attempts,
          j.run_after, j.timeout_seconds, j.locked_at, j.locked_by, j.dedupe_key, j.last_error,
          j.finished_at, j.duration_ms, j.result, j.created_at, j.updated_at;
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(sql, worker_id)
            if not row:
                return None
            return _row_to_job(row)

    # ---- mark done / error (1번 요구사항) ----
    async def mark_done(self, job_id: int, *, result: dict[str, Any] | None = None, duration_ms: int | None = None) -> None:
        assert self.pool is not None, "call start() first"
        sql = """
        UPDATE jobs
        SET status='DONE',
            finished_at=now(),
            duration_ms=COALESCE($2, duration_ms),
            result=COALESCE($3::jsonb, result),
            updated_at=now(),
            locked_at=NULL,
            locked_by=NULL
        WHERE id=$1;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id, duration_ms, result)

    async def mark_error(
        self,
        job_id: int,
        error: str,
        *,
        retry_after: timedelta | None = None,
        terminal: bool = False,
        duration_ms: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> None:
        assert self.pool is not None, "call start() first"
        delay = retry_after or timedelta(minutes=1)
        interval_str = f"{int(delay.total_seconds())} seconds"

        sql = """
        UPDATE jobs
        SET
          last_error = $2,
          updated_at = now(),
          duration_ms = COALESCE($5, duration_ms),
          result = COALESCE($6::jsonb, result),

          status = CASE
            WHEN $3::bool THEN 'FAILED'
            WHEN attempts >= max_attempts THEN 'FAILED'
            ELSE 'READY'
          END,

          finished_at = CASE
            WHEN $3::bool THEN now()
            WHEN attempts >= max_attempts THEN now()
            ELSE NULL
          END,

          run_after = CASE
            WHEN $3::bool THEN run_after
            WHEN attempts >= max_attempts THEN run_after
            ELSE now() + $4::interval
          END,

          locked_at = NULL,
          locked_by = NULL
        WHERE id = $1;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id, error, terminal, interval_str, duration_ms, result)

    async def cancel(self, job_id: int) -> None:
        assert self.pool is not None, "call start() first"
        sql = "UPDATE jobs SET status='CANCELLED', finished_at=now(), updated_at=now() WHERE id=$1;"
        async with self.pool.acquire() as conn:
            await conn.execute(sql, job_id)

    # ---- stale recovery (2번 요구사항) ----
    async def reap_stale(self, *, default_stale_after: timedelta) -> int:
        """
        Reset RUNNING jobs back to READY if they exceeded their timeout.
        timeout_seconds (per-job) overrides default_stale_after.
        """
        assert self.pool is not None, "call start() first"
        default_seconds = int(default_stale_after.total_seconds())

        sql = f"""
        UPDATE jobs
        SET status='READY',
            locked_at=NULL,
            locked_by=NULL,
            updated_at=now(),
            -- don't run immediately unless run_after is already in the future
            run_after = GREATEST(run_after, now())
        WHERE status='RUNNING'
          AND locked_at IS NOT NULL
          AND locked_at < now() - make_interval(secs => COALESCE(timeout_seconds, {default_seconds}));
        """
        async with self.pool.acquire() as conn:
            res = await conn.execute(sql)
            return int(res.split()[-1])

def _row_to_job(row: asyncpg.Record) -> Job:
    return Job(
        id=int(row["id"]),
        job_type=str(row["job_type"]),
        payload=dict(row["payload"]) if row["payload"] is not None else {},
        status=JobStatus(str(row["status"])),
        priority=int(row["priority"]),
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        run_after=row["run_after"],
        timeout_seconds=row["timeout_seconds"],
        locked_at=row["locked_at"],
        locked_by=row["locked_by"],
        dedupe_key=row["dedupe_key"],
        last_error=row["last_error"],
        finished_at=row["finished_at"],
        duration_ms=row["duration_ms"],
        result=dict(row["result"]) if row["result"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
```

models.Job에 timeout_seconds, finished_at, duration_ms, result 필드를 추가해야 해(아래 3번).

## 3) models.py 확장(관측/timeout/result)

src/pgjobq/models.py 필드 추가 버전:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Awaitable, Mapping, Optional

class JobStatus(str, Enum):
    READY = "READY"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

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
    store: Any
    worker_id: str

Handler = Callable[[JobContext], Awaitable[None]]
Handlers = Mapping[str, Handler]
```

## 4) Worker 보강(관측/결과/timeout-aware reaper + API 보호)

src/pgjobq/worker.py 패치 버전

- job 처리 시간 측정 → duration_ms
- handler가 ctx.job.result에 넣는 대신, handler가 리턴값을 주면 result로 기록하는 방식이 깔끔한데, 골격 유지 위해 “result는 worker가 optional로 받는” 형태로 구현(간단).

아래 버전은:

- handler가 아무것도 리턴 안 해도 OK
- 실패 시에도 duration 기록

```python
from __future__ import annotations

import asyncio
import os
import socket
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional, Any

from .backoff import BackoffPolicy, IdlePollPolicy
from .models import Handlers, JobContext
from .store_asyncpg import PgJobStore

@dataclass
class Worker:
    store: PgJobStore
    handlers: Handlers

    concurrency: int = 1
    enabled: bool = True

    idle_policy: IdlePollPolicy = IdlePollPolicy()
    backoff: BackoffPolicy = BackoffPolicy()

    reap_stale_every_seconds: int = 60
    default_stale_after: timedelta = timedelta(minutes=20)

    worker_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.worker_id is None:
            self.worker_id = f"{socket.gethostname()}-{os.getpid()}"
        env_enabled = os.getenv("WORKER_ENABLED")
        if env_enabled is not None:
            self.enabled = env_enabled.lower() in ("1", "true", "yes", "on")
        env_conc = os.getenv("WORKER_CONCURRENCY")
        if env_conc is not None:
            self.concurrency = max(1, int(env_conc))

    async def lifespan(self, app):
        await self.store.start()

        stop_event = asyncio.Event()
        tasks: list[asyncio.Task] = []

        if self.enabled:
            for _ in range(self.concurrency):
                tasks.append(asyncio.create_task(self._run_loop(stop_event)))
            tasks.append(asyncio.create_task(self._reaper_loop(stop_event)))

        try:
            yield
        finally:
            stop_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.store.close()

    async def _reaper_loop(self, stop_event: asyncio.Event) -> None:
        try:
            while not stop_event.is_set():
                await asyncio.sleep(self.reap_stale_every_seconds)
                try:
                    await self.store.reap_stale(default_stale_after=self.default_stale_after)
                except Exception:
                    pass
        except asyncio.CancelledError:
            return

    async def _run_loop(self, stop_event: asyncio.Event) -> None:
        empty_streak = 0
        try:
            while not stop_event.is_set():
                job = await self.store.pickup(worker_id=self.worker_id or "worker")
                if job is None:
                    empty_streak += 1
                    await asyncio.sleep(self.idle_policy.next_sleep(empty_streak))
                    continue

                empty_streak = 0
                started = time.perf_counter()
                try:
                    await self._dispatch(job)
                    dur_ms = int((time.perf_counter() - started) * 1000)
                    await self.store.mark_done(job.id, duration_ms=dur_ms)
                except Exception as e:
                    dur_ms = int((time.perf_counter() - started) * 1000)
                    delay = self.backoff.retry_delay(job.attempts)
                    await self.store.mark_error(job.id, repr(e), retry_after=delay, terminal=False, duration_ms=dur_ms)

                # small sleep to avoid tight loop even when jobs are flowing
                await asyncio.sleep(0)

        except asyncio.CancelledError:
            return

    async def _dispatch(self, job) -> None:
        handler = self.handlers.get(job.job_type)
        if handler is None:
            await self.store.mark_error(
                job.id,
                f"No handler registered for job_type={job.job_type}",
                terminal=True,
            )
            return

        ctx = JobContext(job=job, store=self.store, worker_id=self.worker_id or "worker")
        await handler(ctx)
```

✅ 여기까지로 (1)(2)(3)(4) 충족.

- (3) API 보호는 지금 “기본 동시성 1 + env 스위치 + 즉시 분리 가능”로 해결.
- 더 세게 하려면 “요청량 감지해서 쉬기” 같은 것도 가능하지만, 지금 마무리 목표에선 과하지 않게.

## 5) pg_cron 없이도, / pg_cron으로도 바로 enqueue 가능한 SQL 템플릿(문서/예시)

패키지에 examples/enqueue.sql 같은 파일을 두는 게 가장 실용적이야. (요구사항 5)
예시 템플릿:

```sql
-- (A) 단일 enqueue (dedupe 적용)
INSERT INTO jobs (job_type, payload, dedupe_key, run_after, priority)
VALUES (
  'some_job',
  jsonb_build_object('k','v'),
  'some_job:target:123',
  now(),
  0
)
ON CONFLICT DO NOTHING;

-- (B) batch enqueue (SELECT 기반, dedupe)
INSERT INTO jobs (job_type, payload, dedupe_key)
SELECT
  'some_job',
  jsonb_build_object('conversation_id', c.id),
  'some_job:conversation:' || c.id
FROM conversations c
WHERE c.needs_work = true
ON CONFLICT DO NOTHING;
```

pg_cron은 이걸 함수로 감싸서 호출만 하면 됨.

## 마지막 체크: “이 정도면 충분?”

응. 지금은 충분해.
이 1~5를 넣어두면, 운영 중에 가장 흔한 문제들(중복, 장애 후 RUNNING 고착, 관측 불가, 재시도 정책 산재, 커넥션 관리 난장판)을 초반에 다 잡아.

원하면 다음(정말 마지막)으로 한 번만 더:

- README.md에 “설치/DDL 적용/FastAPI 붙이기/pg_cron enqueue 예시/운영 env”까지 짧게 템플릿으로 써줄까?


---
