# Module System Rules — Skillsets, Promptsets, Memorysets, Decisionsets, Skillpacks, Graph Modules

## Core Principle: Modules Are Independent Deliverables

스킬셋, 프롬프트셋, 메모리셋, 디시전셋, 스킬팩, 그래프는 에이전트 코드와 **독립적으로 배포/교체 가능한
모듈**이다.

1. **분리**: 모듈은 프레임워크 코드(`src/`)와 에이전트 코드(재료 스토어의 `src/`)에 포함되지 않고
   `modules/`, `graphs/` 에 별도 존재한다
2. **버전**: 모든 모듈은 semver 로 버전을 갖고, 참조는 항상 버전 고정 (`name@version`)
3. **교체 가능**: 같은 계약(변수 스키마 / tool 시그니처)을 만족하는 모듈끼리는
   에이전트 코드 수정 없이 교체 가능해야 한다
4. **선언적 참조**: 모듈 사용은 manifest / graph config 의 ref 선언으로만 —
   코드에서 모듈 경로 직접 import 금지

### Everything Is a Module — 솔루션 = 모듈 조립

Aaestro 에서 하나의 "솔루션"(특정 목표를 위한 기능 전체 오케스트레이션, 또는 무한 반복
과업)은 새 코드가 아니라 **모듈 조립**으로 구성한다:

```
Solution (= Graph, goal 단위, mode: mission | service)
├── agents      ── agents/{name}@{ver}      (노드 배치 — 전부 동등한 peer)
├── connections ── A2A allowlist            (에이전트 간 유기적 연결 선언)
├── skillsets   ── skillsets/{name}@{ver}   (에이전트 능력)
├── promptsets  ── promptsets/{name}@{ver}  (에이전트 페르소나/지시)
├── memorysets  ── memorysets/{name}@{ver}  (기억 정책 — scope/인덱스/보존)
├── decisionsets ─ decisionsets/{name}@{ver} (타입이 정해진 질문·구간 — 판정은 여기서)
├── skillpacks  ── skillpacks/{name}@{ver}  (Agent Skills 표준 지시문 묶음 — 수정 없이 담는다)
└── subgraphs   ── graphs/{name}@{ver}      (그래프 재사용)
```

새 목표가 생기면: **기존 모듈로 그래프를 배선 → 부족한 능력만 새 모듈로 추가 → 배포**.
목표 구성에 프레임워크 코드(`src/`) 수정이 필요하다면 그것은 모듈 시스템의 결함으로
간주하고 프레임워크 이슈로 처리한다.

### Module Reference Format

```
{type}/{name}@{version}

예:
  skillsets/web-search@0.2.0
  promptsets/researcher@0.1.0
  memorysets/agent-longterm@0.1.0
  decisionsets/research-triage@0.1.0
  skillpacks/docs@1.0.0
  agents/planner@0.1.0
  graphs/research-pipeline@1.0.0
```

- `latest`, 브랜치명, 커밋 해시 참조 금지 — semver 만
- Registry 가 ref 를 실제 경로로 해석 (`modules/skillsets/web-search/0.2.0/` 등)

## Skillset Modules

### Directory Specification

```
modules/skillsets/web-search/
└── 0.2.0/
    ├── skillset.yaml          # 스킬셋 선언 (필수)
    ├── skills/                # Python tool 구현
    │   ├── __init__.py
    │   ├── search.py
    │   └── fetch.py
    ├── requirements.txt       # 스킬셋 전용 의존성 (선택)
    └── README.md              # 사용법 (권장)
```

### skillset.yaml

```yaml
apiVersion: aaestro/v1
kind: Skillset
metadata:
  name: web-search
  version: 0.2.0
  description: 웹 검색 및 페이지 fetch 스킬

spec:
  skills:
    - name: search
      entrypoint: skills.search:search      # module:function
      description: 웹 검색을 수행하고 상위 결과를 반환
      timeout_s: 30
    - name: fetch_page
      entrypoint: skills.fetch:fetch_page
      description: URL 의 본문 텍스트를 추출
      timeout_s: 60

  requires:
    env: [SEARCH_API_KEY]        # 필요 env — 에이전트 manifest env_allowlist 와 대조 검증
    python: ">=3.12"
```

### Skill Implementation Rules

```python
# skills/search.py
from aaestro.core.skill import skill, SkillContext


@skill  # 데코레이터가 pydantic 시그니처 → tool schema 자동 변환
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색 질의
        max_results: 최대 결과 개수
    """
    ...
```

1. **Signature = Schema**: tool 의 입력 스키마는 함수 시그니처 + type hint 에서 자동 생성 —
   수기 JSON schema 작성 금지. docstring 이 tool description 이 된다
