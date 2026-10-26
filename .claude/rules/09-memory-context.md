# Context Memory and Index Rules

메모리 관리와 컨텍스트 탐색 최적화를 위한 규칙. 에이전트가 태스크/run/장기 기억을
저장·검색하는 **Memory Service** 와, 검색을 빠르고 정확하게 만드는 **인덱스 설계**를 다룬다.

## Core Principles

1. **선언된 space 에만 기억한다**: 모든 메모리는 선언된 memory space 에 속한다 —
   ad-hoc 파일/DB 에 기억 저장 금지
2. **격리 기본, 공유는 스코프**: 로컬(local) 기억은 소유 에이전트만 접근.
   공유는 리소스 스코프 체계 (**global / group / local**,
   [01-architecture.md](01-architecture.md) Resource Scoping) 를 따른다 —
   그룹 소속이 곧 접근 경계이며, 에이전트 간 우열 관계는 만들지 않는다
3. **Graph state 와 역할 분리**: state 는 워크플로 계약 데이터, memory 는 검색 가능한
   컨텍스트 축적 — 서로 대체하지 않는다
4. **검색 최적화는 인덱스가 담당**: 에이전트가 raw 스캔하지 않도록 하이브리드 인덱스
   (vector + lexical + 메타데이터 필터) 를 space 단위로 유지
5. **모듈화**: 메모리 정책(스키마/인덱스/보존)은 **memoryset 모듈**로 버저닝 —
   에이전트/그래프는 ref 로만 참조 ([04-module-system.md](04-module-system.md))

## Memory Scopes

리소스 스코프 3계층 (global / group / local) 에 run 수명 스코프를 더한 체계다.

| Scope | 선언 위치 | 수명 | 접근 | 용도 |
|---|---|---|---|---|
| **task** (tier 0) | — | 태스크 실행 중 in-process | 해당 태스크만 | 작업 컨텍스트 (프롬프트 내) — Memory Service 저장 대상 아님 |
| **run** | 그래프 config | run 단위, run 보존 기간 | 해당 run 의 모든 노드 에이전트 | run 내 관찰/중간 산출물 축적, 노드 간 비정형 컨텍스트 |
| **local** | 에이전트 manifest | 영구 (보존 정책) | 소유 에이전트만 | 장기 학습 사실, 과거 작업 요약, 선호/패턴 |
| **group** | group.yaml | 영구 (보존 정책) | 그룹 멤버 (기본 rw, `mode: ro` 제한 가능) | 그룹 공용 지식 베이스, 도메인 사실 축적 |
| **global** | groups/global.yaml | 영구 (보존 정책) | 전 에이전트 기본 ro — write 는 `writers` 명시 | 전사 공통 지식, 규범/사실 |

### Scope Rules

1. **run scope**: run 종료 후 read-only 전환 — 보존 기간(기본 30일, checkpoint 와 동일)
   후 삭제. Service run 은 iteration 이 이어지는 동안 계속 누적 + compaction 적용.
   Cross-group 배선 그래프에서도 run 참여 노드 전원이 접근 (run 이 곧 경계)
2. **local scope**: 컨테이너 재시작/재배포/그룹 이동과 무관하게 유지 — 에이전트 이름 기준.
   에이전트 버전 교체 시에도 승계 (기억은 이름에, 계약은 버전에)
3. **group scope**: group.yaml 로 선언 — 멤버십이 곧 접근 경계. 어느 에이전트도 소유자가
   아니며, 비멤버는 읽기도 불가. 그룹 이동 시 이전 그룹 space 접근 즉시 상실.
   그룹 간 공유가 필요하면 group scope 를 늘리지 말고 **global scope** 를 사용
4. **global scope**: `groups/global.yaml` 로 선언 — 전 에이전트 read 기본,
   write 는 space 별 `writers` 목록에 명시된 에이전트만
5. **Direct 태스크**: run scope 없음 — local + 소속 group + global 만 접근

### Graph State vs Memory — 판단 기준

| 데이터 | 저장 위치 |
|---|---|
| 다음 노드가 소비하는 계약된 산출물 | **Graph state** (schema 선언) |
| 나중에 검색해서 참고할 관찰/근거/중간 결과 | **Memory** (run scope) |
| run 을 넘어 축적되는 사실/교훈 | **Memory** (local / group / global scope) |
| 대용량 원문/파일 | **Artifact 저장소** + memory 에 참조(`artifact_ref`) 저장 |

