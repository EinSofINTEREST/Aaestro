"""End-to-end graph runs over the live stack.

살아있는 컨테이너 스택 위에서 mission/service run 을 굴린다. 노드 실행은
실제 Control API 를 거치며, 모델은 echo 대역이라 검증 대상이 **라우팅과 state
병합**으로 좁혀진다 — 모델 출력이 흔들려 테스트가 깨지지 않는다.

Docker 가 없으면 skip 한다. nightly CI 에서만 실행된다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.checkpoint import build_checkpointer
from aaestro.orchestrator.run import RunStatus
from aaestro.orchestrator.submit import RunSubmitter
from aaestro.orchestrator.topology import GraphTopology
from aaestro.runtime.control import ControlClient
from aaestro.runtime.nodes import ControlNodeRuntime
from tests.e2e.test_stack import (
    AGENT_TOKEN,
    COMPOSE_FILE,
    compose_up,
    docker,
    requires_docker,
    wait_healthy,
)

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_PORTS = {"planner": 18082, "researcher": 18083, "writer": 18084}


def topology(name: str) -> GraphTopology:
    """저장소의 레퍼런스 그래프를 읽는다."""
    document = yaml.safe_load((REPO_ROOT / "graphs" / f"{name}.yaml").read_text("utf-8"))
    return GraphTopology.model_validate(document)


@pytest.fixture(scope="module")
def graph_stack() -> Iterator[dict[str, int]]:
    """세 에이전트가 뜬 스택 — finalizer 가 반드시 정리한다."""
    compose_up()
    try:
        for port in AGENT_PORTS.values():
            assert wait_healthy(f"http://127.0.0.1:{port}"), f"agent on {port} never became healthy"
        yield AGENT_PORTS
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


@pytest.fixture
async def node_runtime(graph_stack: dict[str, int]):
    """실제 Control API 를 호출하는 노드 런타임.

    각 ControlClient 가 httpx.AsyncClient 를 들고 있으므로 finalizer 에서
    반드시 닫는다 — 안 닫으면 커넥션이 새고 unclosed-client 경고가 뜬다.
    """
    clients = {
        name: ControlClient(f"http://127.0.0.1:{port}", agent=name, token=AGENT_TOKEN)
        for name, port in graph_stack.items()
    }
    try:
        yield ControlNodeRuntime(clients=clients)
    finally:
        for client in clients.values():
            await client.aclose()


def submitter(runtime: ControlNodeRuntime) -> RunSubmitter:
    return RunSubmitter(runtime=runtime, checkpointer=build_checkpointer("memory"))


@requires_docker
async def test_mission_run_reaches_the_end(node_runtime):
    """레퍼런스 mission 그래프가 실제 컨테이너를 거쳐 END 에 도달한다."""
    result = await submitter(node_runtime).submit(
        topology("research-pipeline"), {"query": "왜 하늘은 파란가"}, run_id="e2e-mission"
    )

    assert result.ok, result.error.message if result.error else "run failed"
    assert result.status is RunStatus.COMPLETED


@requires_docker
async def test_mission_run_visits_every_node(node_runtime):
    """conditional edge 가 리서치 경로를 택해 세 노드를 모두 거친다."""
    await submitter(node_runtime).submit(
        topology("research-pipeline"), {"query": "q", "needs_research": True}
    )

    assert node_runtime.invoked == ["planner", "researcher", "writer"]


@requires_docker
async def test_nodes_receive_the_run_id_over_the_wire(graph_stack):
    """run_id 하나로 전 계층 로그를 잇는다 — 컨테이너를 건너도 유지되어야 한다.

    **그래프가 실제로 넘긴 TaskRequest** 를 관찰한다. 별도의 직접 호출을 만들어
    검증하면, 오케스트레이터가 run_id 를 통째로 누락시켜도 테스트가 통과한다.
    """
    run_id = "e2e-traced"
    observed: list[tuple[str, str]] = []

    clients = {
        name: ControlClient(f"http://127.0.0.1:{port}", agent=name, token=AGENT_TOKEN)
        for name, port in graph_stack.items()
    }

    class Observing:
        """실제 Control API 로 넘기되, 넘어간 태스크를 기록한다."""

        def __init__(self) -> None:
            self._inner = ControlNodeRuntime(clients=clients)

        async def invoke(self, node, task):
            observed.append((task.run_id, task.trace.trace_id))
            return await self._inner.invoke(node, task)

    try:
        await submitter(Observing()).submit(  # type: ignore[arg-type]
            topology("research-pipeline"), {"query": "q"}, run_id=run_id
        )
    finally:
        for client in clients.values():
            await client.aclose()

    assert observed, "no node was invoked"
    assert all(seen == (run_id, run_id) for seen in observed), observed


@requires_docker
async def test_mission_state_only_merges_declared_keys(node_runtime):
    """노드가 state 를 통째로 덮어쓰지 못한다 — output_map 선언 키만 병합된다."""
    result = await submitter(node_runtime).submit(
        topology("research-pipeline"), {"query": "원본 질의"}
    )

    assert result.state["query"] == "원본 질의"


@requires_docker
async def test_failed_node_frees_the_run_slot(graph_stack):
    """노드가 실패해도 슬롯이 반납되어야 다음 run 이 들어갈 수 있다."""

    class Unreachable:
        async def invoke(self, node, task):
            raise AaestroError(
                category=ErrorCategory.GRAPH, code=ErrorCode.GRAPH_002, message="unreachable"
            )

    submit = submitter(Unreachable())  # type: ignore[arg-type]
    result = await submit.submit(topology("research-pipeline"), {"query": "q"})

    assert not result.ok
    assert submit.manager.active(result.mode) == 0


@requires_docker
async def test_service_graph_is_rejected_for_completion(node_runtime):
    """상주 그래프는 완주 개념이 없다 — 제출 경로에서 막힌다."""
    with pytest.raises(AaestroError) as exc_info:
        await submitter(node_runtime).submit(topology("feed-monitor"), {})

    assert exc_info.value.code == "GRAPH_001"


@requires_docker
async def test_resume_continues_from_the_failed_node(node_runtime):
    """재개는 checkpoint 에서 잇는다 — 이미 끝난 노드의 컨테이너를 다시 부르지 않는다 (#309)."""

    class FailResearcherOnce:
        """researcher 의 첫 호출만 실패시키고 나머지는 실제 컨테이너로 보낸다."""

        def __init__(self) -> None:
            self.invoked: list[str] = []

        async def invoke(self, node, task):
            self.invoked.append(node.id)
            if node.id == "researcher" and self.invoked.count(node.id) == 1:
                raise AaestroError(
                    category=ErrorCategory.GRAPH, code=ErrorCode.GRAPH_002, message="injected"
                )
            return await node_runtime.invoke(node, task)

    runtime = FailResearcherOnce()
    submit = submitter(runtime)  # type: ignore[arg-type]
    failed = await submit.submit(topology("research-pipeline"), {"query": "q"}, run_id="e2e-resume")
    assert not failed.ok

    result = await submit.resume(topology("research-pipeline"), "e2e-resume")

    assert result.ok, result.error
    assert result.run_id == "e2e-resume"
    assert runtime.invoked == ["planner", "researcher", "researcher", "writer"]


@requires_docker
def test_every_agent_in_the_stack_is_non_root(graph_stack):
    """격리 계약은 그래프를 굴리는 스택에서도 유지된다."""
    for service in ("agent-planner", "agent-researcher", "agent-writer"):
        uid = docker("compose", "-f", str(COMPOSE_FILE), "exec", "-T", service, "id", "-u")
        assert uid == "1000", f"{service} runs as uid {uid}"


@requires_docker
async def test_the_refinement_graph_completes_over_the_live_stack(node_runtime):
    """모듈 조립만으로 만든 그래프가 실제 컨테이너를 거쳐 완주한다 (#210).

    04 는 순환에 `max_iterations` 를 요구하는데 어느 레퍼런스에도 순환이
    없었다 — 이 그래프가 그 규칙의 사례다.
    """
    result = await submitter(node_runtime).submit(
        topology("draft-review"), {"query": "왜 하늘은 파란가"}, run_id="e2e-draft"
    )

    assert result.ok, result.error.message if result.error else "run failed"
    assert result.state["approved"] is True
    assert result.state["draft"]