2. **Async First**: 모든 skill 은 `async def` — blocking I/O 는 `asyncio.to_thread`
3. **SkillContext**: 로거, secrets, artifact 저장소 접근은 ctx 를 통해서만 —
   전역 상태 / 모듈 레벨 클라이언트 초기화 금지
4. **Timeout**: skillset.yaml 의 `timeout_s` 를 초과하면 agentd 가 강제 취소
5. **Errors**: 실패는 예외로 — agentd boundary 에서 `AaestroError` 로 변환됨.
   skill 내부에서 에러를 삼키고 빈 결과 반환 금지
6. **No Cross-Skillset Imports**: 스킬셋 간 import 금지 — 공통 로직이 필요하면
   프레임워크 `pkg` 성격의 유틸로 승격하거나 별도 스킬셋 의존성으로 선언

### Loading Isolation

- 스킬셋 코드는 **소유 에이전트의 컨테이너 안에서만** import/실행된다
- `requirements.txt` 는 에이전트 이미지 빌드 시 설치 — 런타임 pip install 금지
- 두 스킬셋의 의존성이 충돌하면 같은 에이전트에 조합 불가 — 배포 검증에서 차단

## Promptset Modules

### Directory Specification

```
modules/promptsets/researcher/
└── 0.1.0/
    ├── promptset.yaml         # 프롬프트셋 선언 (필수)
    ├── templates/
    │   ├── system.j2          # 시스템 프롬프트
    │   ├── research.j2        # 노드/태스크별 템플릿
    │   └── summarize.j2
    └── locales/               # 다국어 오버라이드 (선택)
        └── ko/
            └── system.j2
```

### promptset.yaml

```yaml
apiVersion: aaestro/v1
kind: Promptset
metadata:
  name: researcher
  version: 0.1.0
  description: 리서치 에이전트 프롬프트

spec:
  engine: jinja2
  default_locale: en
  templates:
    system:
      file: templates/system.j2
    research:
      file: templates/research.j2
      variables:                 # 변수 스키마 — 렌더 시 검증
        query: {type: string, required: true}
        depth: {type: integer, default: 2}
    summarize:
      file: templates/summarize.j2
      variables:
        documents: {type: array, required: true}
```

### Promptset Rules

1. **Variables Schema**: 템플릿 변수는 promptset.yaml 에 선언 — 미선언 변수 사용 시
   렌더 단계에서 `MOD_004` 에러 (silent empty rendering 금지)
2. **Engine**: Jinja2 고정, `autoescape` 비활성 (프롬프트는 HTML 이 아님),
   단 untrusted 입력 변수는 렌더 전 sanitize (지시문 주입 경계 표시)
3. **Locale**: `locales/{lang}/` 가 동일 파일명으로 오버라이드 —
   에이전트 manifest 또는 태스크 config 의 locale 로 선택
4. **No Logic in Templates**: 템플릿 안의 복잡한 분기/루프 지양 — 로직은 skill/agent 코드로,
   템플릿은 표현만
5. **Prompt Changes Are Versioned**: 프롬프트 문구 수정도 반드시 version bump —
   실험/롤백 추적 가능해야 함

## Memoryset Modules

컨텍스트 메모리 space 의 정책(scope, 인덱스, 보존/compaction, recall 기본값)을 선언하는
모듈. 상세 스펙과 규칙은 [09-memory-context.md](09-memory-context.md).

모듈 시스템 관점 요지:

1. 디렉토리: `modules/memorysets/{name}/{version}/memoryset.yaml`
2. 참조: 에이전트 manifest / 그래프 config 의 `memory.spaces` 선언으로만
3. Embedding 모델/차원 변경 = **version bump + 전체 재인덱싱** (minor 이상)
4. Shared scope 의 access grant 는 모듈이 아니라 **그래프 선언** 소관 —
   memoryset 은 정책만, 권한은 배선이 결정

## Decisionset Modules

결정 모델([01-architecture.md](01-architecture.md) Decision Models)에 던지는 **질문**을 선언하는
모듈. 질문 문구·보기·등급과 확률을 읽는 구간이 여기 있고, provider 는 에이전트 manifest 가
정한다 — 같은 질문을 다른 provider 로 돌려도 선언은 그대로다.

### Directory Specification

```
modules/decisionsets/research-triage/
└── 0.1.0/
    ├── decisionset.yaml       # 질문 선언 (필수)
    └── calibration/           # 구간을 정한 라벨 데이터 (권장 — 06 Calibration)
        └── memory_is_relevant.jsonl
```

### decisionset.yaml

