# Control Plane API

한국어 | **[English](../en/api.md)**

Control Plane 은 에이전트 컨테이너 **바깥의 모든 것**을 소유하는 단일 프로세스다 — 선언
카탈로그, 그것을 쓰는 저작 표면, 그래프를 실제 컨테이너로 만드는 배포, 그 컨테이너를
구동하는 run. [Web UI](ui.md) 는 이 API 의 클라이언트일 뿐이며, 모든 화면은 아래 호출 중
하나다.

```bash
python -m aaestro.orchestrator
```

`configs/{AAESTRO_ENV}.yaml` 을 읽는다. 어떤 표면이 열리는지는 그 파일이 정한다:

| 표면 | 열리는 조건 |
|---|---|
| Run 조회·drain | 항상 |
| 카탈로그·저작 | 항상 — `registry.roots` 는 `AAESTRO_REPO_ROOT` 기준으로 해석된다 |
| 배포·run 제출·재개·steer | `orchestrator.deployment_store` 가 설정된 경우 |
| 빌드 재료 | `orchestrator.material_store` 가 설정된 경우 — 아니면 라우트가 `400` (`CFG_001`) 로 답한다 |
| 이미지 빌드, 커스텀 에이전트의 배포 게이트 | `orchestrator.material_store` **와** `orchestrator.build_store` 가 모두 설정된 경우 — 아니면 이미지 라우트가 없다 (`404`) |
| 권한 레지스트리 | `orchestrator.access_store` 가 설정된 경우 — 아니면 라우트가 없다 (`404`) |
| `/ui` 의 Web UI | 항상 |

`orchestrator.deployment_store` 가 없으면 컨테이너를 띄우지 않고
`POST /v1/runs/{id}/resume` 과 `POST /v1/runs/{id}/steer` 에 `501` 로 답한다 — run 기록을 읽기만 하고 구동하지는 않기
때문이다.

## 인증

**인증은 `orchestrator.control_token` 을 설정했을 때만 켜진다.** 토큰이 있으면 모든
`/v1/*` 라우트가 그것을 bearer 토큰으로 요구한다:

```bash
curl -H "Authorization: Bearer $AAESTRO_CONTROL_TOKEN" http://127.0.0.1:8700/v1/graphs
```

**토큰이 없으면 검사가 꺼지고 모든 라우트가 열린다.** 이 구성은 loopback 바인드에서만
허용된다 — 다른 주소에 토큰 없이 바인드하면 기동을 거부하고 (`CFG_001`), 토큰 없이 뜰
때는 경고를 남긴다. 운영자 한 명의 기계가 아니라면 토큰을 설정한다.

토큰을 설정해도 무인증인 라우트가 둘 있다: `GET /v1/health` (Docker healthcheck 가 직접
부른다) 와 `/ui` 의 정적 파일 (화면 자체는 데이터를 갖고 있지 않다).

토큰이 없거나 틀리면 `401` 이며, 이 응답은 아래의 에러 봉투가 **아니다**. 검사가 라우트보다
먼저 도는 의존성이라 FastAPI 자체의 `{"detail": "invalid control plane token"}` 과
`WWW-Authenticate` 헤더로 나간다.

## 에러

control plane 이 내는 실패는 같은 봉투를 쓰고, 안정적인 부분은 `code` 다 — 메시지가 아니라
코드로 분기한다. 두 응답만 이 봉투가 **아니다**: 위의 `401`, 그리고 run 라우트의 `501`
(run 을 구동하지 않는 control plane, 또는 steer 할 수 없는 에이전트 실행기 — 더 납작한 레거시 모양).

```json
{
  "error": {
    "category": "not_found",
    "code": "NF_001",
    "message": "unknown graph: no-such-graph",
    "agent": null,
    "task_id": null,
    "retryable": false,
    "details": {"kind": "graph", "name": "no-such-graph"}
  }
}
```

상태 코드는 한 가지 기준을 따른다 — **누가 고칠 수 있는가**:

| 상태 | 의미 | 대표 코드 |
|---|---|---|
| `400` | 요청 형식이 틀렸거나 선언이 유효하지 않다 | `VAL_001`, `VAL_002`, `MOD_002`, `CFG_002` |
| `401` | 토큰 누락/불일치 | — |
| `404` | 그런 리소스가 없다 | `NF_001` |
| `409` | 현재 상태와 충돌하며, 호출자가 해소할 수 있다 | `GRAPH_006`, `RT_010`, `RT_011`, `RT_012` |
| `500` | 서버가 고쳐야 한다 | `RT_001`, `GRAPH_002`, `STOR_003` |
| `503` | 지금은 안 되지만 다시 걸어볼 만하다 | `retryable: true` 인 모든 것 |

`details` 에는 조치에 필요한 값이 담긴다 — 어느 필드가 틀렸는지, 어떤 버전을 기대했는지,
어떤 에이전트가 이미 배포돼 있는지.

## Health 와 UI

### `GET /v1/health`

무인증 liveness. `{"status": "ok"}`.

### `GET /` → `GET /ui/`

`/` 는 운영자 UI 로 리다이렉트한다. [UI 가이드](ui.md) 참조.

## 카탈로그

디스크에 있는 선언의 읽기 전용 뷰. 모든 목록은 배열 두 개를 돌려준다 — 파싱된 것은
`items`, 실패한 것은 `problems`. **하나가 깨져도 나머지를 가리지 않는다** — 그러지 않으면
매니페스트 하나가 깨졌을 때 카탈로그 전체가 `500` 이 되고, 어느 파일인지 알 수 없다.

problem 하나는 `{path, code, message, errors, reason}` 이다. `path` 는 파일(또는 모듈 버전
디렉토리), `errors` 는 스키마 위반이면 문제의 키를 `{field, problem}` 으로, `reason` 은 파일을
읽지 못한 위치를 말한다 — 중복 키(`duplicate key 'k' at line N`), 파싱 실패
(`ScannerError at line N, column M`), 텍스트가 아닌 파일(`not valid UTF-8`). 어느 쪽도 거절된
값이나 입력 조각을 싣지 않는다. 키 이름은 나간다 (64자로 자르고 인쇄할 수 없는 문자는 바꾼다).
이름 불일치는 기대 이름(`located`)만 싣고 선언에 적힌 이름은 싣지 않는다.

### `GET /v1/agents`, `GET /v1/graphs`, `GET /v1/groups`

```json
{
  "items": [
    {
      "name": "planner",
      "version": "0.4.0",
      "group": "research",
      "description": "질의를 실행 가능한 리서치 계획으로 나누는 에이전트",
      "model": {"provider": "anthropic", "name": "claude-sonnet-5"}
    }
  ],
  "problems": []
}
```

그래프 요약은 `model` 대신 `mode`, `goal`, `nodes` 를 싣고, 그룹 요약은 `quotas` 를 싣는다.
`problem` 은 `path` 와 `code`, 메시지를 담는다. 선언(에이전트·그래프·그룹)은 두 종류의
실패 — 스키마 위반과 이름·위치 불일치 — 를 모두 `VAL_002` 로 보고한다. 모듈 목록은
레지스트리가 낸 코드를 그대로 싣는다 (`MOD_001`, `MOD_003`).

### `GET /v1/agents/{name}`, `GET /v1/graphs/{name}`, `GET /v1/groups/{name}`

파싱된 선언 전문 — 파일 텍스트가 아니라 런타임이 실제로 사용할 문서다. 이름이 없으면
`404` (`NF_001`).

### `GET /v1/modules/{module_type}`

`module_type` 은 `skillsets`, `promptsets`, `memorysets`, `skillpacks` 중 하나. 게시된 모든
버전을 나열한다:

```json
{"items": [{"name": "planner", "versions": ["0.1.0", "0.2.0", "0.3.0"]}], "problems": [], "warnings": []}
```

