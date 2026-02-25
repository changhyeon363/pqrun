---
icon: lucide/list-checks
---

# Implementation Decisions

구현 시작 전에 결정이 필요한 사항들을 정리합니다.

---

## 🔴 Critical Decisions (반드시 결정 필요)

### 1. Connection Pool 관리 방식

**현재 설계**:
```python
PgJobStore(
    dsn="postgresql://...",  # 옵션 A
    pool=existing_pool       # 옵션 B
)
```

**결정 필요**:

- [ ] **옵션 A**: Store가 자체 pool 생성/관리
  - 장점: 사용 간단, 독립적
  - 단점: 앱과 pool 분리 → connection 낭비 가능

- [ ] **옵션 B**: 앱이 pool 전달
  - 장점: Connection 효율적 공유
  - 단점: 사용자가 pool 관리 필요

- [ ] **옵션 C**: 둘 다 지원 (추천)
  - dsn 있으면 자체 pool 생성
  - pool 있으면 재사용

**추천**: **옵션 C** - 유연성 최대화

---

### 2. Handler 결과값 처리

**현재 설계**:
```python
async def handler(ctx: JobContext) -> None:
    # result를 어떻게 저장?
```

**선택지**:

#### 옵션 A: Handler가 return 값으로 result 전달
```python
async def handler(ctx: JobContext) -> dict | None:
    return {"summary": "...", "tokens": 123}

# Worker가 자동으로 jobs.result에 저장
```
**장점**: 깔끔, 명시적
**단점**: Handler 시그니처 변경 (`-> None` → `-> dict | None`)

#### 옵션 B: Context를 통해 명시적 저장
```python
async def handler(ctx: JobContext) -> None:
    result = {"summary": "..."}
    # 옵션 B-1: ctx.set_result(result)
    # 옵션 B-2: 직접 DB 저장 (store.mark_done에서)
```
**장점**: 유연성, side-effect 명확
**단점**: 보일러플레이트

#### 옵션 C: result 저장 안 함 (애플리케이션 DB에 직접)
```python
async def handler(ctx: JobContext) -> None:
    async with ctx.store.transaction() as conn:
        await conn.execute("INSERT INTO summaries ...")
    # jobs.result는 NULL
```
**장점**: 가장 단순
**단점**: 관측성 낮음

**현재 브레인스토밍**: 옵션 A와 유사하지만 `-> None` 유지
**결정 필요**: **어느 방식을 채택할 것인가?**

**추천**: **옵션 A** - return 값 활용, 시그니처는 `-> dict | None` 허용

---

### 3. Transaction 제공 방식

Handler가 DB 작업을 할 때:

#### 옵션 A: Context에 connection 제공
```python
@dataclass(frozen=True)
class JobContext:
    job: Job
    store: PgJobStore
    worker_id: str
    conn: asyncpg.Connection  # ← 추가

async def handler(ctx: JobContext):
    await ctx.conn.execute("UPDATE ...")
```
**장점**: 간편
**단점**: 모든 handler가 conn을 받음 (불필요할 수도)

#### 옵션 B: Store를 통해 on-demand 제공
```python
async def handler(ctx: JobContext):
    async with ctx.store.transaction() as conn:
        await conn.execute("UPDATE ...")
```
**장점**: 필요할 때만 사용
**단점**: 코드가 약간 길어짐

**추천**: **옵션 B** - on-demand가 더 유연

---

### 4. Logging 전략

**선택지**:

#### 옵션 A: 라이브러리가 자체 logger 제공
```python
import logging
logger = logging.getLogger("pqrun")

# Worker에서:
logger.info(f"Picked job {job.id} type={job.job_type}")
```
**장점**: 즉시 사용 가능
**단점**: 앱의 logging 설정과 충돌 가능

#### 옵션 B: Context에 logger 주입
```python
@dataclass(frozen=True)
class JobContext:
    job: Job
    store: PgJobStore
    worker_id: str
    logger: logging.Logger  # ← 추가

async def handler(ctx: JobContext):
    ctx.logger.info("Processing...")
```
**장점**: 앱이 제어 가능
**단점**: Worker 초기화 시 logger 설정 필요

#### 옵션 C: Logging 미제공 (최소주의)
```python
# Handler에서 직접:
import logging
logger = logging.getLogger(__name__)
```
**장점**: 가장 단순
**단점**: 표준화 안 됨

**추천**: **옵션 A** - 기본 logger 제공, 추후 옵션 B로 확장 가능

---

## 🟡 Important Decisions (권장)

### 5. Worker 종료 시 Job 처리

Worker가 shutdown될 때 실행 중인 job은?

#### 옵션 A: Graceful shutdown (현재 job 완료 대기)
```python
# lifespan finally:
stop_event.set()  # 새 job pickup 중지
await asyncio.gather(*tasks)  # 현재 job 완료까지 대기
```
**장점**: 안전, 작업 손실 없음
**단점**: Shutdown 지연 가능

#### 옵션 B: Immediate shutdown (즉시 취소)
```python
for t in tasks:
    t.cancel()
```
**장점**: 빠른 종료
**단점**: Reaper가 복구해야 함 (RUNNING → READY)

#### 옵션 C: Hybrid shutdown (취소 + 제한 시간 대기)
```python
stop_event.set()
for t in tasks:
    t.cancel()
await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=30.0)
```
**장점**: 종료 지연을 제한하면서도 정리 시도 가능
**단점**: 실행 중이던 job은 재시작 후 reaper 복구 필요

**현재 구현**: **옵션 C**

---

### 6. Dedupe 충돌 시 동작

