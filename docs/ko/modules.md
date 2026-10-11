# 모듈 시스템

한국어 | **[English](../en/modules.md)**

Malkuth 에서 배포 가능한 모든 것은 모듈입니다: 솔루션은 버저닝된 모듈의 배선으로
조립되며, 프레임워크 코드를 수정하지 않습니다. 규범 규칙:
[04-module-system.md](../../.claude/rules/04-module-system.md),
[09-memory-context.md](../../.claude/rules/09-memory-context.md).

## 모듈 타입

| 타입 | 선언 내용 | 위치 |
|---|---|---|
| **Skillset** | Python tool (능력) | `modules/skillsets/{name}/{version}/` |
| **Promptset** | Jinja2 템플릿 + 변수 스키마 | `modules/promptsets/{name}/{version}/` |
| **Memoryset** | 메모리 정책 — scope, 인덱스, 보존, recall | `modules/memorysets/{name}/{version}/` |
| **Agent** | 계약 — 모델, 모듈, 프로토콜, 리소스 | `agents/{name}/manifest.yaml` |
| **Graph** | Goal — nodes, edges, connections, mode | `graphs/{name}.yaml` |
| **Group** | 리소스 스코프 경계 — quota, secrets, 메모리 | `groups/{name}.yaml` |

## 참조 형식

```
{type}/{name}@{version}     예: skillsets/web-search@0.2.0
```

- 항상 정확한 semver — `latest`, 브랜치, 커밋 해시 금지
- 참조 해석은 registry 가 담당 — 경로 하드코딩 금지
- 게시된 버전 디렉토리는 불변 — 변경은 새 버전으로

## 스킬셋

Skill 은 async Python 함수이며, tool 스키마는 시그니처와 docstring 에서 자동 생성됩니다
— 수기 JSON schema 없음.

```python
@skill
async def search(
    ctx: SkillContext, query: str, depth: Literal["quick", "deep"] = "quick"
) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색할 내용
        depth: 결과 링크를 얼마나 따라갈지
    """
    ...
```

- docstring 첫 문단은 tool 설명이 되고, `Args:` 의 각 항목은 그 파라미터의 `description` 이
  됩니다. 설명이 없는 파라미터는 타입만 모델에 닿습니다.
- 타입은 다른 계약과 같은 규칙으로 pydantic 이 변환합니다: `Literal` 과 `StrEnum` 은
  `enum`, pydantic 모델과 `list[Model]` 은 object 스키마가 됩니다. 중첩 모델은 최상위
  `$defs` 에 모여 `#/$defs/...` 참조가 풀립니다.

핵심 규칙: async-first, secrets/로깅은 `SkillContext` 로만 접근, timeout 은
`skillset.yaml` 에 선언, 실패는 예외로 (agentd boundary 에서 변환).

**`SkillContext` 는 런타임에 import 하세요 — `TYPE_CHECKING` 뒤에 두지 마세요.**
tool 스키마는 `get_type_hints()` 로 시그니처에서 도출됩니다. 어노테이션의 이름이
런타임에 해석되지 않으면 그 호출이 실패하고, 프레임워크는 **스키마 없이** 진행합니다 —
모델은 타입 없는 파라미터를 보게 되어 어떤 값을 넣어야 할지 알 수 없습니다:

```python
# 잘못됨 — get_type_hints() 가 NameError, 모든 파라미터가 타입을 잃는다
if TYPE_CHECKING:
    from malkuth.core.skill import SkillContext

# 올바름
from malkuth.core.skill import SkillContext, skill
```

동적 정의 skill 을 막지 않기 위해 에러가 아닌 경고로 드러냅니다
(`skill type hints could not be resolved` / `skill parameters have no type`).
`LoadedSkillset.untyped_parameters()` 가 tool 별로 같은 내용을 보고합니다.

## 프롬프트셋

템플릿은 그래프 `node_id` (direct 요청은 `default`) 로 선택됩니다. 변수는 스키마로
선언되며, 미선언 변수 사용 시 조용히 빈 값이 되지 않고 렌더 실패(`MOD_004`) 합니다.
Locale 오버라이드는 `locales/{lang}/`. 프롬프트 문구 변경도 반드시 version bump.

