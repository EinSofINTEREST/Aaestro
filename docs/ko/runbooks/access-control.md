# 권한 통제 운영

한국어 | **[English](../../en/runbooks/access-control.md)**

떠 있는 에이전트의 권한을 회수하는 절차와, 권한 레지스트리에 닿지 않을 때의 동작.
배경: [권한 통제](../architecture.md#권한-통제--컨테이너-밖에서-판정한다).

## 긴급 회수

에이전트가 닿는 무언가를 지금 끊어야 한다 — 새어 나간 memory space, 외부 호스트, 원격 MCP 도구,
다른 에이전트로의 연결.

1. **권한을 회수한다.** 화면에서: **권한** 탭 → 에이전트 선택 → 사유 입력 → 선언 권한 줄의
   **회수**(메모리는 **쓰기 회수** / **전체 회수**). API 로:

   ```bash
   curl -X POST -H "Authorization: Bearer $AAESTRO_CONTROL_TOKEN" -H 'content-type: application/json' \
     -d '{"agent": "researcher", "kind": "egress", "target": "api.search.example.com",
          "reason": "incident 311"}' \
     http://127.0.0.1:8700/v1/access/revocations
   ```

   | 끊을 것 | `kind` | `target` | `mode` |
   |---|---|---|---|
   | memory space 쓰기만, 읽기는 유지 | `memory` | space id, 예: `group:research:knowledge` | `rw` |
   | memory space 전체 | `memory` | space id | 생략 |
   | 외부 호스트 | `egress` | `host` 또는 `host:port` (`:443` 은 생략) | 생략 |
   | 원격 MCP 도구 하나 | `mcp_tool` | `server/tool` | 생략 |
   | 원격 MCP 서버 전체 | `egress` | 그 서버의 호스트 | 생략 |
   | 다른 에이전트 호출 | `a2a` | 피호출자 이름 | 생략 |
   | 모델 API 호출 (운영자 키) | `model` | provider 이름, 예: `anthropic` | 생략 |

   회수는 선언과 부여를 모두 이기고, 그 에이전트의 **다음 요청**부터 적용된다. 재시작도 재배포도
   없다.

2. **적용을 확인한다.** 에이전트의 다음 시도가 **최근 거부** 에 `decided_by: operator` 로 뜨거나
   `GET /v1/access/agents/{name}` 의 `denials` 에 보인다. 강제 지점은 거부를 로그로 남기고
   (`decision: deny`), `aaestro_access_grants_total{op="revoke"}` 가 회수를 센다.

3. **그 에이전트가 하는 모든 것을 멈춰야 하면** 배포를 해체한다 (`DELETE /v1/deployments/{id}` 또는
   **해체**). 에이전트가 drain 후 정지하고 나면 그 배포의 신원이 폐기된다 — 정지에 실패한 컨테이너도
   모든 강제 지점에서 거부된다. drain 은 진행 중 태스크를 기다리므로, 초가 급하면 1번의 권한 회수를
   먼저 한다.

4. **해소되면 되돌린다.** 탭의 **되돌리기**, 또는 `DELETE /v1/access/rules/{rule_id}`. 기록은
   `lifted_at` 과 함께 남는다.

### 회수가 닿지 않는 것

- 에이전트가 이미 가진 것: 읽어 둔 기억, 받은 응답, 컨테이너 안에 쓴 파일.
- 컨테이너 안에 머무는 효과 — stdio MCP 서버의 로컬 동작을 포함해서.
- 환경변수로 주입된 secrets. 회수하려면 비밀을 교체하고 재배포한다. 모델 키와 원격 MCP 자격은 여기
  해당하지 않는다 — 이그레스 프록시가 쥔다.
- 넓혀진 권한의 출처. 권한 에이전트가 준 것이면 여기서 회수하고, 그 요청들은
  [AccessGrantRefusalsSpike](incident-response.md#accessgrantrefusalsspike) 로 살핀다.

## 레지스트리에 닿지 않을 때

알림: [AccessRegistryUnreachable](incident-response.md#accessregistryunreachable).

강제 지점이 레지스트리에 닿지 못하는 동안:

| 판정 | 동작 |
|---|---|
| 아직 캐시에 없음 | **거부** (`MEM_001`, `A2A_004`, 프록시는 `ACC_002` / `503`) |
| 선언이 근거인 캐시된 허용 | 계속 동작 |
| 부여가 근거인 캐시된 허용 | `valid_until` 까지 동작하고, 그 뒤로는 장애가 끝날 때까지 거부 |
| 기한부 회수가 근거인 캐시된 거부 | `valid_until` 까지 거부이고, 그 뒤로도 장애 동안은 거부 |
| 장애 중에 한 회수 | 레지스트리에 다시 닿기 전까지 **반영되지 않음** |

의도한 동작이다: 에이전트는 이미 허용된 일을 계속하고, 새로 허용되는 것은 없다. 대가는 장애 중에
회수가 되지 않는다는 것이다.

1. control plane 을, 또는 알림의 `component` 와 그 사이의 네트워크를 복구한다. 강제 지점은 스스로
   다시 붙는다. 끊긴 동안 레지스트리 버전이 움직였다면(장애 중에 한 회수) 캐시를 버리고, 그 회수가
   다음 요청부터 적용된다.
2. 회수를 기다릴 수 없으면 control plane 없이 해당 에이전트를 멈춘다:
   `docker stop aaestro-<agent>-<replica>`. control plane 이 돌아오면 배포를 해체해 기록을 맞춘다.
3. 레지스트리가 답하면서 강제 지점을 **거절**하는 경우(강제 지점 토큰이 틀림)는 장애가 아니다: 그
   강제 지점은 캐시를 버리고 모두 거부한다. 토큰을 고친다.

## 호스트 게이트웨이 닫기

이그레스 프록시를 켜면 에이전트는 외부 경로 없는 내부 네트워크에 있다 — 단, 그 네트워크의 게이트웨이인
호스트 자신에게는 닿는다. 모든 인터페이스에서 받는 호스트 서비스는 프록시를 거치지 않고 에이전트가 닿을
수 있다. 에이전트 네트워크마다 한 번, 호스트에서 닫는다:

```bash
sudo deployments/docker/isolate-agent-network.sh apply aaestro-net     # runtime.network
sudo deployments/docker/isolate-agent-network.sh status aaestro-net
```

- 그 네트워크 브리지에 대해 `INPUT` 에 체인을 하나 더한다: 에이전트가 호스트로 **여는** 연결은 버리고,
  호스트가 연 연결의 응답(control plane 이 에이전트 Control API 를 부르는 것)은 통과한다. `ip6tables`
  가 있으면 IPv6 에도 같은 규칙을 건다.
- 네트워크가 생긴 **뒤에** 적용한다 (첫 격리 배포 때 control plane 이 만든다). 재부팅이나 방화벽 재적재
  뒤에도 다시 적용한다 — 규칙은 영속되지 않는다.
- 에이전트가 닿아야 하는 서비스(이그레스 프록시, Memory Service, `orchestrator.access_agent_url` 에 답하는
  것)는 호스트가 아니라 **에이전트 네트워크 위에** 둔다. 이 규칙 뒤에는 게이트웨이로 닿지 않는다.
- `remove` 로 뺀다. 내부 네트워크가 아닌 곳에는 `apply` 를 거부한다.

프록시를 켰는데 control plane 이 루프백이 아닌 주소에 바인드하면 기동 시 경고한다 (`control plane is
reachable from isolated agents through the network gateway`). 루프백에 바인드하거나 규칙을 적용한다.

## #399 이전의 모델 provider 기록

#399 전에는 모델 종단을 provider 의 논리 호스트(`api.anthropic.com`, `openai-compat.invalid`) 에 대한
`egress` 로 판정했다. 지금은 provider 이름에 대한 `model` 이다. 권한 저장소에 이미 있는 기록은 고쳐 쓰지
않는다:

- 두 호스트에 대한 `egress` **회수** (`effect: deny`) 는 여전히 모델 종단을 끊는다 — 레지스트리가 그것을
  그 provider 의 `model` 회수로 세므로 업그레이드가 회수를 조용히 풀지 않는다. `api.anthropic.com` 으로의
  CONNECT 터널도 여전히 끊는다. 새 형식으로 옮기려면 같은 에이전트에 `model` / `anthropic`
  (또는 `openai-compat`) 을 회수한 뒤 옛 기록을 되돌린다 (**되돌리기** 또는
  `DELETE /v1/access/rules/{rule_id}`).
- 두 호스트에 대한 `egress` **부여** (`effect: allow`) 는 더 이상 모델 종단을 열지 않는다 — 그것이 결함이었다.
  부여는 모두 만료가 있어 스스로 끝나고, 그때까지는 예전처럼 `api.anthropic.com` 으로의 CONNECT 터널만
  연다. 할 일은 없지만 터널이 필요 없으면 되돌린다.
- `spec.model` 에 provider 를 선언하지 않고 `runtime.egress` 나 그런 부여로만 모델에 닿던 에이전트는 이제
  모델 호출이 `403 ACC_001` 이다. provider 를 선언하고 재배포한다.
- 상한의 `egress` 에 `api.anthropic.com` / `openai-compat.invalid` 가 있으면, CONNECT 터널을 뜻한 것이 아니라면
  지운다 — 이제 모델 접근과 무관하다.

이것은 `api.anthropic.com` · `openai-compat.invalid` 에 대한 `egress` 회수라면 **만든 시점과 무관하다** —
업그레이드 전이든 뒤든 그 provider 의 `model` 판정도 막는다. 터널만 막는 회수는 없다. 터널만 막으려면
그 호스트를 에이전트의 `runtime.egress` 에서 빼고 (선언 변경) 그 호스트에 대한 부여를 되돌린다 — 모델은
건드리지 않는다.

모델 호출이 거부돼 원인을 찾을 때: **최근 거부** / `denials` 에는 kind `model`, target 은 provider 이름,
`decided_by: operator` 로 뜨지만 `rules` 에는 `model` 기록이 없다 — 회수는 provider 의 옛 호스트에 대한
`egress` 기록이다.

기록 찾기: `GET /v1/access/agents/{name}` 의 `rules` 에서 kind 가 `egress` 이고 target 이 두 호스트 중 하나인 것.

## 권한을 넓힐 때

운영자는 부여하지 않는다. 작업 에이전트가 A2A 로 권한 에이전트에게 요청하고, 레지스트리가 줄 수 있는
범위를 그룹의 `spec.access.ceiling`(과 `global` 의 것) 안으로 제한한다. 더 허용하려면 그룹 선언의
상한을 바꾼다 — 권한 에이전트도, 요청도 그것을 바꾸지 못한다.
[권한 에이전트](../api.md#권한-에이전트) 참조.

## 함께 보기

- [incident-response.md](incident-response.md) — 알림과 1차 대응
- [권한 레지스트리 API](../api.md#권한-레지스트리)