```python
await store.enqueue(job_type="x", dedupe_key="k1")
await store.enqueue(job_type="x", dedupe_key="k1")  # ← 중복
```

**현재 설계**: `ON CONFLICT ... DO UPDATE SET updated_at=now() RETURNING id`
중복이면 기존 active job의 `id`를 반환

**선택지**:

#### 옵션 A: 0 반환
```python
job_id = await store.enqueue(...)
if job_id == 0:
    # 이미 존재
```
**장점**: 간단
**단점**: 기존 job_id를 모름

#### 옵션 B: 기존 job_id 반환 (현재)
```sql
INSERT INTO jobs (...)
VALUES (...)
ON CONFLICT (dedupe_key) WHERE ... DO UPDATE SET updated_at=now()
RETURNING id;
```
**장점**: 기존 job 추적 가능
**단점**: UPDATE 발생 (성능 영향 미미)

#### 옵션 C: Exception 발생
```python
raise DuplicateJobError("Job with dedupe_key already exists")
```
**장점**: 명시적
**단점**: 정상 동작에서 exception은 과함

**추천**: **옵션 B** - 기존 job_id 반환

---

### 7. Job Cleanup 전략

완료된 job은 언제 삭제?

#### 옵션 A: 라이브러리가 cleanup job 제공
```python
# Built-in handler
"__cleanup": cleanup_handler

# Auto-enqueued by Worker or pg_cron
DELETE FROM jobs
WHERE status IN ('DONE','FAILED','CANCELLED')
  AND finished_at < now() - interval '7 days';
```
**장점**: 기본 제공
**단점**: 앱마다 정책 다를 수 있음

#### 옵션 B: 예시만 제공, 구현은 앱 책임
```sql
-- examples/cleanup.sql
DELETE FROM jobs WHERE ...;
```
**장점**: 유연성
**단점**: 사용자가 직접 구현

#### 옵션 C: 제공 안 함
**장점**: 최소주의
**단점**: Production에서 테이블 무한 증가

**추천**: **옵션 B** - 예시 SQL 제공

---

### 8. Error Serialization

Handler exception을 `last_error`에 저장할 때:

**현재**: `repr(e)` → `"ValueError('invalid input')"`

**선택지**:

#### 옵션 A: repr (현재)
**장점**: 간단
**단점**: Traceback 없음

#### 옵션 B: Traceback 포함
```python
import traceback
last_error = traceback.format_exc()
```
**장점**: 디버깅 편함
**단점**: 길이 제한 필요 (TEXT 타입이라 괜찮음)

#### 옵션 C: Structured error (JSON)
```python
last_error = json.dumps({
    "type": type(e).__name__,
    "message": str(e),
    "traceback": traceback.format_exc()
})
```
**장점**: 파싱 가능
**단점**: 복잡

**추천**: **옵션 B** - traceback 포함, 길이 제한 (예: 10KB)

---

## 🟢 Nice-to-Have Decisions (선택적)

### 9. Type Hints 엄격도

#### 옵션 A: Strict (mypy --strict)
**장점**: 타입 안전성 최대
**단점**: 개발 속도 느림

#### 옵션 B: Moderate (mypy 기본)
**장점**: 균형
**단점**: Runtime error 가능성

**추천**: **옵션 B** - 점진적 강화

---

### 10. Python 버전 지원

**선택지**:
- Python 3.10+ (현재 설계)
- Python 3.11+ (match/case, better asyncio)
- Python 3.12+ (최신)

**추천**: **Python 3.10+** - 널리 사용되는 최소 버전

---

### 11. Testing Framework

#### 옵션 A: pytest + pytest-asyncio
**장점**: 표준, 플러그인 많음

#### 옵션 B: unittest (stdlib)
**장점**: 의존성 없음

**추천**: **옵션 A** - pytest

---

### 12. Documentation

#### 옵션 A: README.md만
**장점**: 단순

#### 옵션 B: Sphinx / MkDocs
**장점**: 구조화된 문서

**추천**: **옵션 A** - 초기에는 README만, 추후 확장

---

## 📋 Decision Summary

| # | 항목 | 추천 | 우선순위 |
|---|------|------|---------|
| 1 | Connection Pool | 옵션 C (둘 다 지원) | 🔴 Critical |
| 2 | Handler 결과값 | 옵션 A (return dict) | 🔴 Critical |
| 3 | Transaction 제공 | 옵션 B (on-demand) | 🔴 Critical |
| 4 | Logging | 옵션 A (자체 logger) | 🔴 Critical |
| 5 | Worker 종료 | 옵션 A (graceful) | 🟡 Important |
| 6 | Dedupe 충돌 | 옵션 B (기존 id 반환) | 🟡 Important |
| 7 | Job Cleanup | 옵션 B (예시 제공) | 🟡 Important |
| 8 | Error Serialization | 옵션 B (traceback 포함) | 🟡 Important |
| 9 | Type Hints | 옵션 B (moderate) | 🟢 Nice-to-have |
| 10 | Python 버전 | 3.10+ | 🟢 Nice-to-have |
| 11 | Testing | pytest | 🟢 Nice-to-have |
| 12 | Documentation | README only | 🟢 Nice-to-have |

---

## ✅ Action Items

구현 시작 전에:

1. [ ] Critical decisions (1~4) 확정
2. [ ] Important decisions (5~8) 검토
3. [ ] Nice-to-have decisions (9~12) 간단히 결정
4. [ ] DESIGN.md에 최종 결정사항 반영

---

**Next Step**: 위 결정사항을 확정한 후 패키지 구조 생성 시작