## 메모리셋

Memoryset 은 memory space 의 정책을 고정합니다: scope (`run | local | group | global`),
임베딩 모델(버전 고정), 청킹, 하이브리드 검색 가중치, 보존/compaction, recall 기본값
(k, 최소 점수, 토큰 예산). 부착 위치는 scope 와 일치해야 합니다: manifest(local),
그래프(run), `groups/<name>.yaml`(group), `groups/global.yaml`(global).

## 그래프

그래프는 배선 모듈입니다: 에이전트 추가/분리는 YAML 변경만으로 완료됩니다.

- `mode: mission` — END 에서 종료; cycle 은 `max_iterations` 필수
- `mode: service` — 상주형; idle backoff 정책 필수, iteration 마다 checkpoint
- `connections` — A2A peer 호출 allowlist (방향 유의, peer 는 동등)
- 배포 시 검증이 dangling ref / 미도달 노드 / mode 위반을 차단
- 노드는 **한 번에 한 길로만** 나간다: `condition` 없는 edge 는 노드당 하나까지다.
  조건 없는 edge 가 둘이면 두 대상이 병렬로 돌고, 반복 카운터 `_iterations` 를 포함한 공유
  state 에 두 분기를 합치는 규칙이 없어 run 이 도중에 죽는다. 대신 edge 에 조건을 준다 — 조건
  edge 옆의 조건 없는 edge 하나는 기본 경로다

### YAML 안의 state 와 조건

새 그래프에 코드가 필요 없다. state 필드를 선언하고 조건은 식으로 쓴다:

```yaml
spec:
  state:
    fields:
      query: {type: string, required: true}
      plan: {type: string}
      needs_research: {type: boolean, default: true}
      findings: {type: array}
  edges:
    - {from: planner, to: researcher, condition: state.needs_research}
    - {from: planner, to: END, condition: not state.needs_research}
```

- **필드 타입**: `string`, `integer`, `number`, `boolean`, `array`, `object`. `required: true`
  가 아니면 선택 필드이고, `default` 없는 선택 필드는 비어 있는 채(`array`, `object`) 또는
  null 로 시작한다. `_` 로 시작하는 이름은 프레임워크 예약이다.
- **조건**은 state 만 읽는다: `state.<field>` (중첩 값은 `.key`), 리터럴(`true`, `false`,
  `null`, 숫자, 문자열, `[...]`), 비교(`==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`),
  `and` / `or` / `not`. 값 하나만 쓰면 참/거짓으로 읽는다 — 빈 목록·`0`·`null` 은 거짓이다.
  함수 호출과 `state` 밖의 것은 그래프를 읽을 때 거부된다 (`GRAPH_001`). `null < 3` 처럼
  비교할 수 없는 값을 비교하면 다른 가지로 조용히 넘어가지 않고 run 이 실패한다
  (`GRAPH_003`).
- **Deprecated**: `state.schema` 와 import ref 로 쓴 조건
  (`malkuth.graphs.conditions:needs_research`) 은 한 버전 동안 그대로 동작한다. 가리킬 수
  있는 곳은 `malkuth.graphs` 뿐이고, 검증이 import ref 조건마다 경고를 남긴다.

## 에이전트 — 모델 회복력