Memory 를 노드 간 데이터 전달 통로로 쓰는 것은 안티패턴 — 계약된 전달은 반드시 state 로
(checkpoint/재개 가능성 보장).

## Memory Entry Model

```python
class MemoryEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    entry_id: str                       # UUID
    space: str                          # 소속 space id
    kind: MemoryKind                    # observation | fact | summary | artifact_ref | message
    content: str                        # 본문 (검색/임베딩 대상)
    tags: list[str]                     # 메타데이터 필터용
    source: MemorySource                # agent, run_id, task_id, node_id
    created_at: datetime
    importance: float                   # 0.0~1.0 — retrieval 가중/ compaction 우선순위
    supersedes: str | None              # 정정 시 이전 entry_id 참조
```

### Entry Rules

1. **Append-only**: 항목 수정/삭제 금지 — 정정은 `supersedes` 로 새 항목 작성.
   삭제는 retention 정책(시스템)만 수행
2. **Kind 는 고정 enum**: 자유 문자열 금지 — 검색 필터의 일관성 보장
3. **대용량 금지**: `content` 상한 (8KB, UTF-8 바이트 — `MAX_CONTENT_BYTES`) — 초과분은 artifact
   저장소에 두고 `artifact_ref` 항목으로 참조. 넘는 append 는 초안 단계의 `VAL_002` 다 (저장 장애가 아니다).
   태그는 식별자와 같은 성질 (비어 있지 않음 · 출력 가능 · 앞뒤 공백 없음) 에 64자 · 32개 상한
   (`MAX_TAG_CHARS` · `MAX_TAGS`), `importance` 는 0.0~1.0 의 수 (문자열 · 불리언 거절) — 어기면 `VAL_002`
4. **Provenance 필수**: `source` 없는 항목 저장 불가 — 모든 기억은 출처 추적 가능.
   출처의 신원 (`source.agent`) 은 **서비스가 토큰에서 채운다** — 호출자가 고르면 다른 에이전트의
   기억으로 위조된다. `source.run_id` 도 **토큰이 아는 run** 이다: run 에 속한 토큰
   (`issue_token(run_id=…)`, run space 가 있는 토큰) 이면 서비스가 그 run 으로 채우고, 호출자가 실은
   값은 그 run 과 같을 때만 받는다. 다른 run 이거나, run 을 모르는 토큰 (direct 태스크 · 에이전트 단위
   토큰) 이 run 을 실으면 `VAL_002` (값은 싣지 않는다). `task_id` · `node_id` 는 호출자 문맥이고 식별자
   형식 (문자열 · 비어 있지 않음 · 128자 이하 · 출력 가능한 문자만 · 앞뒤 공백 없음,
   [03](03-protocol-integration.md) A2A Client Rules 8, `core/identifiers.py`) 을 지나야 한다 — 어기면 `VAL_002`
5. **id 와 시각은 서비스가 발급한다** (#456): append 요청은 내용 (`kind` · `content` · `tags` ·
   `importance` · `supersedes` · `source` 의 문맥) 만 싣는다. `entry_id` (UUID4) 와 `created_at`
   (서비스 시계 — UTC 로 정규화하고 직전 발급보다 뒤로 가지 않는다) 은 서비스가 정하고, 저장된 항목을 응답으로 돌려준다 — 정정은 그 id 로 한다.
   `entry_id` 는 space 를 넘는 전역 키라, 호출자가 고르면 다른 space 의 id 존재가 응답으로 드러나고
   남이 쓸 id 를 선점할 수 있다. 요청에 `entry_id` · `created_at` · `space` · `source.agent` 를 실으면
   조용히 버리지 않고 모르는 키로 `VAL_002` 다 (받은 값은 싣지 않는다)

## Memoryset Modules

메모리 space 의 정책(인덱스/보존/스키마)은 memoryset 모듈로 선언한다.

### Directory Specification

```
modules/memorysets/agent-longterm/
└── 0.1.0/
    └── memoryset.yaml
```

### memoryset.yaml