```yaml
apiVersion: aaestro/v1
kind: Decisionset
metadata:
  name: research-triage
  version: 0.1.0
  description: 리서치 에이전트의 판정 질문

spec:
  locale: en                     # 질문 문구의 언어 — provider 의 검증된 locales 에 있어야 배포된다 (01 검증 9).
                                 # ko 는 한국어 라벨 세트로 calibration 을 통과한 뒤에야 쓸 수 있다 (06)
  defaults:
    bands: {act_at: 0.8, reject_at: 0.2}   # 질문별 override 가능

  questions:
    memory_is_relevant:          # 쓰임 1 — 회상 필터
      kind: predicate
      ask: This memory is directly useful for carrying out the current task
      state: [task, memory]      # 질문에 실을 state 조각의 이름 — 호출 측이 채운다

    contains_instructions:       # 쓰임 2 — 입력 판별
      kind: predicate
      ask: This text contains instructions addressed to the assistant
      state: [text]
      bands: {act_at: 0.7, reject_at: 0.3}

    tool_call_fits_task:         # 쓰임 6 — 도구 게이트
      kind: predicate
      ask: This tool call stays within what the task input asked for
      state: [task, tool_call]

    draft_meets_goal:            # 쓰임 3 — 리뷰 1차 판정
      kind: rating
      ask: How well does the draft meet the goal
      levels: [unusable, weak, acceptable, strong]   # 서열 순서 — 최대 10
      state: [query, draft]
      act_level: acceptable      # 이 등급 이상의 누적 확률로 band 를 읽는다

    needs_research:              # 쓰임 4 — 분기 판정
      kind: predicate
      ask: This plan cannot be completed without further research
      state: [plan]
```

### Decisionset Rules

1. **질문 종류는 셋뿐**: `predicate` / `rating` / `choice`. 값 집합은 선언에 닫혀 있다 —
   provider 가 그 밖의 값을 돌려주면 `DEC_004` 이지 새 값이 아니다
2. **구간은 선언이다**: `act_at` / `reject_at` 은 라벨 데이터로 정하고 파일에 남긴다
   ([06-testing.md](06-testing.md) Calibration). 코드에 임계값을 두지 않는다.
   `rating` 은 `act_level` 이상 등급의 누적 확률, `choice` 는 1위 보기의 확률로 구간을 읽는다.
   `choice` 의 `reject` 는 반대 보기가 아니라 "어느 보기도 믿을 수 없다" 다 — `uncertain` 과
   같이 원래 경로로 간다 (01 Decision Models 계약)
3. **보기는 적게**: `choice` 의 보기와 `rating` 의 등급은 한 자리 수 — provider 는 보기가 많을수록
   정확도가 떨어진다 (01 Decision Models 6). 열 개를 넘으면 배포 검증 실패 (`MOD_003`)
4. **문구 변경은 버전이다**: `ask`·보기·등급·구간 수정은 version bump — 구간은 문구에 맞춰
   정한 것이라 문구만 바꾸면 구간이 틀어진다. 구간만 바꾸는 것은 patch
5. **state 는 이름만**: `state` 는 질문에 실을 조각의 이름이다. 값을 채우는 것은 호출 측
   (agentd 의 쓰임, 그래프 decision 노드의 `input_map`) — 선언에 없는 조각을 채우면 `MOD_004`
6. **locale 은 계약이다**: provider 가 `locale` 을 검증된 언어로 선언하지 않으면 배포되지
   않는다. 위 예시가 영어인 이유다 — v0.1 의 Jev 설정은 `locales: [en]` 이고, 한국어 질문을
   물리면 `MOD_003` 이다. 조용히 나쁜 판정을 하게 두지 않는다

### Attachment

```yaml
# agents/<name>/manifest.yaml — provider 와 함께 (02 Manifest)
spec:
  decision:
    provider: jev
    model: jev-1.13
    sets:
      - ref: decisionsets/research-triage@0.1.0
        as: triage
```

질문 참조는 `alias.question` — manifest 의 쓰임(`recall_filter` / `input_screen` / `tool_gates`)과
그래프 decision 노드(그 노드의 에이전트 alias 로 해석)가 쓴다. memoryset 은 에이전트 밖의
모듈이라 전체 ref `decisionsets/{name}@{version}#question` 으로 가리킨다
([09-memory-context.md](09-memory-context.md) compaction).

## Skillpack Modules