에이전트의 `spec.model` 은 primary 모델이 실패했을 때의 동작을 선언할 수 있습니다. 아래 필드는
모두 선택이며, 생략하면 동작은 그대로입니다. 검증, 해석, 실행 중 전환 루프는 들어와 있습니다.
같은 provider 안의 키 교체(#360)와 비-Anthropic 어댑터(#361)가 뒤따릅니다.

```yaml
spec:
  model:
    provider: anthropic
    name: claude-sonnet-5
    max_tokens: 8192
    effort: high
    fallbacks:                      # primary 가 `on` 의 사유로 실패하면 순서대로 시도
      - {provider: anthropic, name: claude-haiku-4-5, on: [rate_limit, server_error]}
    retry:                          # 카테고리별 재시도; 생략 시 프레임워크 기본값
      rate_limit: {start_s: 10, attempts: 5}        # max_s 선택 (>= start_s)
      network:    {start_s: 1,  attempts: 3}
    circuit: {max_failures: 5, reset_s: 60}
```

프레임워크가 아는 provider(현재 `anthropic`)만 검증을 통과합니다. 두 번째 provider 는
OpenAI 호환 어댑터(#361)와 함께 들어옵니다.

- `on` 값은 `rate_limit`, `server_error`, `context_overflow` 이며 필수이고 비어 있을 수 없습니다.
- `max_tokens` 를 지정하면 양의 정수여야 합니다.
- fallback 의 `(provider, name)` 은 primary 와, 그리고 다른 fallback 과 달라야 합니다.
  모르는 키는 거부됩니다.

### 해석 — 가까운 계층이 이긴다

리소스 스코프 규칙(local > group > global)과 같습니다. 뒤 계층이 앞 계층을 덮습니다:

1. 에이전트 manifest `spec.model` (기본값)
2. 그래프 노드 `nodes[].model` — 같은 에이전트라도 노드마다 다른 모델·fallback
3. `POST /v1/deployments` 의 `model_overrides` (노드 id 키) — 선언 파일을 바꾸지 않고 운영자가 교체

override 는 **부분 선언**입니다: 적은 키만 아래 계층을 덮습니다. `fallbacks: []`, `circuit: null`
처럼 명시한 빈 값·null 은 아래 계층의 값을 지웁니다. `provider` 를 적으면 `name` 도 적어야 합니다
(모델 이름은 provider 에 속합니다). fallback 을 primary 로 올릴 때는 `fallbacks` 도 함께 조정해야
합니다 — primary 와 같은 fallback 은 거절됩니다. 서브그래프 노드에는 `model` 을 둘 수 없습니다.

```yaml
# graphs/research-pipeline.yaml (발췌)
nodes:
  - id: researcher
    agent: agents/researcher@0.1.0
    model:
      name: claude-opus-5
      fallbacks: []                # 이 노드에서는 fallback 없이
```

해석 결과는 `TaskRequest.config.model` 로 agentd 에 전달됩니다. agentd 는 **태스크 시작 시 그
선언으로 모델 체인을 고정**하고 primary 부터 그 모델로 호출합니다. 리로드가 진행 중 태스크에 섞이지
않습니다 (모듈 묶음과 같은 규칙). 선언이 없으면 — 그래프 밖 direct 요청 — manifest 기본값을 씁니다.

### 실행 중 전환

전환은 **턴 단위**입니다. primary 의 재시도 정책(`retry` 선언, 없으면 기본값: rate limit 10초 시작
5회, network 1초 시작 3회)이 소진되면, 실패 부류가 `on` 에 든 다음 fallback 에 **같은 대화**를
보냅니다. `on` 에 없는 부류면 전환 없이 태스크가 실패합니다. 마지막 fallback 도 소진되면 마지막
에러를 돌려줍니다.

| `on` 부류 | 대응하는 실패 |
|---|---|
| `rate_limit` | provider rate limit (`LLM_001`) |
| `server_error` | provider 5xx (`LLM_003`), 그리고 네트워크 오류·타임아웃 (provider 가 답하지 못함. 어댑터가 연결 실패는 `NET_001`, 요청 타임아웃은 `NET_002` 로 매핑하며, 전환 전에 `network` 재시도 정책이 먼저 재시도한다) |
| `context_overflow` | `LLM_002` — 재시도 없이 곧장 전환 ("더 긴 컨텍스트의 모델로 같은 요청") |

해석 불가 응답(`LLM_004`)과 설정 오류는 전환하지 않습니다.

- **`circuit`**: 모델(provider/name)마다 circuit breaker 가 있습니다. open 인 모델은 **호출 없이**
  건너뜁니다. 건너뛴 자리는 **그 breaker 를 연 실패 부류**로 채워지므로 (rate limit 으로 열렸으면
  `rate_limit`, 그 밖에는 `server_error`) 같은 부류를 `on` 에 둔 fallback 이 이어받습니다. 재시도
  도중 breaker 가 열리면 남은 재시도는 보내지 않고 곧장 다음 후보로 갑니다. 같은 모델은
  태스크·체인이 달라도 breaker 를 공유합니다. 모든 후보가 open 이면 마지막 breaker 를 연 실패
  (`LLM_001` 또는 `LLM_003`)로, retryable 하게 실패합니다. 상태는 `malkuth_circuit_state{target="model:<provider>/<name>"}`
  로 노출됩니다.
- fallback 은 `max_tokens`·`effort` 를 primary 에서 물려받습니다.
- **관측**: `TaskResult.usage.model` / `.provider` 는 **마지막 턴에 응답한** 모델이고, 토큰
  합계는 턴을 가로질러 합산됩니다. 시도별 정확한 값은 `malkuth_model_requests_total` /
  `malkuth_model_tokens_total` 라벨에 있습니다.
  `malkuth_model_requests_total` 에 `fallback_index` 라벨이 붙습니다 (0 = primary). 전환할 때마다
  `model`, `provider`, `fallback_index`, `error_code`, `next_model`, `next_provider` 필드의 WARN
  로그를 남기고, open circuit 을 건너뛸 때도 `opened_by` 필드의 WARN 을 남깁니다.
- 범위 밖: 비-Anthropic provider 로의 전환은 어댑터(#361)가 들어와야 합니다 — 그 전에는 체인을 만들 때
  `CFG_001` 로 거절됩니다. 같은 provider 의 키 교체는 #360 입니다.

### 배포 검증 (`model_providers`)

검사 이름은 `model_providers` 이고, finding 코드는 `VAL_002` 이며, HTTP 응답은 `400 VAL_001` 에
finding 이 `details.findings` 로 실립니다. 하나라도 걸리면 아무것도 기동하지 않습니다:

- provider 는 프레임워크가 아는 것(논리 호스트가 있는 것)이어야 합니다
- 이그레스 프록시를 쓰면 provider 가 `runtime.egress_proxy.providers` 에 있어야 합니다 — 아니면
  런타임에 fallback 이 프록시 404 를 맞는 조용한 실패가 됩니다
- 노드·배포 override 는 **manifest 가 선언한 provider 집합(primary + fallbacks) 안에서만**
  provider 를 바꿀 수 있습니다: 이그레스 판정 기본값이 manifest 에서 도출되므로 그 밖의 provider 는
  프록시가 거부합니다. 다른 provider 로 바꾸려면 manifest 의 `fallbacks` 에 먼저 선언합니다
- 그래프에 없는 노드 id 를 가리키는 `model_overrides` 나 잘못된 override 본문(provider 만 적음,
  모르는 키)은 곧바로 `400 VAL_002` 입니다
- 이그레스 baseline 은 fallback provider 의 호스트를 자동 포함합니다 (manifest 선언이 곧 기본 권한)

`malkuth validate` 와 `malkuth deploy` 출력의 `models` 항목이 노드별로 해석된 모델 계층을
보여 줍니다.

## 그룹

그룹은 멤버 에이전트의 리소스(quota, secrets, 그룹 메모리)를 스코프합니다.
배선에는 영향이 없습니다: 같은 그룹이라도 서로 호출하려면 `connections` 선언이
필요합니다. 리소스 해석은 **local > group > global**.

## 호환성과 버저닝

- 에이전트는 정확한 모듈 버전을 고정 참조; 배포 검증이 요구사항을 대조
  (예: skillset `requires.env` ⊆ agent `env_allowlist`)
- Breaking change 기준 (semver): tool 이름/시그니처 변경·삭제, 필수 프롬프트 변수
  변경 → **major**; 하위 호환 추가 → minor; 그래프 state schema 변경 → major;
  임베딩 모델 변경 → minor 이상 + 전체 재인덱싱