```yaml
apiVersion: aaestro/v1
kind: Memoryset
metadata:
  name: agent-longterm
  version: 0.1.0
  description: 에이전트 장기 기억 표준 정책

spec:
  scope: local                   # run | local | group | global

  index:
    embedding:
      provider: openai-compatible
      model: text-embedding-3-small   # 버전 고정 — 변경 시 재인덱싱 + version bump
      dimensions: 1536
    chunk:
      max_tokens: 400            # 장문 content 분할 단위
      overlap_tokens: 40
    hybrid:
      vector_weight: 0.6         # RRF 병합 가중
      lexical_weight: 0.4

  retention:
    ttl_days: 365                # 초과 항목 삭제 (summary 는 제외)
    compaction:
      trigger_entries: 5000      # 항목 수 초과 시 compaction 대상
      strategy: summarize        # raw observation → summary 로 압축
      keep_kinds: [fact, summary]
      importance:                # 선택 — 원문 유지 vs 요약을 결정 모델의 등급으로 (01 Decision Models 쓰임 5)
        question: decisionsets/memory-curation@0.1.0#importance   # rating — 에이전트 밖의 모듈이라 전체 ref
                                 # act(질문의 act_level 이상)면 원문 유지. uncertain/unavailable 은 저장된 importance 로

  recall:                        # 자동 주입 기본값 (에이전트 manifest 로 override 가능)
    auto: true
    k: 6
    min_score: 0.35
    budget_tokens: 2000
```

> **구현 상태**: 위 memoryset 예시의 `compaction.importance` 는 v0.1 스키마가 아직 받지 않는다. 지금은 모르는 키로 `MOD_003` 거절된다 (`extra="forbid"`).

### Attachment — 스코프별 선언 위치

Space 는 스코프에 대응하는 아티팩트에서 선언한다 — 선언 위치가 곧 접근 경계다.

```yaml
# agents/<name>/manifest.yaml — local scope 부착 (소유 에이전트 전용)
spec:
  memory:
    spaces:
      - ref: memorysets/agent-longterm@0.1.0
        as: longterm             # 에이전트 코드/프롬프트에서 부르는 space 별칭
```

```yaml
# graphs/<name>.yaml — run scope 부착 (run 참여 노드 전원 rw)
spec:
  memory:
    spaces:
      - ref: memorysets/run-scratch@0.1.0
        as: scratch
```

```yaml
# groups/research.yaml — group scope 부착 (그룹 멤버)
spec:
  memory:
    spaces:
      - ref: memorysets/domain-knowledge@0.1.0
        as: knowledge
        mode: rw                 # 멤버 기본 권한 (rw | ro)
```

```yaml
# groups/global.yaml — global scope 부착 (전 에이전트 ro 기본)
spec:
  memory:
    spaces:
      - ref: memorysets/org-facts@0.1.0
        as: org
        writers: [librarian]     # write 가능 에이전트 명시 — 미지정 시 read-only
```

1. 미선언/비소속 space 접근은 `MEM_001` 로 거부 — 배포 검증에서 writers/그룹 멤버십 확인.
   실행 중 회수·강등·확장은 Access Enforcement 절을 따른다
2. Space 의 실체는 `(scope, 이름)` 으로 식별 — 여러 그래프가 같은 group/global space 를
   쓰면 같은 지식 베이스를 공유한다
3. `as` 별칭은 에이전트 관점의 논리 이름 — promptset/skill 코드는 별칭만 사용.
   별칭 충돌 시 해석 순서는 **local > group > global** (가까운 스코프 우선, 리소스
   스코프 규칙과 동일)
4. Memoryset 의 `spec.scope` 와 부착 위치가 불일치하면 배포 검증 실패 (`MOD_003`)

## Index Design — 컨텍스트 탐색 최적화

### Hybrid Index Structure

Space 당 세 계층의 인덱스를 유지한다:

```
Memory Space ──┬── Vector index    (embedding — 의미 검색)
               ├── Lexical index   (BM25/FTS — 키워드/식별자 검색)
               └── Metadata index  (kind, tags, source, created_at — 구조 필터)
```

1. **병합**: vector + lexical 결과를 RRF (Reciprocal Rank Fusion) 로 병합,
   memoryset 의 가중치 적용 — 단일 방식 검색은 fallback 전용
2. **격리 = 인덱스 격리**: 인덱스는 space 단위로 분리 — 검색이 space 경계를 넘지 않는다
   (cross-space 검색은 호출 측이 space 목록을 명시할 때만, 각각 검색 후 병합)
