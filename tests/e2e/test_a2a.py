"""A2A across the container boundary.

#118 이 SDK 를 바인딩하고 #166 이 서버를 띄웠지만, 지금까지 그 경로는
**같은 프로세스 안에서만** 검증됐다 (ASGI). 여기서는 실제 컨테이너 사이로
왕복시킨다 (#162).

allowlist 는 그래프의 `connections` 선언이 원본이다 —
`research-pipeline.yaml` 은 `researcher → planner` 만 선언하므로 역방향은
`A2A_004` 로 거부돼야 한다.
"""

from __future__ import annotations

import os

import pytest

from aaestro.core.errors import ErrorCode
from aaestro.protocols.a2a.allowlist import Edge, issue_token
from tests.e2e.test_graph_run import graph_stack  # noqa: F401 — fixture 재사용
from tests.e2e.test_stack import docker, requires_docker

pytestmark = pytest.mark.e2e

A2A_PORTS = {"planner": 19102, "researcher": 19103, "writer": 19104}

SECRET = os.environ.get("AAESTRO_A2A_SECRET", "e2e-a2a-secret")

CALLER_CONTAINER = "aaestro-e2e-agent-researcher-1"
"""호출을 실행할 컨테이너.

**컨테이너 안에서 불러야 한다**: 카드가 광고하는 주소(`agent-planner`)는
compose 네트워크에서만 해석된다. 호스트에서 부르면 그 이름을 풀지 못하고,
loopback 을 광고하도록 우회하면 **peer 가 자기 자신을 가리키는** 카드를
받게 되어 실제 배포에서 깨진다.
"""


def call_from_container(caller: str, callee: str, *, token: str, depth: int = 0) -> str:
    """researcher 컨테이너 안에서 peer 를 호출하고 결과를 문자열로 돌려준다."""
    script = f"""
import asyncio, json
from aaestro.core.agent import TaskConfig, TaskRequest, TraceContext
from aaestro.core.errors import AaestroError
from aaestro.protocols.a2a.sdk import SdkPeerTransport

async def main():
    transport = SdkPeerTransport(
        agent={caller!r},
        addresses={{"planner": "http://agent-planner:19102",
                    "researcher": "http://agent-researcher:19103"}},
        timeout_s=30.0,
    )
    task = TaskRequest(
        task_id="e2e-a2a", run_id="e2e", node_id={callee!r},
        input={{"query": "무엇부터 볼까"}}, config=TaskConfig(),
        trace=TraceContext(trace_id="tr", depth={depth}),
    )
    try:
        result = await transport.send(
            callee={callee!r}, task=task, token={token!r}, headers={{}}
        )
        print("OK:" + json.dumps(result.output, ensure_ascii=False)[:120])
    except AaestroError as err:
        print("ERR:" + str(err.code))
    finally:
        await transport.close()

asyncio.run(main())
"""
    return docker("exec", CALLER_CONTAINER, "python", "-c", script).strip()


def token_for(caller: str, callee: str) -> str:
    return issue_token(SECRET.encode("utf-8"), Edge(caller=caller, callee=callee))


@requires_docker
def test_a_declared_call_crosses_the_container_boundary(graph_stack):  # noqa: F811
    """#162 의 핵심 — 같은 프로세스가 아니라 **컨테이너 사이**를 건넌다."""
    output = call_from_container("researcher", "planner", token=token_for("researcher", "planner"))

    assert output.startswith("OK:"), output


@requires_docker
def test_an_undeclared_direction_is_refused(graph_stack):  # noqa: F811
    """그래프는 researcher→planner 만 선언한다 — 역방향은 거부돼야 한다.

    03 Rule 6: 호출 방향은 순전히 선언의 문제다.
    """
    output = call_from_container("planner", "researcher", token=token_for("planner", "researcher"))

    assert output == f"ERR:{ErrorCode.A2A_004}", output


@requires_docker
def test_a_forged_token_is_refused(graph_stack):  # noqa: F811
    """이름만 주장하는 호출을 막는 것이 per-edge token 의 존재 이유다."""
    output = call_from_container("researcher", "planner", token="forged-token")

    assert output == f"ERR:{ErrorCode.A2A_004}", output


@requires_docker
def test_a_deep_chain_is_refused(graph_stack):  # noqa: F811
    """깊이 상한은 수신 측에서도 본다 — caller 의 정직함에 기대면 순환이 뚫린다."""
    output = call_from_container(
        "researcher", "planner", token=token_for("researcher", "planner"), depth=9
    )

    assert output == f"ERR:{ErrorCode.A2A_005}", output
