# 모듈 시스템

한국어 | **[English](../en/modules.md)**

Aaestro 에서 배포 가능한 모든 것은 모듈입니다: 솔루션은 버저닝된 모듈의 배선으로
조립되며, 프레임워크 코드를 수정하지 않습니다. 규범 규칙:
[04-module-system.md](../../.claude/rules/04-module-system.md),
[09-memory-context.md](../../.claude/rules/09-memory-context.md).

**모르는 키는 거절됩니다.** 모든 선언(에이전트 manifest, 그룹, 그래프, 스킬셋, 프롬프트셋,
메모리셋)은 엄격하게 검증됩니다: 스키마가 모르는 키는 로드 · 배포 검증(`aaestro validate`,
`POST /v1/validate`, 배포) · `PUT` 을 실패시키고, 에러는 문제의 키마다 `{field, problem}` —
경로와 사유 — 을 알려 주며 **값은 싣지 않습니다**(잘못 놓은 `env: {KEY: 비밀}` 이 비밀을
되돌려 보내면 안 됩니다. 거절된 입력 — 값 전체든 8자 이상의 조각이든 — 을 인용하는 validator
메시지는 `<redacted>` 로 가려지고, 원래 예외는 체인으로 잇지 않아 traceback 이나 `exc_info`
로그도 값을 싣지 않습니다). 이 보장의 범위는 정확히 이렇습니다: 스키마가 **거절한** 값은
나가지 않습니다. 스키마를 통과한 선언의 **식별자** — 모듈 ref, 에이전트 · 그룹 · 노드 · provider
이름 — 는 의미 검증 (`aaestro validate`, 배포) 의 finding 에 나갑니다. 운영자가 선언을 고치려면
보여야 하기 때문입니다. **env 키 이름은 나가지 않습니다**: finding 은 위치로 가리킵니다
(`spec.runtime.env_allowlist[2]`, 스킬셋의 `requires.env[0]`). 키 이름 자리에 비밀 값 자체를 적는
실수가 흔하기 때문입니다. 중복을 거절하는 메시지도 값이 아니라 위치를 말합니다
(`duplicate node id at index 1 and 3`). **키 이름은 나갑니다** — 모르는 키 · 매핑 키 · 중복 키는 `field` /
`reason` 에 실립니다 (64자로 자르고 인쇄할 수 없는 문자는 바꿉니다). 그러니 값을 키 자리에
적지 않습니다. 선언을 그 위치와 대조하는 검사 (모듈의 `kind` · 이름 · 버전과 ref, 선언된 이름과
파일 위치) 는 기대 값만 말하고 선언에 적힌 값은 싣지 않습니다. 한 YAML
매핑에서 같은 키를 두 번 적는 것도 (키와 줄 번호와 함께, merge key `<<` 는 그대로 동작)
거절되므로, 좁힌 줄이 뒤의 넓은 줄에 조용히 덮이지 않습니다. YAML 문법 오류는 줄과 열만
알려 주고 줄 내용은 싣지 않으며, UTF-8 이 아닌 파일은 `not valid UTF-8` 로, YAML 이 값으로
만들 수 없는 것 (`!!int abc`, `2024-02-30`) 은 `YAML value could not be constructed at line N`
으로 보고됩니다. `aaestro validate` 와 프로세스 진입점(agentd · control
plane · Memory Service)은 파일과 문제의 키를 한 줄로 출력하고 비정상 종료합니다. 그래서 좁히는 선언의
오타(`allowed_tool`, `egres`, `mod`)가 더 넓은 기본값으로 바뀌지 않습니다. 규약에는 있으나
v0.1 스키마가 아직 받지 않는 선언(`spec.decision`, 그래프 노드 `decision`, memoryset
`compaction.importance` 등)도 같은 방식으로 거절됩니다.

컨테이너 안의 에이전트 데몬도 control plane 과 같은 엄격도로 manifest 를 검증하므로 "검증을
통과한 선언"의 뜻이 프로세스마다 같습니다. 감수하는 점: 스키마에 나중에 추가된 필드를 쓰는
선언은 그 필드를 아는 이미지에서만 기동합니다.

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
    from aaestro.core.skill import SkillContext