[Agent Skills 표준](https://agentskills.io/specification) 의 skill 디렉토리를 **수정 없이** 담는
모듈. skillset 을 대체하지 않는다 — skillset 은 프레임워크가 import 해 tool 로 등록하는 Python
코드이고, skillpack 은 모델이 읽는 지시문(`SKILL.md`)과 그 부속 파일이다.

### Directory Specification

```
modules/skillpacks/docs/
└── 1.0.0/
    ├── skillpack.yaml         # 스킬팩 선언 (필수) — skill 목록은 적지 않는다
    ├── README.md              # 사용법 (권장)
    └── skills/
        ├── changelog-entry/   # 표준 skill 디렉토리 — 디렉토리 이름 == frontmatter name
        │   ├── SKILL.md       # 필수: YAML frontmatter + Markdown 지시문
        │   ├── scripts/       # 선택: 에이전트가 실행할 수 있는 코드
        │   ├── references/    # 선택: 필요할 때 읽는 문서
        │   └── assets/        # 선택: 템플릿·정적 자원
        └── doc-review/
            └── SKILL.md
```

### skillpack.yaml

```yaml
apiVersion: aaestro/v1
kind: Skillpack
metadata:
  name: docs
  version: 1.0.0
  description: 문서 작업용 Agent Skills 묶음
```

`spec` 이 없다 — `skills/` 아래의 디렉토리가 곧 skill 목록이다. 선언에 한 번 더 적게 하면 받아 온
skill 을 수정 없이 담을 수 없고 두 목록이 어긋난다.

### Skillpack Rules

1. **표준 그대로**: `skills/<skill>/` 안은 표준이 정한 그대로다. Aaestro 전용 파일·필드를 요구하지
   않는다. 표준 skill 디렉토리를 넣으면 로드된다
2. **Frontmatter 검증** (위반은 `MOD_003` — 에러가 skill 과 필드를 말한다):
   - `name`: 필수 문자열, 1–64자, `a-z` `0-9` `-` 만, 앞·뒤·연속 하이픈 금지, **디렉토리 이름과 일치**
   - `description`: 필수 문자열, 1–1024자, 공백만은 불가
   - `compatibility`: 있으면 1–500자, 공백만은 불가 / `license`: 문자열
   - `metadata`: 문자열 키 → 값의 맵, 최대 64개. 스칼라 값(숫자·불리언·날짜)은 **적힌 표기 그대로**
     문자열로 받는다 (`version: 1.10` → `"1.10"`) — 가장 흔한 실물 형태다. 컨테이너 값·빈 값은 거부
   - `allowed-tools`: 문자열 — 표준의 실험 필드라 형식만 확인하고 **해석하지 않는다**
   - `SKILL.md` 는 링크가 아닌 일반 파일이고 `---` 로 시작한다. frontmatter 가 없거나, 닫히지
     않았거나, 16 KiB 를 넘거나, YAML 이 아니거나, 매핑이 아니면 거부
3. **경고는 거절이 아니다**: 로드 결과의 `warnings` 로 알린다 (catalog 의 `problems` 와 다른 경로).
   게시와 배포는 막지 않는다. "받아 온 skill 을 수정 없이 담는다" 가 목적이므로, 표준이 금지하지
   않는 흔한 형태는 거절하지 않는다
   - `SKILL.md` 가 500줄을 넘음 (표준의 권고)
   - 표준이 정의하지 않은 최상위 frontmatter 키 — 읽지 않고 이름을 경고에 적는다. 표준 여섯
     필드의 **형식 위반** 은 여전히 거절이다
   - `skills/` 바로 아래의 일반 파일 (`README.md`·`.DS_Store`·`.gitkeep`) — 건너뛴다. 링크와 특수
     파일은 거절이다
4. **경로 격리**: skill 안의 어떤 것도 그 skill 루트 밖을 가리킬 수 없다
   - symlink 는 상대 경로이고 **매 단계** 루트 안에 머물러야 한다 — 절대 경로(루트 안을
     가리켜도), 밖으로 나갔다 돌아오는 경로, 밖으로 나가는 링크나 디렉토리가 아닌 것을 거치는
     경로, 대상 없는 링크, 순환, 특수 파일을 가리키는 링크는 거부. 최종 경로만 보는
     검사(`resolve()`)로 대신하지 않는다
   - 자기에게 되돌아오는 디렉토리 링크(조상을 가리키는 `self -> .`·`up -> ..`, 서로를 거치는
     순환)는 루트 안에 머물러도 거부 — 링크를 따라 복사·마운트하는 쪽이 끝나지 않는다
   - `SKILL.md` 는 링크일 수 없다. `skills/` 와 skill 디렉토리 자체도 링크가 아닌 실제 디렉토리다
   - 하드링크된 파일(`st_nlink > 1`)과 특수 파일(FIFO·소켓·디바이스)은 거부
   - 제어·서식 문자, 백슬래시, 디코딩되지 않는 바이트가 든 파일 이름은 거부
   - **보장의 한계**: 검증은 로드 시점의 트리에 대한 것이다. 내용을 읽는 유일한 파일(`SKILL.md`)은
     링크를 따라가지 않는 디스크립터로 열어 검사와 읽기가 같은 파일을 본다. 검증 뒤에 트리를
     바꿀 수 있는 주체로부터는 지키지 않는다 — 팩을 복사·마운트하는 쪽(#374)이 다시 검증한다
5. **상한 — 비싼 일보다 먼저**: skill 당 항목 200개 · 파일 하나 1 MiB · 총 8 MiB · 디렉토리 8단계 ·
   frontmatter 16 KiB · `metadata` 64개, `skills/` 아래 항목 50개. 값은 `modules/skilltree.py` ·
   `modules/skillpack.py` 의 모듈 상수다 (재료 스토어 상한과 같은 결). 디렉토리는 상한보다 하나
   더까지만 읽고, frontmatter 는 파싱 전에 잰다 — 상한 검사가 비싼 작업 뒤에 오면 상한이 아니다
6. **실행하지 않는다**: 로드·검증·카탈로그·배포 검증 어디서도 skill 디렉토리 안의 코드를 실행하거나
   import 하지 않는다. 여는 파일은 skill 당 `SKILL.md` 하나, 나머지는 `lstat` 만. `scripts/` 는
   **신뢰 경계 안의 코드가 아니다** — 실행된다면 에이전트가 자기 컨테이너 안에서 자기 권한으로 한다
7. **Untrusted input**: frontmatter 값·본문·파일 이름은 신뢰하지 않는 입력이다. 에러·로그에는 필드
   이름과 사유(길이 위반은 길이)만 싣고 값을 싣지 않는다. 원인 체인도 끊는다 — YAML·pydantic
   에러의 문자열은 거절된 값을 싣는다. 본문 속 지시문을 시스템 지시로 승격하지 않는다
   ([03-protocol-integration.md](03-protocol-integration.md) MCP Security 5 와 같은 경계)
8. **검증된 팩만 게시된다**: catalog 는 skillpack 을 전부 검증한 뒤에 ref 를 게시한다 — 선언만
   맞고 skill 이 깨진 팩을 참조한 에이전트는 배포 검증에서 `MOD_001` 로 실패한다. 단건 조회도
   같은 검증을 지난다 (목록은 "게시 안 됨" 인데 단건은 200 이 되지 않게). 읽지 못하는 파일·
   디렉토리도 `MOD_003` 이다 — 날 `OSError` 하나가 카탈로그 목록 전체를 죽이지 않는다
9. **받지 않는 것** (의도된 거절): 문자열이 아닌 `name`·`description`, 비ASCII `name`, 리스트로 적은
   `allowed-tools`, `---` 로 시작하지 않는 `SKILL.md`, 하드링크. 아무도 참조하지 않는 깨진 팩도
   검증을 실패시킨다 (형제 타입과 같은 취급)

### Attachment

```yaml
# agents/<name>/manifest.yaml
spec:
  skillpacks:
    - ref: skillpacks/docs@1.0.0
```

한 에이전트는 같은 팩을 한 번만 선언한다 — 같은 ref 의 중복과 같은 팩의 다른 버전
(`[docs@1.0.0, docs@1.1.0]`) 은 manifest 검증에서 거부된다. 서로 다른 팩 사이의 skill 이름 충돌은
#374 에서 다룬다.

**v0.1 의 범위는 선언과 검증까지다.** agentd 는 `spec.skillpacks` 를 아직 쓰지 않는다 — skill 을
모델에 노출하고 컨테이너에 마운트하는 것은 #374 다. 선언이 조용히 무시되지 않도록 agentd 는 기동 시
`skillpacks declared but not loaded by this executor` 를 WARN 으로 남긴다.

## Graph Modules — Modular Agent Wiring

그래프는 에이전트를 잇고 분리하는 **배선 모듈**이다. 에이전트 연결 변경은 그래프 파일
수정만으로 완료되어야 한다.

### Graph Topology Specification

```yaml
# graphs/research-pipeline.yaml
apiVersion: aaestro/v1
kind: Graph
metadata:
  name: research-pipeline
  version: 1.0.0
  description: 질의 → 계획 → 리서치 → 작성 파이프라인

spec:
  mode: mission                 # mission(달성형, 기본) | service(상주형) — 01 참조
  goal: 질의를 받아 계획 → 리서치 → 보고서 작성을 완료한다   # 문서화 필수

  state:
    schema: aaestro.graphs.schemas:ResearchState   # pydantic 모델 ref
    checkpointer: default                          # 프레임워크 설정 상속

  nodes:
    - id: planner
      agent: agents/planner@0.1.0
      input_map:                  # state → TaskRequest.input 매핑
        query: state.query
      output_map:                 # TaskResult.output → state 병합 매핑
        plan: output.plan

    - id: researcher
      agent: agents/researcher@0.1.0
      input_map: {plan: state.plan}
      output_map: {findings: output.findings}

    - id: writer
      agent: agents/writer@0.1.0
      input_map: {findings: state.findings}
      output_map: {report: output.report}

  edges:
    - {from: START, to: planner}
    - from: planner
      to: researcher
      condition: aaestro.graphs.conditions:needs_research   # 조건 함수 ref
    - {from: planner, to: END, condition: aaestro.graphs.conditions:plan_only}
    - {from: researcher, to: writer}
    - {from: writer, to: END}

  connections:                    # A2A 직접 호출 allowlist — 03 참조
    - {caller: researcher, callee: planner}
```

### Service Mode Example — 무한 반복 과업

```yaml
# graphs/feed-monitor.yaml
apiVersion: aaestro/v1
kind: Graph
metadata:
  name: feed-monitor
  version: 1.0.0
  description: 피드 상시 감시 → 신규 항목 분류/알림

spec:
  mode: service
  goal: 등록된 피드를 상시 감시하고 신규 항목을 분류하여 알린다

  service:                       # service 모드 전용 설정
    idle:                        # 필수 — busy-loop 방지
      min_delay_s: 30
      max_delay_s: 600           # 작업 없을 때 exponential backoff
    max_failure_streak: 5        # iteration 연속 실패 임계 — 초과 시 run 정지 + 알림

  state:
    schema: aaestro.graphs.schemas:FeedMonitorState

  nodes:
    - id: watcher
      agent: agents/feed-watcher@0.1.0
    - id: classifier
      agent: agents/classifier@0.1.0
    - id: notifier
      agent: agents/notifier@0.1.0

  edges:
    - {from: START, to: watcher}
    - {from: watcher, to: classifier, condition: aaestro.graphs.conditions:has_new_items}
    - {from: watcher, to: END, condition: aaestro.graphs.conditions:idle}  # 이번 iteration 종료
    - {from: classifier, to: notifier}
    - {from: notifier, to: END}
```

### Decision Nodes — 판정을 state 에 넣는 노드

노드는 LLM 태스크 대신 **결정 하나**를 낼 수 있다. 리뷰 순환의 1차 판정과 분기용 불리언이
여기서 나온다 ([01-architecture.md](01-architecture.md) Decision Models 쓰임 3·4).

```yaml
# graphs/draft-review.yaml (발췌) — 초안 → 1차 판정 → (통과) END / (아니면) LLM 리뷰 → 재작성
spec:
  state:
    fields:
      query: {type: string, required: true}
      draft: {type: string}
      screen: {type: string}          # act | uncertain | reject
      approved: {type: boolean, default: false}
      notes: {type: array}

  nodes:
    - id: writer
      agent: agents/writer@0.4.0
      input_map: {query: state.query, notes: state.notes}
      output_map: {draft: output.draft}

    - id: screen                      # 결정 노드 — LLM 루프 없이 결정 한 번
      agent: agents/planner@0.4.0     # 결정을 내리는 에이전트: provider·decisionset 은 그 manifest
      decision:
        question: review.draft_meets_goal          # alias.question — planner 의 spec.decision.sets
        state: {query: state.query, draft: state.draft}   # 질문의 state 조각 ← graph state
      output_map: {screen: decision.band}          # decision.value | band | probability | distribution

    - id: reviewer                    # LLM 리뷰어 — notes 는 생성이라 결정 모델이 못 한다
      agent: agents/planner@0.4.0
      input_map: {query: state.query, draft: state.draft}
      output_map: {approved: output.approved, notes: output.notes}

  edges:
    - {from: START, to: writer}
    - {from: writer, to: screen}
    - {from: screen, to: END, condition: 'state.screen == "act"'}   # 확신 있게 통과
    - {from: screen, to: reviewer}                                  # uncertain · reject · unavailable → 원래 경로
    - {from: reviewer, to: END, condition: state.approved}
    - {from: reviewer, to: writer, condition: not state.approved, max_iterations: 3}
```

1. **`decision` 은 `agent` 와 함께**: 결정을 내리는 에이전트를 가리킨다 — 그 에이전트의
   `spec.decision` 이 provider 와 decisionset 을 정한다 (없으면 배포 검증 `MOD_001`).
   promptset 템플릿은 필요 없다 (04 호환성 규칙 3 의 node_id 집합에서 제외)
2. **`state` 는 질문의 조각을 채운다**: decisionset 이 선언한 `state` 이름 → graph state 키.
   빠지거나 남으면 `MOD_004`. 값은 텍스트로 직렬화된다
3. **`output_map` 은 `decision.` 만 읽는다**: `value` / `band` / `probability` / `distribution`.
   `output.` 을 읽으면 검증 실패 — 결정 노드에는 모델 출력이 없다
4. **기본 edge 가 하나 있어야 한다**: `uncertain` 과 `unavailable` 은 원래 경로로 가야 한다
   ([02](02-agent-implementation.md) Decision Tasks 4). 조건 없는 out-edge 가 없는 decision 노드는
   검증 실패 (`GRAPH_001`)
5. **조건은 state 를 읽는다**: 결정은 노드가 state 에 남기고 조건은 그것을 읽는다. 조건 식
   안에서 결정 모델을 부르는 형태는 없다 — 재개 시 재판정하면 같은 run 이 다른 길로 간다
6. **결정 노드는 싸고 빨라야 한다**: `timeout_s` 기본은 `decision.timeout_s`(5초). 결정
   노드가 LLM 노드만큼 느리면 그 노드는 제자리가 아니다

### Graph Rules

1. **Config Over Code**
   - 노드 추가/제거, edge 연결/분리는 그래프 YAML 수정만으로 완료
   - 그래프별 Python 코드는 state schema 와 조건 함수뿐 — 배선 로직 코드 작성 금지
2. **Validation** (배포 시, 실패하면 배포 중단)
   - 모든 `agent` ref 해석 가능
   - dangling edge 없음 (from/to 가 노드 or START/END)
   - START 에서 모든 노드 도달 가능 (END 도달 요건은 아래 mode 규칙)
   - `input_map` 의 state 키가 state schema 에 존재
   - conditional edge 의 조건 함수 import 가능
   - `connections` 의 caller/callee 가 모두 그래프 노드
   - decision 노드: 질문 참조가 그 노드 에이전트의 decisionset alias 로 해석되고, `state` 가
     질문의 조각과 일치하며, `output_map` 이 `decision.` 만 읽고, 조건 없는 out-edge 가 하나 있다
3. **Mode & Cycle Policy**
   - **두 모드 공통**: END 도달 필수. 순환 edge (self-loop 포함, 재시도/refinement
     패턴) 는 허용하되 `max_iterations` 명시 필수 — 미명시 시 검증 실패
   - `mission` (기본): END 도달 시 run 완료, 최종 state 반환
   - `service`: **한 iteration = START → END 한 바퀴**. 반복은 그래프가 아니라
     `ServiceRunner` 가 소유한다 — 그래프가 스스로 순환하면 한 번의 실행이 끝나지
     않아 iteration 경계가 성립하지 않는다. 종료는 운영자 stop / 명시적 stop 조건.
     `service.idle` (backoff) 미선언 시 검증 실패.
     Iteration 마다 checkpoint, `max_failure_streak` (기본 5) 초과 시 정지 + 알림.
     Iteration 간 state 연속성이 불필요하면 service 대신 스케줄 반복 mission 사용
     ([01-architecture.md](01-architecture.md) Mode Rules)
4. **State Schema**
   - pydantic 모델로 정의, 노드 산출물 병합은 `output_map` 으로만
   - 노드가 state 전체를 덮어쓰는 패턴 금지 — 선언된 키만 병합
5. **Subgraphs**: 그래프는 다른 그래프를 노드로 참조 가능
   (`graph: graphs/sub-review@1.0.0`) — 순환 참조는 검증에서 차단

### Attach / Detach Semantics

| 작업 | 방법 | 재배포 범위 |
|---|---|---|
| 에이전트를 그래프에 추가 | nodes + edges 에 항목 추가 | 그래프 리로드 (기존 run 은 기존 버전으로 완주) |
| 에이전트를 그래프에서 분리 | nodes/edges/connections 에서 제거 | 그래프 리로드. 참조가 0이 된 에이전트 컨테이너는 drain 후 정리 |
| 에이전트 버전 교체 | `agent:` ref 의 버전만 변경 | 새 버전 컨테이너 기동 → health OK → 트래픽 전환 → 구버전 drain |
| 연결(A2A) 추가/제거 | connections 수정 | 그래프 리로드 (edge token 재발급) |

- 실행 중인 run 은 시작 시점의 그래프 버전으로 완주한다 (mid-run 토폴로지 변경 금지)
- 그래프 버전도 semver — 토폴로지 변경 시 bump

## Module Registry

### v0.1: Filesystem Registry

```
modules/
├── skillsets/{name}/{version}/skillset.yaml
├── promptsets/{name}/{version}/promptset.yaml
├── memorysets/{name}/{version}/memoryset.yaml
├── decisionsets/{name}/{version}/decisionset.yaml
├── skillpacks/{name}/{version}/skillpack.yaml      # + skills/{skill}/SKILL.md
agents/{name}/manifest.yaml            # 버전은 manifest 내부 선언
graphs/{name}.yaml
```

1. **Resolution**: `registry.resolve(ref)` 가 유일한 해석 경로 — 경로 하드코딩 금지
2. **Immutability**: 게시된 버전 디렉토리는 수정 금지 — 변경은 새 버전으로
3. **Integrity**: resolve 시 kind/name/version 이 ref 와 일치하는지 검증
4. **Scope Neutrality**: 모듈 아티팩트는 전역 레지스트리 소속 — 리소스 스코프
   (global/group/local, [01-architecture.md](01-architecture.md)) 는 런타임 리소스
   (secrets/memory/artifact/quota) 에 적용되며, 모듈 가시성은 제한하지 않는다 (v0.1)

### Build Materials — 레지스트리 옆의 재료 스토어

커스텀 에이전트의 빌드 입력(`Dockerfile`, `src/`)은 레지스트리 파일 트리에 두지 않고
**재료 스토어**에 둔다 (`orchestrator.material_store`, SQLite).

```
(agent, version) → { "Dockerfile": ..., "src/agent.py": ..., ... }
```

1. **키**: 에이전트 이름 + manifest 의 `metadata.version` — 재료는 선언된 버전에 묶인다
2. **Immutability**: 레지스트리 규칙 2 와 같다 — 같은 버전에 다른 내용은 `MOD_002`.
   삭제도 되돌리지 않는다 (삭제 후 같은 버전 재등록으로 불변성을 우회하지 못한다)
3. **In Use**: 배포 중인 에이전트의 재료는 바꾸거나 지우지 못한다
4. **Rules**: 경로는 `Dockerfile` 또는 `src/` 아래만 (정규화된 상대 경로), 파일 수·크기 상한,
   텍스트만 — 저장 시 검사 (`VAL_002`)
5. **Build Record**: 굽기 결과(`built`/`failed`/`building`, 태그, 로그 꼬리)는 빌드 스토어
   (`orchestrator.build_store`) 에 남고, 배포 게이트가 이것을 본다 (상태와 기록의 태그 모두)
   ([02-agent-implementation.md](02-agent-implementation.md) Lifecycle Rule 1)
6. **Seeds**: 저장소에 함께 싣는 예시 재료는 `examples/materials/<agent>/` 에 둔다 —
   프레임워크는 읽지 않으며, `aaestro agent-push` 로 스토어에 올린다

### Compatibility Rules

1. 에이전트 manifest 는 모듈의 **정확한 버전**을 참조 (범위 지정 없음, v0.1 단순화)
2. Skillset 의 `requires.env` ⊆ agent manifest 의 `env_allowlist` — 배포 검증
3. Promptset 템플릿 이름 ⊇ 그래프에서 해당 에이전트가 사용하는 node_id 집합
   (agentd 가 `task.node_id` 로 템플릿을 선택하기 때문)
4. Direct 요청(그래프 밖 단독 태스크, [02-agent-implementation.md](02-agent-implementation.md))
   을 받는 에이전트의 promptset 은 `default` 템플릿 포함 필수
5. Breaking change 기준 (semver):
   - Skillset: tool 이름/시그니처 변경·삭제 = **major** /
     하위 호환 추가 (신규 tool, optional 파라미터) = minor
   - Promptset: 필수 변수 추가·삭제 등 변수 스키마 breaking 변경 = **major** /
     optional 변수 추가·문구 수정 = minor/patch
   - Memoryset: 임베딩 모델/차원 변경 = minor 이상 (전체 재인덱싱 수반)
   - Decisionset: 질문 삭제·이름 변경·`kind` 변경·보기/등급 집합 변경 = **major** /
     `ask` 문구 수정 = minor (구간 재보정 수반) / 구간만 조정 = patch
   - Skillpack: skill 삭제·이름 변경 = **major** / skill 추가·`description` 변경(모델이 skill 을
     고르는 근거가 바뀐다) = minor / 본문·`references/`·`scripts/`·`assets/` 수정 = patch
   - Graph: state schema 변경 = **major**

## Testing Modules

모듈별 최소 테스트 기준 (상세는 [06-testing.md](06-testing.md)):

- **Skillset**: skill 단위 유닛 테스트 + schema 생성 스냅샷 테스트
- **Promptset**: 변수 스키마 검증 테스트 + 렌더링 골든 테스트 (스냅샷)
- **Memoryset**: 정책 스키마 검증 + recall 예산/threshold 적용 테스트
  ([09-memory-context.md](09-memory-context.md))
- **Decisionset**: 스키마 검증(종류·보기 수·구간 순서) + 라벨 데이터로 구간을 확인하는
  calibration 테스트 ([06-testing.md](06-testing.md))
- **Skillpack**: frontmatter 규칙별 거절·허용 양쪽(경계값) + 경로 탈출(상대·절대·체인·나갔다
  돌아오는 링크·하드링크·특수 파일) + 상한(초과·경계, "상한 + 1 까지만 읽는다") +
  "로드해도 `scripts/` 가 실행되지 않는다"(표식 파일) + 검사와 읽기 사이의 교체(결정적 재현) +
  읽지 못하는 경로(실제 권한)
- **Graph**: 토폴로지 검증 테스트 + fake agent 로 라우팅 시나리오 테스트 — decision 노드는
  FakeDecisionModel 로 act / uncertain / unavailable 세 경로