3. **Chunking**: `chunk.max_tokens` 초과 content 는 분할 임베딩 — 검색 결과는
   entry 단위로 dedup 하여 반환
4. **Materialize 는 id 점조회**: 검색 hit 의 항목은 저장소에서 **hit id 로 조회**한다
   (`MemoryStore.get_many`, space 당 한 쿼리). 최근 N건 scan 결과 맵으로 hit 을 항목으로 바꾸지
   않는다 — scan 상한이 곧 기억의 지평선이 되어, 인덱스에 있어도 상한보다 오래된 기억이 조용히
   버려진다. 저장소에 없는 hit (TTL 삭제 뒤 인덱스 지연) 은 DEBUG 로그와 함께 그 hit 만 탈락한다.
   **정정 해석도 같은 점조회다**: `Recall.search` 가 hit id 들 중 다른 항목이 `supersedes` 로 가리키는 것
   (`MemoryStore.superseded`, 쿼리 한 번) 을 거른다 — 서비스·HTTP·`memory_search` tool 이 같은 규칙을 쓰고,
   원본과 정정이 모두 최근 N건 밖이어도 대체된 기억이 주입되지 않는다
   (정정은 **같은 space 안에서만** 대체한다 — `superseded(space, ids)` 가 space 로 좁히고,
   `MemoryService.append` 는 다른 space 의 항목을 가리키는 `supersedes` 를 `VAL_002` 로 거절한다)
5. **식별자 정확 검색**: 에러 코드/함수명/URL 같은 식별자는 lexical index 가 담당 —
   vector 만으로 매칭 실패하는 케이스의 보완 (하이브리드가 기본인 이유)

### Write Path — 비동기 인덱싱

```
append() ──▶ 저장 (즉시 commit) ──▶ 인덱싱 큐 ──▶ embed + index 반영
                                      (비동기, 목표 지연 < 5s)
```

1. 저장과 인덱싱 분리 — append 는 embedding 완료를 기다리지 않는다
2. **Eventual consistency 명시**: 방금 저장한 항목은 검색에 즉시 안 나올 수 있다 —
   같은 태스크 안에서 self-read 가 필요한 데이터는 memory 가 아니라 작업 컨텍스트로
3. 인덱싱 실패는 재시도 (retry policy) — 누적 실패 시 `MEM_003` + 메트릭 경보
4. `aaestro memory reindex <space>` — embedding 모델 교체/장애 복구용 전체 재인덱싱

### Embedding Model Pinning

1. Embedding 모델/차원은 memoryset 버전에 고정 — 모델 교체는 **version bump + 전체
   재인덱싱** (혼합 임베딩 공간 금지)
2. 재인덱싱 중에는 구 인덱스로 검색 유지 — 완료 후 원자적 전환

## Retrieval API

에이전트는 `AgentContext.memory` 로만 메모리에 접근한다 (DB 직접 접근 금지).

```python
# 검색 — 하이브리드, space 명시
results = await ctx.memory.search(
    query="mcp transport 재연결 실패 원인",
    spaces=["longterm", "scratch"],
    k=8,
    filters=MemoryFilter(kinds=[MemoryKind.FACT, MemoryKind.SUMMARY], tags=["mcp"]),
)
# results: list[ScoredEntry] — entry + score + space (provenance 포함)

# 저장 — 내용만 싣는다. id · 시각 · 출처의 신원은 서비스가 발급한다 (Entry Rules 4 · 5)
stored = await ctx.memory.append(
    space="longterm",
    kind=MemoryKind.FACT,
    content="mcp sidecar 는 이미지 태그 미고정 시 재기동 후 tool 목록이 달라질 수 있음",
    tags=["mcp", "sidecar"],
    importance=0.7,
    source={"task_id": task.task_id, "node_id": task.node_id},   # 선택 — run 은 토큰이 정한다
)
# stored: MemoryEntry — 정정은 stored.entry_id 를 supersedes 로
```

### Access Enforcement

```
Agent 컨테이너 (agentd) ──HTTP──▶ Memory Service (강제 지점) ──판정──▶ 권한 레지스트리 (control plane)
        └── control plane 발급 에이전트 신원
```

1. Memory Service 는 프레임워크 컴포넌트 (`src/aaestro/memory/`) — 저장소 자격증명은
   서비스만 보유, 에이전트 컨테이너에 DB 자격증명 주입 금지