# 올바름
from aaestro.core.skill import SkillContext, skill
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
  (`aaestro.graphs.conditions:needs_research`) 은 한 버전 동안 그대로 동작한다. 가리킬 수
  있는 곳은 `aaestro.graphs` 뿐이고, 검증이 import ref 조건마다 경고를 남긴다.

## 에이전트 — 모델 회복력

에이전트의 `spec.model` 은 primary 모델이 실패했을 때의 동작을 선언할 수 있습니다. 아래 필드는
모두 선택이며, 생략하면 동작은 그대로입니다. 검증, 해석, 실행 중 전환 루프는 들어와 있습니다.
fallback 은 provider 를 넘을 수 있습니다 ([Provider](#provider) 참조). 같은 provider 안의 키 교체(#360)가
뒤따릅니다.

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

프레임워크가 아는 provider(`anthropic`, `openai-compat`)만 검증을 통과합니다.

### Provider

`spec.model.provider` (그리고 각 fallback 의 `provider`)는 어댑터 이름입니다. 묶인 것은 둘이고, 그 밖의
이름은 `CFG_001` 로 거절됩니다 — 기본 provider 로 떨어지지 않습니다.

| `provider` | API | 닿는 길 |
|---|---|---|
| `anthropic` | Anthropic Messages API | `ANTHROPIC_BASE_URL`, `ANTHROPIC_API_KEY` |
| `openai-compat` | OpenAI Chat Completions 와 그것을 말하는 서버 (vLLM, Ollama, 게이트웨이) | `OPENAI_COMPAT_BASE_URL`, `OPENAI_COMPAT_API_KEY` |

```yaml
spec:
  model:
    provider: openai-compat
    name: gpt-5-mini               # base URL 의 서버가 서빙하는 모델 id
    fallbacks:
      - {provider: anthropic, name: claude-haiku-4-5, on: [rate_limit, server_error]}
```

fallback 은 어느 방향으로든 provider 를 넘을 수 있습니다. 다른 provider 의 모델이 시작한 턴(결과를
기다리는 도구 호출)도 fallback 이 이어받습니다: 대화를 fallback 의 메시지 형식으로 다시 짓습니다.

`openai-compat` 에만 해당하는 것:

- **어디를 가리키는지는 코드가 아니라 설정입니다.** 기본 주소가 없습니다. 이그레스 프록시 뒤에서는
  runtime 이 `OPENAI_COMPAT_BASE_URL` 을 프록시로 두고 키 자리에 에이전트 신원을 넣습니다. 실 키와
  upstream 주소는 프록시가 쥡니다 ([api.md](api.md#이그레스-강제)). 프록시가 없으면 둘을
  `runtime.agent_env` / 에이전트 secrets 로 줍니다. 이 provider 를 선언하고 두 설정이 없으면 에이전트가
  기동에 실패합니다 (`CFG_001`).
- **base URL 은 `/v1` 앞까지입니다.** 어댑터와 프록시가 `v1/chat/completions` 를 붙입니다.
  `https://api.openai.com`, `http://vllm:8000`, `http://ollama:11434`, 그리고 경로 접두가 있는
  게이트웨이 `https://gateway.example.com/llm` 은 맞고, `https://api.openai.com/v1` 은 틀립니다 —
  프록시가 그 값으로는 기동을 거부합니다 (`CFG_001`).
- **`effort` 는 보내지 않습니다.** fallback 은 primary 의 `effort` 를 물려받는데, reasoning 을 지원하지
  않는 모델은 `reasoning_effort` 를 400 으로 거절합니다 — 그 필드 때문에 실패하는 fallback 은 선언의
  목적을 무너뜨립니다. `effort` 가 있는 선언으로 어댑터가 만들어질 때 WARN
  (`effort is not forwarded to this provider`) 을 한 번 남겨, 선언이 조용히 사라지지 않게 합니다.
- **도구 이름은 선언한 그대로 보냅니다.** OpenAI 는 함수 이름을 `^[a-zA-Z0-9_-]{1,64}$` 로 제한합니다.
  스킬 이름과 `mcp__{server}__` 접두는 들어맞지만, 원격 MCP 도구의 이름이 길거나 다른 문자를 담으면
  provider 가 400 으로 답하고 태스크는 설정 오류로 실패합니다. 어댑터는 이름을 줄이거나 바꾸지
  않습니다 — 모델이 부르는 이름이 도구 registry 의 이름과 같아야 합니다. 배포 검증은 아직 이것을 보지
  않습니다.
- **에러**는 Anthropic 어댑터와 같습니다: 429 → `LLM_001`, context 초과 400 → `LLM_002`, 5xx →
  `LLM_003`, 연결 실패 → `NET_001`, 타임아웃 → `NET_002`, 해석 불가 응답 → `LLM_004`. 그 밖의 4xx
  (자격 오류, 모르는 모델, 잘못된 인자)는 설정 오류라 재시도·전환하지 않습니다. context 초과는
  `error.code` 또는 문구로 알아봅니다. 다르게 표현하는 서버의 초과는 설정 오류로 취급되고, 프롬프트를
  조용히 자르는 서버는 알아챌 수 없습니다.
- **호환 서버마다 다른 곳은 너그럽게**: 도구 호출의 `arguments` 가 비어 있으면 인자 없는 호출로 봅니다.
  토큰 집계 때문에 태스크가 실패하지는 않습니다: `usage` 가 없거나, 비었거나, 필드가 빠졌거나 null ·
  정수가 아님 · 음수이면 그 필드를 0 으로 세고 WARN
  (`provider response carries no readable token usage, counting zero`) 을 남깁니다.
- 오류로 표시된 도구 결과는 앞줄에 `[tool error]` 를 붙여 보냅니다 — Chat Completions 의 tool 메시지에는
  오류 표시가 없습니다.

알려진 한계:

- **`max_tokens` 는 그 이름 그대로, 선언했을 때만 보냅니다.** 일부 모델 — 특히 reasoning 계열 — 은
  `max_tokens` 를 거절하고 `max_completion_tokens` 를 요구할 수 있습니다 (실 API 로 확인하지는
  않았습니다). 이 이름을 받지 않는 모델에는 `max_tokens` 를 선언하지 않습니다 — 그러면 서버 기본값이
  적용됩니다. fallback 은 primary 의 `max_tokens` 를 물려받습니다.
- **경로는 `v1/chat/completions` 로 고정입니다.** 다른 버전 접두(`/v1beta/openai` 등)로 API 를 내는
  게이트웨이는 지원하지 않습니다. `/v1` **앞의** 접두(`https://gateway.example.com/api`)는 됩니다.
- **응답 `content` 는 텍스트이거나 null 이어야 합니다.** `content` 가 파트 배열인 응답은 `LLM_004`
  입니다.
- **모델 호출에 필요한 헤더만 provider 에 닿습니다** — `content-type`, `accept`, `accept-encoding`,
  `user-agent` (`anthropic` 은 `anthropic-version` / `anthropic-beta` 추가). 에이전트 안에서 넣은
  조직 · 프로젝트 · 다른 키 헤더는 프록시가 떼어 내고, 쿠키는 어느 방향으로도 프록시를 건너지 않습니다.

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
  (`LLM_001` 또는 `LLM_003`)로, retryable 하게 실패합니다. 상태는 `aaestro_circuit_state{target="model:<provider>/<name>"}`
  로 노출됩니다.
- fallback 은 `max_tokens`·`effort` 를 primary 에서 물려받습니다.
- **관측**: `TaskResult.usage.model` / `.provider` 는 **마지막 턴에 응답한** 모델이고, 토큰
  합계는 턴을 가로질러 합산됩니다. 시도별 정확한 값은 `aaestro_model_requests_total` /
  `aaestro_model_tokens_total` 라벨에 있습니다.
  `aaestro_model_requests_total` 에 `fallback_index` 라벨이 붙습니다 (0 = primary). 전환할 때마다
  `model`, `provider`, `fallback_index`, `error_code`, `next_model`, `next_provider` 필드의 WARN
  로그를 남기고, open circuit 을 건너뛸 때도 `opened_by` 필드의 WARN 을 남깁니다.
- 어댑터가 없는 provider 는 체인을 만들 때 `CFG_001` 로 거절됩니다. 같은 provider 의 키 교체는 #360
  입니다.

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

`aaestro validate` 와 `aaestro deploy` 출력의 `models` 항목이 노드별로 해석된 모델 계층을
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