`problems` 는 게시되지 않은 버전이다 (각각 `path`, `code`, `message`). `warnings` 는 게시**된**
버전에 대한 권고 문자열이다 — 지금은 스킬팩만 낸다 (500줄을 넘는 `SKILL.md`, 표준 밖
frontmatter 키, `skills/` 아래의 낱개 파일). 스킬팩은
여기서 전부 검증되므로, 선언은 맞고 담긴 skill 이 표준을 어긴 팩은 `problems` 에 나온다
([modules.md](modules.md#스킬팩)).

### `GET /v1/modules/{module_type}/{name}/{version}`

모듈 문서 하나. 버전은 정확히 지정한다 — `latest` 는 없다.

스킬팩은 목록과 같은 검증을 지난다. 목록의 `problems` 에 나온 팩은 여기서도 `200` 이 아니라
같은 `MOD_003` 에러 (`400`) 로 답한다. 스킬팩 선언에는 skill 목록이 없으므로 (디렉토리가 곧
목록이다) 응답은 검증한 팩에서 목록을 더한다:

```json
{"apiVersion": "aaestro/v1", "kind": "Skillpack",
 "metadata": {"name": "docs", "version": "1.0.0"},
 "skills": [{"name": "changelog-entry", "files": 3}, {"name": "doc-review", "files": 3}]}
```

`skills` 에는 이름과 파일 수만 싣는다. `description` 같은 frontmatter 값은 skill 에 딸려 온
내용이라 여기서 돌려주지 않는다.

## 저작

검증을 통과한 선언을 디스크에 쓴다. 위치가 곧 식별자다: `docs-demo` 라는 그래프는
`graphs/docs-demo.yaml` 에 있으므로 경로 세그먼트와 선언된 이름이 일치해야 한다 (아니면
`VAL_002`).

이름이 곧 파일 경로가 되므로, 에이전트·그래프·그룹 이름을 받는 모든 라우트가 먼저 이름을
검사한다: 소문자·숫자·단일 하이픈만 허용한다. `..`, 슬래시, 대문자 같은 것은 `400`
(`VAL_002`) 이고 디스크에서 아무것도 읽거나 쓰거나 지우지 않는다. 조회, 재료, 이미지
라우트도 같다.

### `POST /v1/validate`

**아무것도 쓰지 않고** 초안을 검증한다. 편집기가 변경마다 부르는 것이 이것이다.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graphs": [ ... ], "agents": [ ... ]}' http://127.0.0.1:8700/v1/validate
```

```json
{"ok": false, "findings": [
  {"check": "mode_rules", "code": "GRAPH_001",
   "message": "mission graph must be able to reach END", "graph": "docs-demo"}
]}
```

`condition` 없는 edge 가 둘인 노드도 finding 이다 (`mode_rules`, `GRAPH_001`, `node_id` 와
`targets` 포함). 옆에 조건 edge 가 없으면 두 대상이 병렬로 돌고, 반복 카운터 `_iterations` 를
포함한 공유 state 에 두 분기를 합치는 규칙이 없어 run 이 실패한다. 조건 edge 옆이라면 기본
경로로는 첫 번째만 쓰인다. 병렬 분기는 아직 지원하지 않는다.

finding 은 에러 상태가 아니라 `200` 이다 — "아직 유효한가?" 에 대한 정상적인 답이기
때문이다. 초안 형식 자체가 아니면 `400` (`VAL_002`).

### `PUT /v1/graphs/{name}`, `PUT /v1/agents/{name}`

선언 하나를 검증 후 저장한다. `{"name": ..., "path": ...}` 를 돌려준다.

편집기가 기대는 두 가지 규칙:

- **내용을 바꾸면 버전을 올려야 한다.** 같은 버전으로 다른 내용을 저장하면 `400`
  (`MOD_002`, `details` 에 `existing` 과 `proposed`). 같은 내용이면 멱등이다.
- **사용 중인 선언은 덮어쓰지 못한다.** 실행 중 run 이나 살아 있는 배포가 참조하면 거절된다
  (`VAL_002`, "agent is currently deployed — tear the deployment down first"). 배포를 먼저
  해체한다.

쓰기는 원자적이다 — 같은 디렉토리의 임시 파일에 쓴 뒤 rename 하므로, 쓰는 도중 죽어도
잘린 선언이 남지 않는다.

### `PUT /v1/declarations`

여러 선언을 **한 단위로** 저장한다 — 함께 검증하고, 하나라도 실패하면 함께 되돌린다.
노드와 그 노드가 가리키는 에이전트를 같이 추가할 때처럼 변경이 파일을 넘나들 때 쓴다:

```json
{"graphs": {"docs-bundle": { ... }}, "agents": {"echo": { ... }}}
```

```json
{"written": ["/repo/graphs/docs-bundle.yaml", "/repo/agents/echo/manifest.yaml"]}
```

### `DELETE /v1/graphs/{name}`, `DELETE /v1/agents/{name}`

성공 시 `204` — 에이전트 디렉토리에 사람이 둔 파일이 남으면 그 이름들과 함께 `200` 이다 (아래).
참조가 남아 있으면 거절하며 (`400`, `VAL_002`), 사유에 따라 `details` 가 다르다 — 저장된 그래프가 쓰는 에이전트는 그 목록을 `referenced_by` 에 싣고, 현재 배포 중인
것은 `kind` 와 `name` 을 싣는다.

**에이전트 디렉토리는 매니페스트와 함께 사라진다 — 누군가 다른 것을 두지 않았다면.** 프레임워크가
`agents/<name>/` 에 쓰는 파일은 매니페스트뿐이므로, 그것이 사라지면 디렉토리도 지운다. `204` 가
말 그대로 삭제다. 디렉토리에 파일이 남아 있으면 — 손으로 둔 것이다 — 지우지 않고 그 목록을 `200`
으로 답한다:

```json
{"retained": ["Dockerfile", "src/agent.py"]}
```

`agents/` 아래에서 지우는 것은 매니페스트와 비게 된 디렉토리뿐이다. 남은 파일은 직접 지운다 —
두면 같은 이름을 다시 선언할 때 되살아난다.

**빌드 재료는 건드리지 않는다.** 재료는 재료 스토어에 버전으로 묶여 산다: 같은 이름과 버전을 다시
선언하면 그 재료가 돌아오고, 그 버전에 다른 재료를 넣는 것은 여전히 거절된다 (`MOD_002`). 새로
시작하려면 버전을 올리거나, 에이전트를 지우기 전에 `DELETE /v1/agents/{name}/materials` 로 비운다.

## 빌드 재료와 이미지

커스텀 에이전트는 **빌드 재료**를 가진 에이전트다: `Dockerfile`(선택)과 `src/` 트리. 재료는
저장소가 아니라 control plane 의 재료 스토어에 에이전트 이름과 선언된 버전을 키로 산다.
declarative 에이전트는 재료가 없고 굽지도 않는다 — base 이미지에 선언을 마운트해 돈다.

굽기는 명시적 단계다. 재료를 저장해도 굽지 않고, 배포도 굽지 않는다 — 그 버전의 이미지가
`built` 가 아니거나 `built` 기록의 태그가 배포가 띄울 태그와 다른 커스텀 에이전트는 배포가
**거절한다** (`409`, `RT_012`) — [`POST /v1/deployments`](#post-v1deployments) 참조.

### `GET /v1/agents/{name}/materials`

에이전트 **현재** 버전의 재료. 없는 것은 오류가 아니다:

```json
{"agent": "docs-custom", "version": "0.1.0", "files": {}, "updated_at": ""}
```

에이전트가 선언되지 않았으면 `404` (`NF_001`).

### `PUT /v1/agents/{name}/materials`

에이전트 현재 버전의 재료를 저장한다. 본문은 컨텍스트 상대 경로 → 텍스트 내용이다:

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"files": {"src/marker.py": "MARK = 1\n"}}' \
  http://127.0.0.1:8700/v1/agents/docs-custom/materials
```

응답은 `GET` 과 같은 모양이고 `updated_at` 이 찍힌다. 규칙:

- **경로**는 `Dockerfile` 이거나 `src/` 아래, 상대·posix 형식, 이미 정규화된 형태여야 한다
  (`src/./a.py`, `src//a.py`, `..` 가 들어간 것은 거절). 파일은 최대 200개, 각각 텍스트
  256 KiB 까지. 위반은 `400` (`VAL_002`) 이고 `details` 에 문제의 `path` 가 실린다.
- **한 버전의 재료는 불변이다.** 같은 버전에 다른 내용은 `400` (`MOD_002`), 같은 내용은
  멱등이다. 에이전트 버전을 올려 에이전트를 저장한 뒤 새 재료를 저장한다.
- **배포 중인 에이전트의 재료는 바꿀 수 없다** (`400`, `VAL_002`).

`Dockerfile` 은 저장할 때가 아니라 구울 때 검사한다.

### `DELETE /v1/agents/{name}/materials`

`204`. 현재 버전의 재료를 비워 에이전트가 다시 declarative 에이전트로 돈다. 버전은 계속
점유된다: 그 뒤에 그 버전으로 다른 재료를 저장해도 `MOD_002` 다. 배포 중이면 거절한다.

### `POST /v1/agents/{name}/image`

빌드를 제출하고 곧바로 `202` 로 돌아온다:

```json
{"agent": "docs-custom", "version": "0.1.0", "status": "building",
 "image": "aaestro/agent-docs-custom:0.1.0", "error": null, "log": "",
 "updated_at": "2026-09-14T11:02:03.123456+00:00"}
```

빌더는 카탈로그와 스토어에서 임시 빌드 컨텍스트를 조립해 `aaestro/agent-<name>:<version>`
으로 굽고, 그 디렉토리를 지운다:

```
Dockerfile        # 사용자의 것, 저장하지 않았으면 스켈레톤
manifest.yaml     # 에이전트의 선언
modules/          # 모듈 루트
src/              # 재료
```

스켈레톤은 이 셋을 `/app` 에 복사하고 `/app/src` 를 `PYTHONPATH` 에 올린다. 그래서
매니페스트의 `spec.entrypoint: agent:MyAgent` 는 `src/agent.py` 로 해석된다. 직접 쓴
`Dockerfile` 은 다음을 지켜야 한다:

- `FROM aaestro/agent-base:<tag>` 로 시작한다 — base 에 `agentd` 가 들어 있다
- `root` 로 끝나지 않는다 (설치하려고 올라갔다 내려오는 것은 괜찮다)
- `COPY`/`ADD` 는 컨텍스트 안에서만 — 절대 경로, `..`, 원격 URL 금지

굽기 전에 거절되는 경우:

- `404` (`NF_001`) — 에이전트가 선언되지 않았다.
- `400` (`VAL_002`) — 재료가 없거나 `Dockerfile` 이 위 규칙을 어겼다.
- `409` (`RT_011`) — 이 버전의 빌드가 이미 진행 중이다. 두 빌드가 같은 태그를 쓰면 늦게
  끝난 쪽이 이긴다.

시작한 뒤 실패한 빌드는 HTTP 오류가 **아니다**. 아래 기록에 `failed` 로 남는다. `error` 는
어느 이미지가 실패했는지만 말하고, 원인은 Docker 출력의 꼬리인 `log` 에 있다:

```
Step 2/2 : RUN exit 3
 ---> Running in 80c5a8ac55e7
The command '/bin/sh -c exit 3' returned a non-zero code: 3
```

### `GET /v1/agents/{name}/image`

에이전트 현재 버전의 빌드 기록과, 빌드가 필요한지:

```json
{"agent": "docs-custom", "version": "0.1.2", "status": "failed",
 "image": "aaestro/agent-docs-custom:0.1.2", "expected_image": "aaestro/agent-docs-custom:0.1.2",
 "needs_build": true, "error": "image build failed: aaestro/agent-docs-custom:0.1.2",
 "log": "Step 1/2 : FROM aaestro/agent-base:0.1.0\n ... returned a non-zero code: 3",
 "updated_at": "..."}
```

| `status` | 의미 |
|---|---|
| `null` | 구운 적이 없다 |
| `building` | 굽는 중 — 다시 조회한다 |
| `built` | 이미지가 있다 — 배포가 쓸 수 있다 |
| `failed` | 마지막 빌드가 실패했다 — `error` 와 `log` 를 읽는다 |

`image` 는 기록이 구워진 태그, `expected_image` 는 배포가 띄울 태그다. 둘이 다른 `built`
기록은 배포가 거절한다. 재료가 없는 에이전트는 `needs_build` 가 `false` 다. `log` 는 마지막 8000자를 보관한다. 기록은
control plane 재시작을 넘는다.

## 배포

배포는 그래프 하나를 실행 중인 컨테이너 집합으로 만든다. base 이미지는 선언을 갖고 있지
않으므로, runtime 이 매니페스트와 모듈 루트를 읽기 전용으로 마운트하고 그래프가 선언한
A2A 배선을 주입한다.

### `POST /v1/deployments`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graph": "research-pipeline"}' http://127.0.0.1:8700/v1/deployments
```

선택으로 `model_overrides`(노드 id → 부분 `spec.model`)를 넘기면 선언 파일을 바꾸지 않고 모델을
교체합니다. [에이전트 — 모델 회복력](modules.md#에이전트--모델-회복력) 참조. agentd 는 그 모델로 태스크를
실행합니다:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"graph": "research-pipeline", "model_overrides": {"researcher": {"name": "claude-haiku-4-5", "fallbacks": []}}}' \
  http://127.0.0.1:8700/v1/deployments
```

응답은 보낸 map 을 적은 키만 그대로 돌려준다 — 여기서는 `"model_overrides": {"researcher": {"name": "claude-haiku-4-5", "fallbacks": []}}` (첫 예시는 `{}`). fallback 을 primary 로 올릴 때는 `fallbacks` 도 함께 조정해야 합니다 —
primary 와 같은 fallback 은 거절됩니다.

모든 에이전트가 healthy 를 보고하면 `201`:

```json
{
  "deployment_id": "dep-2fc94a0b5f24",
  "graph": "research-pipeline",
  "version": "1.0.0",
  "status": "ready",
  "error": null,
  "updated_at": "2026-09-13T11:29:06.391594+00:00",
  "model_overrides": {},
  "agents": [
    {"name": "planner", "replica": 0, "container_id": "0f2edbe1aa5a",
     "image": "aaestro/agent-base:0.1.0", "control_port": 32852, "a2a_port": 9100}
  ]
}
```

이 호출은 에이전트가 healthy 가 되거나 기한이 지날 때까지 기다린다 — 레퍼런스 그래프에서는
수 초다. 에이전트별 토큰은 응답에 실리지 않는다.

`status` 는 다음 중 하나다:

| 상태 | 의미 |
|---|---|
| `starting` | 컨테이너가 올라오는 중 |
| `ready` | 모든 에이전트가 health 를 통과해 태스크를 받는다 |
| `failed` | 어느 단계가 실패했고, **이 배포가 띄운 것은 전부 다시 내려갔다** |
| `stopped` | `DELETE` 로 해체됨 |
| `lost` | 재시작 후 컨테이너 중 하나 이상이 사라져 있었다 |

실패해도 유령 컨테이너는 남지 않는다. 어떤 종류의 실패인지는 응답이 알려준다:

- `404` (`NF_001`) — 그런 그래프가 없다.
- `400` (`VAL_001`) — 그래프가 검증을 통과하지 못했다. 아무것도 기동하지 않았다. `model_providers`
  검사도 여기에 해당한다: 모르는 provider, `runtime.egress_proxy.providers` 에 없는 provider,
  manifest 의 provider 집합을 벗어나는 override — 사유는 `details.findings` 에 실린다.
- `400` (`VAL_002`) — 빌드 재료가 있는 에이전트가 빌드 태그(`aaestro/agent-<name>:<version>`)
  와 다른 `runtime.image` 를 선언했다. 두 곳이 다른 이미지를 가리키면 무엇이 도는지 알 수 없다.
- `400` (`VAL_002`) — `model_overrides` 가 그래프에 없는 노드를 가리키거나 override 본문이 잘못됐다
  (provider 만 적음, 모르는 키). 아무것도 기동하지 않았다.
- `409` (`RT_012`) — 빌드 재료가 있는 에이전트에 그 버전의 `built` 이미지가 없다: 굽힌 적이
  없거나, 마지막 빌드가 실패했거나, 아직 굽는 중이거나, 기록의 태그가 배포가 띄울 태그와 다르다.
  `details` 에 `image` (기대 태그), `build_status`, `build_error` 가 실리고, 태그가 다르면
  `recorded_image` 도 실린다. 구운 뒤 다시 배포한다 — 배포는 대신 굽지 않는다. 이미지 태그
  규칙이 바뀐 뒤에는 이전에 구운 커스텀 에이전트를 다시 구워야 한다. 아무것도 기동하지
  않았고 배포 기록도 남지 않는다. 재료가 없는 에이전트는 base 이미지로 돌며 게이트를 거치지
  않는다.
- `409` (`RT_010`) — 이 그래프의 에이전트가 다른 배포로 이미 돌고 있다. 그쪽을 먼저
  해체한다 — 그래프와 그 에이전트는 한 단위로 배포되므로 두 배포가 에이전트를 공유할 수
  없다.
- `503` (`RT_002`) — 기한 안에 healthy 가 되지 못했다. retryable 로 표시된다: 컨테이너는
  되감겼으므로 다시 시도해도 안전하다.

### `GET /v1/deployments`, `GET /v1/deployments/{id}`

목록은 `{"items": [...]}`, 단건 조회는 기록 자체다. 각 기록은 배포 생성 때 받은
`model_overrides` 를 담는다 (없으면 `{}`). 둘 다 **지금 실제로 서 있는**
컨테이너를 반영한다 — 감시가 죽은 컨테이너를 다시 세웠다면 기록은 새 id 와 포트를 보여준다.

### `DELETE /v1/deployments/{id}`

에이전트마다 drain 을 청해 진행 중 태스크를 기다린 뒤 컨테이너를 정지·제거한다.
`status: "stopped"` 인 기록을 돌려주며, 이력을 읽을 수 있도록 기록 자체는 남긴다. 이미
`stopped` 나 `failed` 인 배포는 그대로 돌려준다.

## Run

Run 은 배포된 그래프를 구동한다. 배포가 에이전트의 위치를 알고 있으므로, run 제출에는
주소가 아니라 `deployment_id` 를 준다.

### `POST /v1/runs`

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"deployment_id": "dep-2fc94a0b5f24", "input": {"query": "..."}}' \
  http://127.0.0.1:8700/v1/runs
```

즉시 `202` — run 은 수 분이 걸릴 수 있고, 그동안 HTTP 요청을 붙잡고 있어 봐야 누구에게도
도움이 되지 않는다:

```json
{"run_id": "run-a2bce8e90f40", "graph": "research-pipeline", "mode": "mission",
 "status": "running", "iteration": 0, "failure_streak": 0,
 "drain_requested": false, "updated_at": "..."}
```

`mode` 는 호출자가 아니라 그래프가 정한다. 선택 필드: 아이디를 지정하는 `run_id`, 그래프의
모드를 단언하는 `mode` (불일치는 `400`).

거절: 배포가 없거나 `ready` 가 아니면 `404` (`NF_001`). 디스크의 그래프가 배포된 것과 다른
버전으로 편집됐으면 `400` (`VAL_002`) — 먼저 재배포한다. 그러지 않으면 다른 선언으로 만든
컨테이너를 구동하게 된다.

### `GET /v1/runs`, `GET /v1/runs/{run_id}`

목록은 `?mode=mission|service` 를 받는다. 단건 조회는 **이 프로세스에서** 완주한 run 에
한해 두 필드를 더 싣는다 — `state` (최종 그래프 state) 와 `error`. Control plane 이
재시작하면 기록은 남지만 그 최종 state 는 남지 않는다. 지속되는 상태는 checkpointer 에 있다.

### `POST /v1/runs/{run_id}/drain`

현재 iteration 을 마친 뒤 정지하도록 요청하고 `drain_requested: true` 와 함께 즉시
돌아온다. run 은 스스로 `stopped` 에 도달하므로 `GET` 으로 확인한다. service run 을
멈추는 방법은 drain 이다 — 강제 종료는 없다.

### `POST /v1/runs/{run_id}/resume`

멈춘 자리에서 이어간다. 그 의미는 모드마다 다르다:

- **service run** 은 마지막 iteration 부터, 그리고 `halted` 에서만 이어간다 — service
  그래프가 연속 실패 임계를 넘어 도달하는 상태다 (`GRAPH_005`). 그 외의 상태는 `409`
  (`GRAPH_006`): 의도적으로 drain 한 run 은 재개가 아니라 새로 제출하는 것이 맞다.
- **mission run** 은 마지막 checkpoint 에서 이어가며 **상태 가드가 없다** — 무엇을 이어갈지는
  checkpointer 가 정한다. 이미 완주한 run 을 재개하면 그 checkpoint 부터 다시 구동된다.
  영속 checkpointer 가 없으면 이어갈 지점 자체가 없다 (`STOR_002`).

`501` 은 이 control plane 에 배포 표면이 없다는 뜻이다 — run 을 읽기만 하고 구동하지 않는데
`200` 을 주면 운영자가 재개됐다고 믿는다.

### `POST /v1/runs/{run_id}/steer`

run 이 실행 중인 태스크의 방향을 **지금까지의 작업을 버리지 않고** 바꾼다. 취소 후 재제출은
처음부터 다시 돈다 — steer 는 에이전트가 이미 진행 중인 대화에 지시 하나를 더한다.

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"text": "2023 이 아니라 2024 수치를 쓴다.", "by": "alice"}' \
  http://127.0.0.1:8700/v1/runs/run-a2bce8e90f40/steer
```

| 필드 | |
|---|---|
| `text` | 지시. 필수, 최대 4000자. |
| `by` | 누가 냈는지 — 감사용. 기본값 `operator`, 출력 가능한 문자 최대 200자. **호출자의 주장**이지 인증된 신원이 아니다 — control plane 토큰은 사람을 가르지 않는다. |
| `node` | steer 할 노드. 생략하면 지금 실행 중인 노드. |

`202` — 지시는 **접수된 것이지 반영된 것이 아니다**:

```json
{"run_id": "run-a2bce8e90f40", "node": "researcher", "task_id": "…",
 "agent": "researcher", "status": "queued"}
```

**언제 반영되나.** 에이전트는 다음 *턴 경계* — 현재 턴의 tool 결과 바로 뒤 — 에 지시를 user
턴으로 넣는다 (`assistant(tool 호출) → tool 결과 → user(steer)`). 진행 중인 모델 호출에는
끼어들지 않고, 이미 보낸 메시지를 고쳐 쓰지 않으며, system 프롬프트를 건드리지 않는다 (건드리면
그 태스크의 프롬프트 캐시 prefix 가 깨진다). 같은 경계에 쌓인 여러 지시는 user 턴 하나가 된다.
모델이 태스크 입력과 구분할 수 있게 각 지시 앞에 `[operator steer]` 가 붙는다. tool 결과가 이미
`user` 역할로 가는 provider 에서는 지시를 두 번째 `user` 메시지로 보내지 않고 그 메시지 뒤에 붙인다.

**반영되지 않을 수도 있다.** 다음 턴 경계가 오기 전에 태스크가 끝나면 — 모델이 이미 최종 답을
쓰고 있었거나, tool 이 실패했거나, turn 상한에 닿았거나, 태스크가 취소됐으면 — 지시는 버려진다.
`queued` 는 약속이 아니다. 에이전트는 `WARN` 을 남기고 (`steer dropped, task ended before the
next turn boundary` — `agent`, `task_id`, `run_id`, 누가 냈는지) 그 밖의 일은 일어나지 않는다.
run 의 결과를 확인하고, 방향이 중요했다면 다시 제출한다. 스트림 소비자는 지시가 대화에 들어간
순간 `steer` 이벤트를 보고, 버려졌으면 보지 못한다. 이벤트는 지시가 대화에 붙었다는 것까지만
말한다 — 모델은 다음 호출에서 읽고, 그 호출은 실패할 수 있다.

거절:

| 상태 | 코드 | 언제 |
|---|---|---|
| `404` | `NF_001` | 모르는 run 이거나, 진행 중인 태스크가 없거나 (끝났거나 두 노드 사이), 지목한 `node` 가 실행 중이 아니거나, 에이전트가 아직 루프에 들어가지 않았거나 (노드가 방금 배정됐다 — 다시 시도한다), 지시가 닿기 직전에 태스크가 끝났다. 어느 것인지는 `message` 가 말한다. |
| `400` | `VAL_002` | 본문이 형식에 맞지 않거나 모르는 키가 있다. `text` 가 비었거나 4000자를 넘는다. `by` 가 빈 문자열이거나 200자를 넘거나 제어문자·개행을 담았다. 그 태스크에 이미 8건이 대기 중이다. 여러 노드가 실행 중인데 `node` 를 생략했다 (details 의 `running` 에 목록). 그 노드에 진행 중인 태스크가 둘 이상이라 노드 이름으로 가를 수 없다 (`task_ids` 에 목록). |
| `501` | 레거시 모양 ([에러](#에러)) | 이 control plane 이 그 run 을 구동하지 않거나 (위 resume 참고), 에이전트의 실행기가 steer 를 지원하지 않는다 — 구현하지 않은 커스텀 `spec.entrypoint`. `200` 으로 답하는 일은 없다. |

steer 할 수 없는 것: 턴 경계는 LLM tool 루프에만 있고, 에이전트가 A2A 로 peer 에게 위임한
태스크는 여기서 지목할 수 없다 — 위임한 노드를 steer 한다. 거절된 지시도 control plane 이
누가 시도했는지와 함께 로그에 남긴다 (`run steer refused`). 지시 본문은 로그에 남기지 않는다
(길이만). 태스크 스트림의 `steer` 이벤트에는 실린다.

## 권한 레지스트리

에이전트가 도는 동안 바뀌는 권한은 **에이전트 컨테이너 밖에서, 요청마다** 판정한다 — 에이전트는
자기 컨테이너 안에서 모든 권한을 가질 수 있으므로, 거기서 하는 검사는 편의일 뿐 경계가 아니다.
레지스트리는 그 판정을 내리는 곳이고, 강제 지점(Memory Service, 이그레스 프록시, 피호출자의
A2A 서버)이 여기에 묻고 답을 캐시한다.

`orchestrator.access_store` 가 설정되면 열린다:

```yaml
orchestrator:
  access_store: ./var/access.db          # 신원·부여·회수 — 재시작을 넘어 남는다
  access_enforcer_token: ${ENFORCER}     # control_token 과 달라야 한다
  access_agent_url: http://control-plane:8700   # 에이전트 컨테이너에서 닿는 control plane 주소
  access_stewards: [permission-agent]    # 부여할 수 있는 에이전트는 이들뿐
```

`access_store` 를 켜고 `access_enforcer_token` 없이 loopback 이 아닌 주소에 바인드하면 기동을
거부한다 (`CFG_001`) — control plane 토큰을 요구하는 것과 같은 이유다. `access_agent_url` 없이
`access_store` 만 켜도 기동을 거부한다 (`CFG_001`): 에이전트는 A2A 호출에서 자기가 누구인지
증명할 때 이 주소가 필요하고, 없으면 그래프의 모든 에이전트가 쥐는 서명 키로 되돌아가야 한다.

**에이전트는 이 주소로 자격을 보낸다.** 평문 `http` 는 에이전트와 프레임워크 서비스만 쓰는 사설
네트워크용이다 — Agent Control API, Memory Service 와 같은 신뢰 경계다. 에이전트 트래픽이 호스트를
넘거나 통제하지 않는 네트워크를 지나면 control plane 앞에서 TLS 를 종단하고 `https` 를 쓴다. 주소는
`http(s)://host[:port]` 만 받는다 — 자격·경로·쿼리가 들어 있으면 거부한다 (`CFG_001`).

**강제는 지점별로 차례차례 들어온다.** 메모리, A2A 호출, 프록시를 거친 이그레스는 지금 강제된다 —
레지스트리 모드의 Memory Service, 피호출자의 A2A 서버, 이그레스 프록시가 요청마다 묻는다
([메모리 강제](#메모리-강제), [A2A 강제](#a2a-강제), [이그레스 강제](#이그레스-강제) 참조). 프록시를
켜면 에이전트는 외부 경로 없는 네트워크에 있으므로 나가는 길은 프록시뿐이고, 원격 MCP 도구는 도구 하나씩
판정한다 ([원격 MCP 도구](#원격-mcp-도구) 참조).

### 호출자 셋, 자격 셋

토큰 하나로 묶으면 강제 지점이 운영자 권한을, 권한 에이전트가 모든 판정 조회 권한을 덤으로
갖는다. 그래서 각자 자기 자격으로 인증한다:

| 호출자 | 자격 | 라우트 |
|---|---|---|
| 운영자 | control plane 토큰 | 기록 조회, 회수, 기록 종료 |
| 권한 에이전트 | **자기 에이전트 신원** | 부여 |
| 모든 에이전트 (A2A) | **자기 에이전트 신원** | 호출 표, 받은 표 확인, 변경 알림 |
| 강제 지점 | `access_enforcer_token` | 판정, 신원, 변경 알림 |

### 에이전트 신원

배포는 에이전트마다 신원을 하나씩 발급해 `AAESTRO_ACCESS_CREDENTIAL` 로 주입한다. 레지스트리는
SHA-256 해시만 저장하며, 값은 어떤 API 응답에도 실리지 않는다. 신원은 control plane 재시작을
넘어 유지되고, 감독이 컨테이너를 교체하면 다시 주입되며, 배포를 해체하거나 되감는 순간 통하지
않게 된다.

### 요청 판정 순서

위에서부터, 먼저 맞는 것이 이긴다:

1. 요청을 덮는 운영자 **회수**가 살아 있다 → `deny`
2. 에이전트 **선언**이 허용한다 → `allow`, `decided_by: "declaration"`
3. 요청을 덮는 **부여**가 살아 있다 → `allow`, `decided_by: <권한 에이전트>`
4. 그 밖 → `deny`, `decided_by: "default"`

따라서 회수는 선언과 부여를 모두 이긴다. 메모리에 `mode: "rw"` 로 회수하면 쓰기만 막고 읽기는
남는다.

2단계는 강제 지점이 갖춰진 자원 종류에만 적용된다 — 지금은 넷 모두: `memory`, `a2a`, `egress`,
`mcp_tool`. `a2a` 의 선언은 호출자가 **지금 배포된** 그래프의 `connections` 다. 저장소에 있어도 배포되지
않은 그래프는 아무것도 허용하지 않는다. `egress` 의 선언은 매니페스트의 `runtime.egress`, 모델 provider
호스트, external MCP 서버 호스트다. `mcp_tool`(`server/tool`) 의 선언은 매니페스트가 선언한 원격 서버의
모든 도구, `allowed_tools` 를 적었으면 그 목록이다. 종류별 선언 판정은 그것을
쓰는 강제 지점과 함께 연결되므로, 선언과 강제가 따로 놀 수 없다. 그 전까지는 해당 종류에서 선언이
아무것도 허용하지 않고, 부여가 없는 요청은 `decided_by: "default"` 로 `deny` 된다.

선언은 판정마다 읽으므로 바뀌면 재시작 없이 반영된다. 레지스트리는 에이전트 매니페스트·그룹
파일·그래프 파일(그래프의 `connections` 가 A2A 선언이라 연결 회수도 즉시 보인다)을 지켜보다가 바뀌면 버전을 올려 강제 지점이 캐시한 판정을 버리게 한다. 기동할 때도 한 번
올린다 — 멈춘 동안 무엇이 바뀌었는지 알 수 없기 때문이다.

### 확장 상한

권한 에이전트는 운영자가 그 에이전트의 그룹이나 `global` 에 선언한 **상한** 안에서만 권한을
넓힐 수 있다. 제한하는 것은 레지스트리다 — 권한 에이전트가 처리하는 요청은 untrusted input
이므로, 한계를 그 판단에 맡기지 않는다.

```yaml
# groups/research.yaml
spec:
  access:
    ceiling:
      max_ttl_s: 3600                                   # 모든 부여는 늦어도 이만큼 뒤에 만료
      memory:
        - {space: "group:research:knowledge", mode: ro}
      egress: [api.search.example.com]
      mcp_tool: []
      a2a: []
```

상한이 없는 그룹은 확장도 없다. 그룹 상한과 `global` 이 같은 요청을 함께 덮으면 더 작은
`max_ttl_s` 가 적용된다. 메모리 space 는 명시적인 영구 space id(`local|group|global:<소유자>:<별칭>`)
여야 하고, 어떤 대상도 비어 있거나 와일드카드를 담을 수 없다. 상한 변경은 선언 변경이며, 권한
에이전트는 자기 상한을 바꾸지 못한다.

### `POST /v1/access/grants` — 권한 에이전트

```bash
curl -X POST -H "Authorization: Bearer $AAESTRO_ACCESS_CREDENTIAL" \
  -H 'content-type: application/json' \
  -d '{"agent": "researcher", "kind": "egress", "target": "api.search.example.com",
       "ttl_s": 600, "reason": "fetch sources for run r-12", "requested_by": "researcher"}' \
  http://127.0.0.1:8700/v1/access/grants
```

`kind` 는 `memory`, `egress`, `mcp_tool`, `a2a` 중 하나다. `mode`(`ro`/`rw`)는 메모리에는 필수,
나머지에는 없어야 한다. 종류에 맞지 않는 mode 는 회수·판정을 포함한 모든 권한 라우트에서 `400`
(`VAL_002`) 이다. `201` 로 기록을 돌려준다:

```json
{
  "rule_id": "rule-7c1e0a9b2d3f4e5a", "agent": "researcher", "kind": "egress",
  "target": "api.search.example.com", "mode": null, "effect": "allow",
  "decided_by": "permission-agent", "requested_by": "researcher",
  "reason": "fetch sources for run r-12",
  "created_at": 1789381200.0, "expires_at": 1789381800.0, "lifted_at": null
}
```

거절은 `403` + `ACC_003` 이며 로그와 계측(`aaestro_access_grants_total{op="refuse"}`)에 남는다.
호출자가 `access_stewards` 에 없을 때, 자기 자신에게 부여할 때, 메모리 권한에 mode 를 빠뜨렸을
때, 상한이나 `max_ttl_s` 를 넘을 때, 운영자가 회수한 것을 요청할 때 거절한다 — 회수를 되돌리는
것은 운영자뿐이다. 모르거나 폐기된 신원은 `403` + `ACC_001`, 없는 에이전트는 `404` 다.

### 권한 에이전트

작업 에이전트는 부여 경로를 부르지 않는다. A2A 로 **권한 에이전트**에게 요청하고, 권한 에이전트가
자기 신원으로 그 경로를 부른다. 참조 구현은 `agents/permission-agent`
(`aaestro.access.steward:PermissionAgent`) 이고 `graphs/permissions.yaml` 로 배포한다. 규칙만으로
결정하며 매니페스트에 선언된 모델을 부르지 않는다 — 요청 문구로 무엇을 넘기도록 설득할 수 없다.
어느 쪽이든 상한은 레지스트리가 강제한다.

배선:

- `orchestrator.access_stewards` 에 올린다. 모든 에이전트는 목록의 권한 에이전트를 부를 수 있다:
  권한 에이전트는 모든 호출자의 선언에서 허용된 `a2a` 대상이고, 레지스트리가 설정되어 있으면
  runtime 이 떠 있는 권한 에이전트를 모든 에이전트의 peer 에 넣는다.
- **요청할 에이전트의 그래프보다 `permissions` 를 먼저 배포한다.** 에이전트는 기동할 때 peer 를
  받는다. 권한 에이전트가 뜨기 전에 기동한 에이전트는 재배포할 때까지 권한 에이전트에 닿는 길이
  없다.

태스크 입력은 요청 자체이거나 `{"request": "<같은 요청의 JSON 문자열>"}` 이다 — `ask_peer` 가
보내는 모양이다:

```json
{"kind": "memory", "target": "global:global:org", "mode": "rw", "ttl_s": 300,
 "reason": "record the findings of run r-12"}
```

모르는 필드는 거절하고, `agent` 필드는 없다: 부여는 언제나 피호출자 A2A 서버가 티켓으로 확인한
호출자에게 간다 — 다른 에이전트를 대신해 요청할 수 없다. 확인된 호출자가 없는 태스크(직접 요청,
그래프 노드)는 레지스트리에 묻지 않고 거절한다. 부여되면 이렇게 답한다:

```json
{"granted": true, "rule_id": "rule-7c1e0a9b2d3f4e5a", "kind": "memory",
 "target": "global:global:org", "mode": "rw", "expires_at": 1789381500.0}
```

실패는 호출자에게 `A2A_003` 으로 가고, 권한 에이전트의 에러가 `details.peer_error` 에 실린다:

| `peer_error.code` | 언제 | 재시도 |
|---|---|---|
| `ACC_003` | 레지스트리가 거절했다 — 레지스트리의 코드는 `details.registry_code` 에 | 아니오 |
| `VAL_002` | 확장 요청의 모양이 맞지 않는다 | 아니오 |
| `ACC_002` | 레지스트리에 닿지 않았다 | 예 |

같은 task id 를 다시 보내면 부여를 두 번 기록하지 않고 첫 답을 돌려준다. `ACC_002` 답은 기억하지
않으므로 재시도는 다시 묻는다. 권한 에이전트를 멈추면 새 부여만 멈춘다 — 이미 준 부여는 만료까지
유효하고, 운영자 회수도 그대로 동작한다.

**부여받은 메모리 space 부르기.** 선언하지 않은 space 에는 별칭이 없다. 대신 space id 로 부른다 —
Memory Service 요청에 `{"space": "global:global:org"}` — Memory Service 는 다른 space 와 똑같이
판정한다.

### `POST /v1/access/decisions` — 강제 지점

```json
{"credential": "<강제 지점에 제시된 신원>",
 "kind": "egress", "target": "api.search.example.com"}
```

```json
{"agent": "researcher", "decision": "allow", "decided_by": "permission-agent", "version": 42,
 "valid_until": 1789381800.0}
```

`valid_until` 은 이 판정의 근거가 된 기록 중 가장 이른 만료 시각이며, 없으면 `null` 이다. 만료는
레지스트리 버전을 올리지 않으므로, 강제 지점은 `valid_until` 이 지난 캐시 판정을 쓰면 안 된다 —
레지스트리에 닿지 않는 동안에도 자기 시계로 알 수 있다. 메모리 판정에는 `mode` 가 필요하다.

모르거나 폐기된 신원은 에러가 아니다: `200` 에
`{"agent": null, "decision": "deny", "decided_by": "unknown-identity"}` 로 답해, 강제 지점이 다른
답과 똑같이 거부를 캐시할 수 있게 한다.

### `POST /v1/access/identities` — 강제 지점

```json
{"credential": "<강제 지점에 제시된 신원>"}
```

```json
{"agent": "researcher", "version": 42}
```

판정을 묻기 전에 이름을 해석해야 하는 강제 지점이 먼저 누가 묻는지 알아낸다 — Memory Service 는
에이전트의 선언으로 별칭을 space id 로 바꾼다. 모르거나 폐기된 신원은 `200` 에 `"agent": null` 이며,
거부와 똑같이 캐시할 수 있다.

### `GET /v1/access/changes?after=<version>&wait_s=<seconds>` — 강제 지점 또는 에이전트

긴 폴링. 레지스트리 버전이 `after` 를 넘는 즉시, 또는 `wait_s`(최대 30)가 지나면
`{"version": N}` 으로 답한다. 부여·회수·기록 종료와 배포 해체(그 신원이 통하지 않게 된다)는 모두
버전을 올리며, 강제 지점은 새 버전을 보면 캐시를 버린다. bearer 는 `access_enforcer_token` 또는 살아
있는 에이전트 신원이다 — 피호출자가 캐시한 A2A 판정을 버리려고 따라오며, 알림에는 버전 번호뿐이다.

### `GET /v1/access/agents/{name}` — 운영자

운영자가 한 에이전트의 권한을 좁히는 데 필요한 것 전부를 한 응답으로 — [권한 탭](ui.md)이 보여 주는
것이 이것이다:

```json
{
  "agent": "researcher",
  "version": 42,
  "rules": [
    {"rule_id": "rule-7c1e0a9b2d3f4e5a", "agent": "researcher", "kind": "memory",
     "target": "local:researcher:longterm", "mode": "rw", "effect": "deny",
     "decided_by": "operator", "requested_by": "", "reason": "incident 311",
     "created_at": 1789381200.0, "expires_at": null, "lifted_at": null}
  ],
  "declared": [
    {"kind": "memory", "target": "local:researcher:longterm", "mode": "rw"},
    {"kind": "memory", "target": "global:global:org", "mode": "ro"},
    {"kind": "a2a", "target": "planner", "mode": null},
    {"kind": "egress", "target": "api.anthropic.com", "mode": null},
    {"kind": "mcp_tool", "target": "corp/*", "mode": null}
  ],
  "ceilings": [
    {"group": "research", "max_ttl_s": 3600, "memory": [], "egress": ["api.search.example.com"],
     "mcp_tool": [], "a2a": []}
  ],
  "denials": [
    {"kind": "memory", "target": "local:researcher:longterm", "mode": "rw",
     "decided_by": "operator", "at": 1789381260.0}
  ]
}
```

- `rules` — 기록 전부, 살아 있는 것·만료된 것·종료된 것. 기록은 지워지지 않으므로 누가 무엇을 왜
  결정했는지의 이력이다. `requested_by` 는 운영자 회수에서는 비어 있고, 부여에는 요청한 에이전트가
  실린다.
- `declared` — 강제 지점이 갖춰진 종류마다, 선언이 오늘 이 에이전트에게 주는 것. 판정이 읽는 그
  선언에서 만들므로 목록과 2단계 허용이 어긋나지 않는다. `server/*` 는 `allowed_tools` 없는 원격 MCP
  서버의 모든 도구를 뜻하는 표시이고 회수 대상이 아니다 — `server/tool` 이나 그 서버의 호스트를
  회수한다.
- `ceilings` — 소속 그룹과 `global` 의 확장 상한을, 선언한 그룹 이름과 함께.
- `denials` — 레지스트리가 이 에이전트에게 내린 최근 거부 50건, 새것부터. control plane 의 메모리에만
  있어 재시작하면 비어 있다. 강제 지점이 캐시로 답한 거부는 여기 오지 않는다.

### `POST /v1/access/revocations` — 운영자

```json
{"agent": "researcher", "kind": "memory", "target": "group:research:knowledge",
 "mode": "rw", "reason": "incident 311", "expires_in_s": 3600}
```

`201` 로 `effect: "deny"`, `decided_by: "operator"` 인 기록을 돌려준다. `expires_in_s` 는
선택이며, 없으면 종료할 때까지 유지된다.

### `DELETE /v1/access/rules/{rule_id}` — 운영자

회수를 되돌리거나 부여를 일찍 끝낸다. 기록은 `lifted_at` 이 채워진 채 남는다. 없는 id 는 `404`
(`NF_001`).

### 메모리 강제

Memory Service 는 환경에 아래 둘이 **모두** 있으면 **레지스트리 모드**로 뜬다 (하나만 있으면
`CFG_001` 로 기동을 거부한다):

| 변수 | 값 |
|---|---|
| `AAESTRO_ACCESS_URL` | Memory Service 에서 닿는 control plane 주소 |
| `AAESTRO_ACCESS_ENFORCER_TOKEN` | control plane 의 `access_enforcer_token` |

레지스트리 모드에서는:

- **메모리 토큰을 발급하지 않는다.** 에이전트는 배포가 주입한 신원을 내민다. 레지스트리가 설정된
  control plane 은 그 신원을 `AAESTRO_MEMORY_TOKEN` 으로 넣으며, 정적 토큰으로 되돌아가지 않는다.
- **모든 요청을 판정한다.** 서비스는 에이전트의 현재 선언으로 별칭을 해석한 뒤 그 space id 에 대해
  `ro`(읽기·검색·latest) 또는 `rw`(append) 를 묻는다. 거부는 기존과 같은 `401` + `MEM_001` 이고
  감사 로그에 남는다.
- **회수는 떠 있는 에이전트의 다음 요청부터 적용된다.** `rw`→`ro` 강등도 같다 — 읽기는 계속되고
  쓰기는 멈춘다. 아무것도 재시작하지 않는다.
- **space 를 지정하지 않은 검색은 지금 허용되지 않은 space 를 건너뛴다** — 검색 전체를 실패시키지
  않는다. `GET /v1/spaces` 는 허용된 space 를 실제로 허용된 mode 와 함께 보여 준다.
- **신원은 Memory Service 재시작을 넘는다.** 신원은 control plane 에 있다.
- **레지스트리에 닿지 않는 동안** 이미 캐시된 판정은 계속 동작하고, 아직 판정하지 않은 것은
  거부된다. 캐시된 판정도 `valid_until` 이 지나면 버린다.

서비스는 도는 동안 변경 알림을 따라간다. 알림이 끊겼지만 레지스트리가 답하는 동안에는 2초가 지난
캐시 판정을 다시 묻는다.

**run scope 는 레지스트리 모드가 필요하다.** 토큰 모드 Memory Service 는 run 없이 토큰을 발급하므로
run scope space 를 실을 수 없다. 카탈로그의 그래프가 `spec.memory.spaces` 를 선언했으면 토큰 모드
Memory Service 는 그 space 를 조용히 뺀 채 뜨지 않고 기동에서 실패한다 (`CFG_001`, 에러에는 그래프
이름 · space 별칭과 읽지 못한 선언의 저장소 기준 상대 위치만 실린다). 읽지 못한 그래프도 거부한다 —
그 안에 run scope 선언이 없다는 것을 증명할 수 없다. 검사는 기동 때 한 번뿐이라, 뜬 뒤에
`PUT /v1/graphs` 로 저장된 그래프는 떠 있는 서비스가 모른다. 토큰 모드 배포를 막는 일은 배포
검증 (PR #471 이후) 의 몫이다.

**양쪽을 함께 켠다.** `access_store` 를 켠 control plane 은 신원을 메모리 토큰으로 넣는데, 토큰 모드로
남은 Memory Service 는 그 신원을 몰라 모든 에이전트를 거부한다. 반대로 레지스트리 모드 Memory Service
와 레지스트리 없는 control plane 도 신원을 발급하는 곳이 없어 모든 에이전트를 거부한다.

### 이그레스 강제

이그레스 프록시(`python -m aaestro.egress`, 이미지 `aaestro/egress-proxy`)는 에이전트가 바깥으로 나가는
길이다. 도구를 호스팅하지 않고, 판정하고 전달만 한다.

| 창구 | 에이전트가 보내는 것 | 판정 |
|---|---|---|
| CONNECT (`8080`) | `HTTPS_PROXY` 를 통한 HTTPS, 프록시 자격으로 신원 | `host[:port]` 에 대한 `egress` — `:443` 은 생략 |
| Provider (`8081`) | `ANTHROPIC_BASE_URL` / `OPENAI_COMPAT_BASE_URL` 로 가는 모델 API 호출, 키 자리에 신원 | provider 의 논리 호스트(`api.anthropic.com`, `openai-compat.invalid`) 에 대한 `egress` |

프록시는 TLS 안을 보지 않는다. 모델 호출에서는 에이전트 신원을 떼고 자기가 쥔 키를 붙인 뒤 provider 의
응답을 그대로 흘려보내므로, **에이전트 환경에는 모델 키가 없고** 모델 접근 회수에 재배포가 필요 없다.

provider 마다 모델 호출에 필요한 경로만 연다 — `anthropic` 은 `POST v1/messages`,
`POST v1/messages/count_tokens`, `GET v1/models`, `openai-compat` 은 `POST v1/chat/completions`.
그 밖의 경로는 키를 붙이기 전에 `403 ACC_001` 로 거절하고, 맞춰 본 경로를 그대로 전달한다.
`openai-compat` 의 키는 `Authorization: Bearer <key>` 로 나간다. 에이전트가 보낸 `Authorization` 은
신원이라 항상 먼저 뗀다.

요청 헤더는 provider 별 허용 목록이다: `content-type`, `accept`, `accept-encoding`, `user-agent`,
그리고 `anthropic` 은 `anthropic-version` · `anthropic-beta`. 에이전트가 넣은 그 밖의 헤더는 떼어 낸다 —
컨테이너 안에서 고른 헤더가 운영자의 키와 함께 나가면 안 된다. 쿠키는 어느 방향으로도 건너지 않고,
프록시는 호출 사이에 쿠키를 기억하지 않는다: 한 에이전트의 upstream 세션이 다른 에이전트의 요청에
실리지 않는다.

`openai-compat` 의 판정 대상은 upstream 주소가 아니라 `openai-compat.invalid` 다: 운영자가 회수하는
것은 이 배치가 어디를 가리키든 "모델 API" 다. 합성 이름이다 — `.invalid` 는 예약 TLD 이고, CONNECT
창구는 `.invalid` 목적지를 레지스트리에 묻기 전에 `403 ACC_001` 로 거절하므로 provider 선언이 터널을
열지 않는다.

control plane 에서 켠다:

```yaml
runtime:
  egress_proxy:
    connect_url: http://aaestro-egress:8080
    providers_url: http://aaestro-egress:8081
    container: aaestro-egress   # MCP 사이드카를 쓸 때만 필요 — 아래 참조
    providers: [anthropic]      # 프록시가 종단하는 provider — upstream 을 설정했으면 openai-compat 을 더한다 (기본값 [anthropic])
```

배포 검증은 `providers` 에 없는 모델 provider(primary·fallback)를 거절한다 (`400 VAL_001`, finding `model_providers`).

그러면 배포가 에이전트마다 `HTTPS_PROXY`(자기 신원을 자격으로), 프록시를 가리키는 `ANTHROPIC_BASE_URL`
과 `OPENAI_COMPAT_BASE_URL`, 그리고 `ANTHROPIC_API_KEY` / `OPENAI_COMPAT_API_KEY` 자리에 신원을 넣는다 —
manifest 가 `env_allowlist` 에 올려도 실 키는 프록시에만 있다. 평문 `http` 는 프록시로 보내지 않는다 — 에이전트
네트워크의 프레임워크 서비스는 그대로 직접 닿는다. `orchestrator.access_store` 없이
`runtime.egress_proxy` 를 켜면 기동을 거부한다 (`CFG_001`).

#### 네트워크 격리

에이전트가 돌아갈 수 있는 프록시는 아무것도 판정하지 못한다. 그래서 **프록시를 켜면 에이전트 네트워크가
격리된다**. `runtime.network` 는 외부 경로 없는 내부 Docker 네트워크(`--internal`)가 된다: 에이전트
컨테이너 안에서 그 네트워크 밖의 이름은 풀리지 않고, 밖의 주소로 직접 연결하면 실패한다. 나가는 길은
프록시뿐이다.

- **두 네트워크에 함께 붙는 것은 프록시와 프레임워크 서비스뿐이다.** 이그레스 프록시, Memory Service,
  `orchestrator.access_agent_url` 에 답하는 것을 에이전트 네트워크와 외부 경로가 있는 네트워크에 함께
  붙인다. 에이전트는 에이전트 네트워크에서 이름으로 닿는다.
- **이미 있는 네트워크는 내부 네트워크여야 한다.** 없으면 control plane 이 만든다. 이미 있는
  네트워크의 격리가 다르면 재사용하지 않고 에이전트 기동을 거부한다 (`RT_001`, 재시도 불가) — 외부
  경로가 있는 네트워크라면 에이전트가 프록시를 우회할 수 있다. 그 네트워크를 지우거나 다시 만든다.
- **재부착도 격리를 확인한다.** 재시작 뒤 배포는 에이전트 네트워크가 내부 네트워크이고 각 컨테이너가 그
  네트워크에만 붙어 있을 때만 재부착한다. 아니면 배포는 `lost`("network isolation mismatch")가 되고
  아무것도 붙이지 않으며 그 에이전트 신원을 회수한다 — 외부 경로가 남은 컨테이너는 메모리·peer·모델에
  닿지 못한다. 컨테이너는 원인을 보고 해체하도록 남겨 둔다.
- **에이전트는 포트를 게시하지 않는다.** 내부 네트워크에서는 포트를 게시할 수 없으므로 control plane 은
  그 네트워크에서의 컨테이너 주소로 Control API 에 닿고, 재시작 뒤 재부착도 주소를 다시 구한다. control
  plane 이 그 주소에 닿아야 한다: 에이전트 네트워크 안에서 돌리거나, 그 네트워크의 브리지를 가진 Linux
  호스트에서 돌린다.
- **에이전트는 여전히 호스트에 닿는다.** 여느 브리지 네트워크처럼 에이전트 네트워크의 게이트웨이
  주소로 닿는다 — 모든 인터페이스에서 받는 호스트 서비스는 프록시가 보지 못하는 출구다.
  `deployments/docker/isolate-agent-network.sh apply <network>` (root) 로 닫는다: 에이전트가 호스트로
  여는 연결은 막고, 호스트가 연 연결의 응답은 통과시키므로 control plane 은 계속 에이전트에 닿는다.
  [runbook](runbooks/access-control.md#호스트-게이트웨이-닫기) 참조. 프록시를 켰는데 control plane 이
  루프백이 아닌 주소에 바인드하면 기동 시 경고한다 — 그 자신이 그런 서비스이기 때문이다.

`runtime.egress_proxy` 가 없으면 에이전트는 예전처럼 루프백에 포트를 게시하는 일반 네트워크에 있다.

목적지는 매니페스트에 선언한다 — 호스트 또는 `host:port`, 와일드카드·스킴·경로는 받지 않는다:

```yaml
spec:
  runtime:
    egress: [api.search.example.com, feeds.example.com:8443]
```

프록시 프로세스 설정:

| 변수 | 의미 |
|---|---|
| `AAESTRO_ACCESS_URL`, `AAESTRO_ACCESS_ENFORCER_TOKEN` | 레지스트리 — 둘 다 필수, 없으면 기동 거부 |
| `ANTHROPIC_API_KEY` | 프록시가 모델 호출에 붙이는 키 |
| `AAESTRO_EGRESS_ANTHROPIC_UPSTREAM` | 모델 호출이 가는 곳 (기본 `https://api.anthropic.com`) |
| `OPENAI_COMPAT_API_KEY` | 프록시가 `openai-compat` 호출에 `Authorization: Bearer` 로 붙이는 키. 키가 필요 없는 로컬 서버도 자리는 채운다. 설정했는데 공백뿐인 provider 키는 기동을 거부한다 (`CFG_001`) — `ANTHROPIC_API_KEY` 도 같다 |
| `AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM` | `openai-compat` 호출이 가는 곳 — **`/v1` 앞까지**: `https://api.openai.com`, `http://vllm:8000`, `https://gateway.example.com/llm`. 기본값이 없다. 키와 함께 두거나 둘 다 두지 않는다. 하나만 있거나 `/v1` 로 끝나면 기동을 거부한다 (`CFG_001`). 아래 평문 `http` 규칙이 적용된다 |
| `AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM` | `true` 이고 목적지가 `AAESTRO_EGRESS_PRIVATE_DESTINATIONS` 에도 **있을 때만** 평문 `http` 를 받는다 — 사설망의 테스트 대역용. 요청마다 이름을 풀어 사설 주소로만 붙고, 목록의 이름이 공인 주소로 풀리면 거부한다(`502`). 공인 주소로는 설정을 어떻게 해도 평문이 나가지 않는다 |
| `AAESTRO_EGRESS_PORT`, `AAESTRO_EGRESS_PROVIDER_PORT` | CONNECT 창구와 provider 창구 (기본 `8080`, `8081`) |
| `AAESTRO_EGRESS_MODE` | `enforce`(기본) 또는 `record` |
| `AAESTRO_EGRESS_PRIVATE_DESTINATIONS` | 사설 주소로 풀려도 되는 목적지, 쉼표로 |

응답: 거부된 목적지는 `403`(`ACC_001`), 레지스트리에 닿지 않고 캐시도 없어 판정하지 못하면
`503`(`ACC_002`), 신원이 없거나 모르는 신원이면 CONNECT 는 `407`, provider 창구는 `401` 이다. 설정 형식이
틀리면 기동을 거부하고(`CFG_001`), 판정 피드나 두 창구 중 하나가 끝나도 프로세스가 멈춘다 — 재시작
정책 아래에서 돌린다.

**사설 주소.** 프록시는 외부 네트워크에 있으므로 에이전트가 닿지 못하는 곳 — 클라우드 메타데이터 주소,
호스트의 서비스 — 에 닿을 수 있다. 사설·루프백·링크 로컬·공유(CGNAT) 주소로 풀리는 목적지는
`AAESTRO_EGRESS_PRIVATE_DESTINATIONS` 에 없으면 거부하고, 프록시는 확인한 주소로 연결하므로 판정과 연결
사이에 이름이 다른 주소로 바뀌지 못한다.

**기록 모드.** `record` 는 거부를 로그(`egress denied but recorded only`)로 남기고 통과시킨다. 강제하기
전에 선언되지 않은 목적지를 드러낼 때 쓴다. 신원이 없거나 모르는 신원, 판정할 수 없는 경우는 두 모드
모두 거부한다 — 기록할 에이전트가 없다.

#### 원격 MCP 도구

프록시는 원격 MCP 서버(`url` 이 있는 `streamable-http`)도 종단한다. `runtime.egress_proxy` 를 켜면 agentd 는
선언된 URL 대신 `http://<proxy>:8081/mcp/<server>` 로 접속하고, 서버 자격 자리에 자기 신원을 싣는다. 프록시는
요청마다:

1. 신원으로 에이전트를 알고 **그 에이전트의 선언**에서 서버 URL 과 `auth.token_env` 를 찾는다 — 요청은 서버
   이름만 담으므로 에이전트가 프록시의 자격을 다른 주소로 보내지 못한다
2. 서버 호스트를 `egress` 로, `tools/call` 마다 `server/tool` 을 `mcp_tool` 로 판정한다
3. 신원을 떼고 자격을 붙여, 확인한 주소로 연결한다

도구 하나를 회수하면 그 도구의 다음 호출이 거부되고 같은 서버의 다른 도구는 계속 동작한다:

```json
{"agent": "researcher", "kind": "mcp_tool", "target": "corp/search", "reason": "incident 311"}
```

거부된 도구 호출은 JSON-RPC 에러(`ACC_001: mcp_tool denied: corp/search`)로 답하고, 에이전트에는
`MCP_003` 으로, 사유는 `details.detail` 에 실려 닿는다. 거부된 호출이 섞인 배치는 통째로 거부한다
(`403`). 에이전트가 선언하지 않은 서버는 `404` 다.

이를 위해 프록시에 설정이 둘 더 필요하다:

| 변수 | 의미 |
|---|---|
| `AAESTRO_REPO_ROOT` | 읽기 전용으로 마운트한 선언 — 없으면 원격 MCP 호출은 `404` |
| `AAESTRO_EGRESS_MCP_TOKENS` | 프록시가 MCP 서버에 보내도 되는 자격 이름, 쉼표로. 각 값은 프록시 env 에 둔다 |

**목록에 있는 자격 이름만 보낸다.** 선언이 다른 비밀을 `auth.token_env` 로 적으면 그 값 대신
`502`(`CFG_002`) 로 답한다. 목록에도 프록시가 다른 용도로 쥔 비밀은 올리지 못한다 — `ANTHROPIC_API_KEY`, `OPENAI_COMPAT_API_KEY` 나
`AAESTRO_*` 이름이 있으면 기동을 거부한다(`CFG_001`). 원격 MCP 서버는 `https` 여야 한다: 평문 `http` 는
`AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM=true` 이고 **동시에** 그 서버가 `AAESTRO_EGRESS_PRIVATE_DESTINATIONS`
에 있을 때만 받고, 아니면 거부한다(`502`). 스위치 하나로는 아무것도 열리지 않으므로 운영 설정에 스위치가 남아
있어도 공인 호스트로 자격이 평문으로 나가지 않는다. 사설 주소로 풀리는 서버는 여느 목적지처럼 그 목록에 있어야
한다.

**세션은 한 에이전트와 한 서버의 것이다.** 모든 에이전트가 같은 프록시 자격으로 서버에 닿으므로 서버는 세션
주인을 가리지 못한다. 프록시는 돌려주는 `mcp-session-id` 마다 발급받은 에이전트·서버로 서명을 붙이고,
들어올 때 서명을 확인해 벗긴다. 다른 에이전트가, 다른 서버에 쓰거나, 고친 세션 id 는 `404` 다 — MCP
명세가 모르는 세션에 정한 응답이다. 서명 키는 프록시의 레지스트리 자격에서 끌어내므로 프록시를 재시작해도 세션이
유지된다.

배포는 원격 MCP 자격을 에이전트 env 에 넣지 않는다. 검증이 해석할 수 있도록 평소처럼 스코프에
선언한다(`env_allowlist` 와 그룹 또는 global `secrets` 목록). 원격 서버의 `auth.token_env` 를 stdio 서버의
`env_allowlist` 에 함께 적으면 매니페스트를 거부한다 — 프록시가 쥔 자격이 컨테이너 안 프로세스로 간다.

닿지 않는 것: stdio MCP 서버는 에이전트 컨테이너 안에서 돌기 때문에 도구 하나씩 판정하지 않는다 — 외부 효과는
프록시의 목적지 판정을 거치고, 로컬 효과는 실시간 통제 대상이 아니다.

**agentd 가 이제 선언된 MCP 세션을 연다.** 도구가 카드에 광고되고 첫 태스크부터 실행된다. 필수 서버가
실패하면 기동이 멈추고(`MCP_001`), 리로드는 떠 있는 세션을 유지한다.

#### MCP 사이드카

`sidecar` 로 선언한 서버는 에이전트 하나가 소유하는 별도 컨테이너에서 돈다:

```yaml
spec:
  mcp:
    servers:
      - name: browser
        transport: streamable-http
        sidecar:
          image: mcp/playwright:1.2.0   # 고정 태그
          resources: {cpu: "0.5", memory: 512Mi}
          port: 8000                    # 기본값
          path: /mcp                    # 기본값
        allowed_tools: [navigate, screenshot]
```

runtime 이 에이전트보다 먼저 `aaestro-<agent>--mcp-<server>` 라는 이름으로 띄우고, 배포를 해체하거나 되감을
때 지운다. 주소는 그 이름에서 나온다 — 사이드카는 URL 을 선언하지 않는다.

- **자기 네트워크.** 에이전트의 사이드카는 에이전트 네트워크가 아니라 내부 네트워크 `aaestro-<agent>--mcp` 에
  있다. `runtime.egress_proxy` 를 켜면 거기 함께 붙는 컨테이너는 프록시뿐이다: 에이전트는 원격 서버처럼
  `…/mcp/<server>` 를 부르고, 프록시가 `tools/call` 마다 `mcp_tool` 을 판정한다. 도구 하나를 회수하면 그
  도구의 다음 호출이 거부되고 같은 서버의 다른 도구는 계속 동작하며, 아무것도 재시작하지 않는다. 사이드카는
  바깥 목적지가 아니므로 `egress` 판정은 없다. 프록시가 없으면 소유 에이전트의 레플리카가 그 네트워크에 직접
  붙고, 다른 에이전트는 여전히 닿지 못한다.
- **프록시를 붙여야 한다.** `runtime.egress_proxy.container` 에 프록시 컨테이너 이름을 적는다. runtime 이
  사이드카 네트워크마다 그 컨테이너를 `connect_url` 의 호스트 이름으로 붙인다. 이 설정 없이 프록시 뒤에서
  사이드카를 쓰는 그래프는 거부한다(`CFG_001`) — 에이전트를 사이드카에 직접 이으면 도구 판정이 에이전트
  컨테이너 안의 검사로 떨어지기 때문이다.
- **에이전트와 같은 방호.** uid 1000, 쓸 수 있는 `/tmp`(`HOME` 도 여기)만 둔 읽기 전용 루트 파일시스템,
  `cap-drop ALL`, `no-new-privileges`, PID 상한, 게시 포트 없음. 선언하지 않은 리소스는 에이전트 기본값(CPU 1,
  1Gi)이고, 사이드카는 레플리카 수와 무관하게 하나씩 그룹 quota 에 합산된다.
- **환경.** 서버의 `env_allowlist` 만, 그리고 프록시가 켜져 있으면 소유 에이전트의 `HTTPS_PROXY` — 사이드카의
  바깥 호출은 그 에이전트의 신원으로 목적지마다 판정된다. 프록시가 없으면 사이드카에는 바깥 길이 없다.
- **평문 http, 사설 주소만.** 사이드카는 자격을 싣지 않으므로(사이드카에는 `auth` 를 선언할 수 없다) 프록시는
  `http` 로 부르고, 사설 주소로 풀릴 때만 보낸다.
- **재시작.** 오류로 끝난 사이드카는 Docker 가 다섯 번까지 다시 띄운다. control plane 이 재시작하면 재부착이
  사라지거나 멈춘 사이드카를 되살리고, 되살리지 못하면 배포를 `lost` 로 표시한다. agentd 는 아직 뜨는 중인
  사이드카를 10초쯤 기다린다 — 원격 서버는 기다리지 않는다.

### A2A 강제

레지스트리가 없으면 피호출자는 runtime 이 **그래프의 모든 에이전트에게** 준 키로 서명한 HMAC 토큰을
확인한다 — 누구든 다른 에이전트 행세의 토큰을 만들 수 있다. 레지스트리가 있으면 피호출자가 호출마다
직접 판정한다:

1. 호출자는 **자기 신원**으로 인증해 피호출자 하나에 쓸 **표**를 레지스트리에서 받는다. 표는 5분
   살고, 만료 30초 전까지 재사용한다.
2. 호출자는 표를 `x-aaestro-a2a-ticket` 에 실어 보낸다. 호출자 신원 자체는 피호출자에게 가지 않는다.
3. 피호출자의 A2A 서버는 **자기 신원**으로 인증해 표를 레지스트리에 보낸다. 레지스트리는 다른
   피호출자의 표, 만료된 표, 호출자 신원이 폐기된 표를 거부하고, 그 밖에는 (호출자, 피호출자) 에 대해
   `a2a` 를 판정한다.
4. 거부 — 또는 레지스트리에 닿지 않고 캐시도 없어 판정하지 못한 경우 — 는 태스크가 에이전트에 닿기
   전에 `A2A_004` 다. 피호출자는 판정을 캐시하고 자기 신원으로 변경 알림을 따라가며, 표의 만료를 넘겨
   캐시하지 않는다.

그래서:

- **연결을 회수하면 떠 있는 호출자의 다음 호출부터 적용된다.** 되돌리면 재배포 없이 호출이 돌아온다.
  아무것도 재시작하지 않는다.
- **호출자 쪽 검사를 건너뛰어도 소용없다.** edge token 으로, 다른 에이전트 이름으로, 또는 표 없이 포트를
  직접 부르면 피호출자가 거부한다.
- **표는 다른 곳에서 쓸모가 없다.** 표는 신원이 아니다: Memory Service 는 받지 않고, 다른 에이전트는
  피호출자가 달라 거부한다.

호출자도 선언된 연결을 먼저 확인한다 — 왕복을 아끼고 로컬에서 분명한 `A2A_004` 를 주지만, 그 검사는
편의이지 경계가 아니다.

레지스트리가 있든 없든 에이전트의 A2A 포트가 답하는 것은 둘뿐이다: 에이전트 카드
(`GET /.well-known/agent-card.json`, 자격 없이 읽는다) 와 `POST /` 의 JSON-RPC.

- **새 태스크만 받는다.** 처리하는 요청은 `SendMessage` 와 `SendStreamingMessage` 뿐이고, 태스크를
  만들기 전에 호출자를 확인한다 — 유효한 토큰이나 표가 없는 요청은 피호출자에 태스크를 만들지 못하고
  에이전트에 닿지 않는다.
- **태스크를 다시 읽거나 취소할 수 없다.** `GetTask`, `ListTasks`, `SubscribeToTask`, `CancelTask`,
  push 알림 설정 요청, `GetExtendedAgentCard` 는 "This operation is not supported" (`-32004`) 로
  거절한다. 응답은 누가 묻든, 어떤 task id 든 같으므로 태스크가 있는지 알려 주지 않는다.
- **기존 태스크에 메시지를 이을 수 없다.** `taskId` 를 지정한 send 도 같은 응답으로 거절한다. 위임은
  늘 새 태스크다.
- **태스크 store 는 끝난 태스크를 쥐지 않는다.** 태스크를 실행 중에만 들고, 완료·실패·거부되면
  store 에서 버린다 — 결과는 응답으로만 간다. 알려진 한계: 에이전트 실행이 취소로 끝난 태스크는
  SDK 내부의 활성 태스크 레지스트리에 남을 수 있다 (SDK 동작, 별도 이슈).

### `POST /v1/access/a2a/tickets` — 모든 에이전트

```json
{"callee": "planner"}
```

`201` 에 `{"ticket": "...", "callee": "planner", "expires_at": 1789381500.0}`. 표는 신원 증명이지
허가가 아니다: 회수된 연결에도 발급되며, 피호출자가 호출을 거부한다. 모르거나 폐기된 신원은 `403`
(`ACC_001`), 없는 피호출자는 `404` 다.

### `POST /v1/access/a2a/verify` — 피호출자

```json
{"ticket": "<호출자가 보낸 표>"}
```

```json
{"agent": "researcher", "decision": "allow", "decided_by": "declaration", "version": 42,
 "valid_until": 1789381500.0}
```

이 피호출자의 것이 아니거나, 만료됐거나, 위조됐거나, 호출자 신원이 폐기된 표는 `200` 에
`"agent": null, "decision": "deny", "decided_by": "invalid-ticket"` 이다. bearer 는 살아 있는 에이전트
신원이어야 한다 — 아니면 `403` (`ACC_001`).

## 운영 시 알아 둘 것

- **에이전트 하나에 배포 하나.** 에이전트를 공유하는 두 그래프는 동시에 배포할 수 없다
  (`409`, `RT_010`).
- **배포 중에는 편집이 막힌다.** 의도된 동작이다 — 컨테이너는 특정 선언으로 만들어졌고,
  파일이 따로 흘러가면 그 배포를 재현할 수 없게 된다.
- **살아 있는 시스템을 수정한다**는 것은: 새 버전을 배포하고 옛것을 해체하는 것이다.
  실행 중 배포의 in-place 패치는 지원하지 않는다.
- **재시작하면 다시 붙는다.** 기동 시 기록과 Docker 를 대조해 컨테이너를 health 감시까지
  포함해 다시 잡는다. 컨테이너가 사라진 기록은 조용히 지우지 않고 `lost` 로 표시한다.

## CLI 대응

| API | CLI |
|---|---|
| `POST /v1/runs` | `aaestro run --deployment <id> --input '{...}'` |
| `GET /v1/runs` | `aaestro run-list [--mode service]` |
| `POST /v1/runs/{id}/drain` | `aaestro run-drain <id>` |
| `POST /v1/runs/{id}/resume` | `aaestro run-resume <id>` |
| `POST /v1/runs/{id}/steer` | `aaestro run-steer <id> "<text>" [--node <node>] [--by <who>]` |
| `PUT /v1/agents/{name}/materials` | `aaestro agent-push <name> <directory>` |
| `POST` + `GET /v1/agents/{name}/image` | `aaestro agent-build <name> [--wait]` |

이 명령들은 `--control-url` 과 `--control-token` (또는 `AAESTRO_CONTROL_TOKEN`) 을 받는다.
`agent-push` 는 디렉토리를 읽되 `__pycache__` 같은 캐시 디렉토리는 건너뛰고 심볼릭 링크는
거절한다. `agent-build --wait` 은 빌드가 실패하면 로그 꼬리를 출력하고 0 이 아닌 코드로
끝난다.

`aaestro validate` 를 표에서 뺀 것은 의도적이다 — 저장소를 직접 읽는 **로컬** 명령이고
control plane 플래그를 받지 않는다. 저장하지 않은 초안을 검증하는 원격 대응이
`POST /v1/validate` 다. 전체 명령 레퍼런스는 [루트 README](../../README.md#commands) 참조.