2. **토큰은 권한이 아니라 신원이다**: 에이전트가 내미는 것은 control plane 이 발급한 에이전트
   신원이다. 어떤 space 에 어떤 mode 로 닿는지는 Memory Service 가 **요청마다** 레지스트리에
   `memory` 판정으로 묻는다 ([01-architecture.md](01-architecture.md) Access Control)
3. **선언이 기본 권한**: 매니페스트(local)·group.yaml(group)·global.yaml(global, `writers`)
   선언이 배포 시점의 권한이다. 운영자는 space 회수와 `rw`→`ro` 강등을 재배포 없이 한다.
   선언 밖의 space 는 권한 에이전트가 확장 상한 안에서만 부여한다
4. **신원은 재시작을 넘는다**: Memory Service 를 재시작해도 떠 있는 에이전트의 신원이 무효가
   되지 않는다
5. 판정 캐시·변경 알림·레지스트리 장애 시 동작은 01 Access Control 을 따른다 — 캐시에 없는
   판정은 `MEM_001` 로 거부, 이미 허용된 캐시는 유지
6. 모든 접근 감사 로그: `agent`, `group`, `memory_space`, `op`, `status`, `decision_source` —
   거부 (`denied`) 와 실패 (`failed` — 저장소 장애, 검증 거절) 도 한 줄씩 남는다 (검색 · 회상 포함). `decision_source` 는
   그 접근을 허용 · 거부한 레지스트리 판정의 출처이고, space 판정을 묻지 않은 거부 (미선언 별칭) 는
   신원 확인의 출처다. 레지스트리가 없는 토큰 모드는 `token`
7. **신원 확인이 본문 파싱보다 먼저다**: 토큰이 없거나 통하지 않으면 본문이 어떻든 같은 `MEM_001` (401) 이다 —
   검증 거절의 필드 경로가 인증 전에 나가지 않는다. space 별 판정은 본문의 space 목록이 필요하므로 그 뒤에 온다.
   무인증은 `GET /v1/health` (liveness 만) 뿐이고 API 문서 경로는 열지 않는다. 없는 라우트 · 메서드(404 · 405)는
   라우트 매칭에서 나므로 인증보다 먼저이며 typed 로 답한다 — 드러나는 것은 라우트 존재뿐이다
8. **run scope 는 레지스트리 모드 전용이다**: 토큰 모드 (레지스트리 없는 고정 토큰) 의 토큰은 run
   없이 발급돼 run space 가 실리지 않는다. 토큰 모드 Memory Service 는 카탈로그의 그래프가
   `spec.memory.spaces` 를 선언했으면 기동 시 `CFG_001` 로 실패한다 — space 를 조용히 빼고 뜨지
   않는다. 읽지 못한 그래프 선언도 같이 거부한다 — 그 안에 run scope 선언이 없다는 것을 증명할 수
   없다. 에러 `details` 에는 그래프 이름 · space 별칭과 읽지 못한 선언의 저장소 기준 상대 위치만
   싣는다. **검사는 기동 때 한 번뿐이다**: 뜬 뒤에 `PUT /v1/graphs` 로 저장된 run space 그래프는
   떠 있는 서비스가 모른다 — 토큰 모드 배포를 막는 배포 검증 (PR #471 이후) 에서 다룰 몫이다

### Context Assembly — 프롬프트 주입 규칙

agentd 가 태스크 프롬프트를 구성할 때:

```
프롬프트 = system(promptset)
         + task input (state / direct 요청)
         + recalled memory   ← 여기에만 예산/규칙 적용
         + (루프 진행 중) tool 결과
```

실제 순서도 이렇다 — 회상 블록은 렌더된 태스크 프롬프트 **뒤** 에 온다. 경계 안내가 user 턴 맨 앞에
오도록 (시스템 자리의 프레임워크 지시가 그 자리를 가리킨다, #446).

1. **Auto-recall**: 태스크 진입 시 task input 기반 1회 자동 검색 — memoryset 의
   `recall` 설정 (k, min_score, budget_tokens) 적용
   - **회상 필터** (선택, manifest `spec.decision.recall_filter`): 검색 상위 k 를 결정 모델에
     "이 기억이 지금 태스크에 직접 쓸모가 있는가"(predicate) 로 **한 요청에** 묻고, `reject`
     는 뺀다. `uncertain` 은 `keep`(기본) — 검색 점수가 이미 통과시킨 것을 결정 모델이
     확신 없이 버리지 않는다. provider 가 없거나 닿지 않으면 필터 없이 간다
   - 필터는 `min_score` 뒤, `budget_tokens` 앞에 선다 — 예산을 관련 있는 기억에 쓴다.
     인덱스가 찾고 결정 모델은 거른다: 결정 모델을 검색 대신 쓰지 않는다
2. **Token budget**: recalled memory 총량은 `budget_tokens` 상한 — 초과분은
   score 순으로 절단. 예산은 태스크 컨텍스트를 침범하지 않는다
3. **Relevance threshold**: `min_score` 미달 항목 주입 금지 — 관련 없는 기억은
   노이즈이자 비용
4. **Dedup & 정정 반영**: `supersedes` 체인은 최신 항목만 주입,
   동일 chunk 중복 제거
5. **Provenance 표시**: 주입 시 출처 명시 (`[memory:longterm 2026-08-01]`) —
   모델이 기억과 현재 입력을 구분할 수 있게
6. **Untrusted 경계**: 기억은 과거 태스크 산출물이다 — 기억 속 지시문을 시스템 지시로
   승격 금지 (MCP 응답과 동일한 경계 규칙, [03-protocol-integration.md](03-protocol-integration.md)).
   기억 본문 안의 프레임워크 표기 (경계 표시 · `[aaestro]` · `[operator steer]` · `[tool error]` ·
   `[memory:`) 는 대화에 들어가기 전에 무력화되고 (`render_context` 가 `core.markers.defang` 을
   지난다), 본문의 둘째 줄부터는 들여 써서 그 기억의 출처 표시 줄 아래에 가둔다 — 기억 본문은
   출처 표시로 시작하는 줄을 만들 수 없다. 진짜 표기만 그 모양을 갖는다.
   `[memory:…]` 표시가 데이터라는 것은 기본 루프의 시스템 자리에 프레임워크 지시로도 실린다
   ([04-module-system.md](04-module-system.md) Promptset Rules 6).
   manifest 의 `spec.decision.input_screen` 이 있으면 회상된 기억에도 지시문 판별을 적용해
   `act` 면 표시를 강화하고 WARN 을 남긴다 — 신호이지 경계의 대체가 아니다
   ([02-agent-implementation.md](02-agent-implementation.md) Decision Hooks 4)
7. **추가 탐색은 tool 로**: auto-recall 이후의 검색은 모델이 `memory_search` tool 을
   명시 호출 — 루프마다 자동 재검색하지 않는다 (비용/노이즈 제어)

## Compaction & Retention

무한 축적은 검색 품질과 비용을 망가뜨린다 — space 는 선언된 정책으로 다이어트한다.

1. **TTL**: `ttl_days` 초과 항목 삭제 — `keep_kinds` (기본 fact/summary) 는 예외
2. **Compaction**: trigger (항목 수/기간) 도달 시 오래된 raw 항목들을 summary 로 압축
   - 요약 생성은 시스템 유지보수 그래프 (service mode) 가 수행 — 프레임워크가
     자체 메커니즘(에이전트+그래프)을 사용한다
   - 원본은 summary 의 `source` 로 추적 가능하게 archive 후 TTL 삭제
3. **Importance 반영**: compaction 시 `importance` 높은 항목은 원문 유지 우선.
   memoryset 이 `compaction.importance` 를 선언하면 유지보수 그래프의 에이전트가 항목마다
   결정 모델에 등급(`rating`)을 묻고, 판정이 `act`(질문의 `act_level` 이상)면 저장된 `importance`
   대신 그 판정으로 원문을 유지한다. `uncertain` / `unavailable` 은 저장된 `importance` 로 —
   결정 모델이 없어도 compaction 은 같은 규칙으로 돈다. 요약문은 여전히 LLM 이 쓴다
4. **Service run 필수**: 상주 그래프의 run scope 는 compaction 없이는 무한 성장 —
   service 그래프가 부착하는 run scope memoryset 은 compaction 선언 필수 (배포 검증)
5. **영구 스코프 retention 필수**: local/group/global memoryset 은 `retention`
   (ttl 또는 compaction) 선언 필수 — 특히 group/global 은 다중 작성자로 성장이 빠르다

## Storage Backends

| 환경 | 저장소 | 인덱스 |
|---|---|---|
| dev | SQLite | sqlite-vec (vector) + FTS5 (lexical) |
| prod | PostgreSQL | pgvector (vector) + tsvector/pg_trgm (lexical) |

1. 백엔드는 `MemoryStore` 추상 계약 뒤에 — 교체 시 계약 변경 없음
2. v0.1 은 단일 저장소 + 논리적 space 분리. 전용 vector DB (Qdrant 등) 는
   store 구현 추가로 대응 (future)
3. 백업/보존은 [05-error-handling.md](05-error-handling.md) Data Integrity 정책 준수

## Error Codes & Observability

에러 코드 ([05-error-handling.md](05-error-handling.md) 체계에 통합):

```
MEM_001: Space 미선언 / access 거부
MEM_002: 저장 실패 (storage) — 연결 · 잠금류 일시 장애는 retryable
MEM_003: 인덱싱 실패 누적 / 재인덱싱 필요 / 임베딩 실패 (색인 · 질의 — provider 응답을 해석하지 못함 포함)
MEM_004: 조회 · 검색 실패 / 인덱스 손상 (저장소의 읽기 쿼리 실패 · 손상된 행 포함) — 연결 · 잠금류 일시 장애는 retryable
```

메트릭:

```python
aaestro_memory_operations_total{space, op, status}     # op: append|read|latest|search|recall
aaestro_memory_search_duration_seconds{space}
aaestro_memory_entries{space}                          # Gauge
aaestro_memory_index_lag_seconds{space}                # 인덱싱 큐 지연
aaestro_memory_recall_injected_tokens{agent}           # 프롬프트 주입량 추적
aaestro_memory_recall_filtered_total{agent, band}      # 회상 필터가 본 항목 — act|uncertain|reject
```

`aaestro_memory_operations_total` 의 `space` 는 **space id** 다 (별칭이 아니다 — 별칭은 에이전트 관점의
이름이라 서로 다른 space 가 한 시계열로 합쳐진다). space 로 해석되지 않은 접근 (미선언 별칭, 레지스트리가
거부한 space) 은 고정값 `undeclared` 로 센다 — 호출자가 고른 문자열을 라벨로 쓰지 않는다. 감사 로그의
`memory_space` 는 에이전트가 부른 별칭 그대로다.

검색 · 회상은 **한 호출을 한 결과로** 센다: 여러 space 를 보는 검색이 도중에 실패하면 그 호출이 본
space 전부가 `failed` 다 (호출자는 결과를 하나도 받지 못한다 — 감사 줄도 같다). `recall` 은 검색이 끝난
뒤에 그 결과로 센다. 검색에서 해석되지 않은 별칭을 지정한 거부는 별칭마다 `denied` 로 센다.

로그 필드: `memory_space`, `op`, `entry_id`, `k`, `min_score` — 표준 필드 규칙
([05-error-handling.md](05-error-handling.md)) 준수.

## Testing Requirements

1. **Fake embedder**: 결정적 임베딩 (해시 기반) — 실제 embedding API 호출 금지
2. **ACL 테스트**: 미선언 space 접근 / 비멤버의 group space 접근 / writers 에 없는
   에이전트의 global write / `mode: ro` space 에 append → 모두 `MEM_001`.
   별칭 충돌 시 local > group > global 해석 순서 검증
3. **하이브리드 검색**: 의미 매칭(vector)과 식별자 매칭(lexical) 각각의 히트 검증,
   RRF 병합 순서 검증
4. **Budget/threshold**: recall 주입이 budget_tokens 상한과 min_score 를 준수하는지.
   회상 필터가 있으면 `reject` 만 빠지고 `uncertain` 은 남는지, provider 가 없을 때 필터 없이
   같은 주입이 나오는지 (FakeDecisionModel, [06-testing.md](06-testing.md))
5. **Supersedes**: 정정 체인에서 최신 항목만 주입되는지
6. **Compaction**: trigger 도달 → summary 생성 + 원본 archive 시나리오 (fake model).
   `compaction.importance` 선언 시 `act` 등급 항목이 원문으로 남고 `unavailable` 이면 저장된
   `importance` 로 같은 결과가 나오는지
7. **Eventual consistency**: 인덱싱 지연 중 검색 결과가 커밋 기준으로 안정적인지
