"""Node execution over the Agent Control API.

오케스트레이터의 ``NodeRuntime`` 계약을 Control API 호출로 구현한다.
그래프는 이 계약 뒤의 컨테이너 사정을 알지 못한다 — 노드 하나가 어느
컨테이너의 어느 포트에 있는지는 runtime 만 안다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import agent_name

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.core.agent import TaskRequest, TaskResult
    from aaestro.core.topology import NodeSpec
    from aaestro.runtime.control import ControlClient

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class InflightTask:
    """A node task this runtime is waiting on — 어느 레플리카가 받았는지까지 쥔다.

    steer 는 태스크를 **받은** 컨테이너로 가야 한다. 이름으로 다시 라우팅하면 round-robin 이
    다른 레플리카를 고른다.
    """

    node_id: str
    task_id: str
    agent: str
    client: ControlClient


@dataclass(frozen=True)
class SteerReceipt:
    """Where a steer went and whether the agent could take it.

    steer 가 어디로 갔는지. ``supported`` 가 False 면 그 에이전트의 실행기가 steer 를
    지원하지 않아 아무것도 대기되지 않았다.
    """

    node_id: str
    task_id: str
    agent: str
    supported: bool


@dataclass
class ControlNodeRuntime:
    """Routes graph nodes to their agents' Control APIs.

    그래프 노드를 담당 에이전트의 Control API 로 라우팅한다.

    Attributes:
        clients: Agent name to its Control API client.
    """

    clients: Mapping[str, ControlClient]
    invoked: list[str] = field(default_factory=list)
    """호출된 노드 순서 — 라우팅 추적과 trace 출력에 쓴다."""
    inflight: dict[str, dict[str, InflightTask]] = field(default_factory=dict)
    """run_id → task_id → 지금 응답을 기다리는 노드 태스크. steer 가 "현재 실행 중인 노드" 를
    여기서 찾는다. 항목은 ``invoke`` 가 도는 동안만 있고 ``finally`` 가 지운다 — run 이 끝나면
    그 run 의 키도 없다. 넣고 빼는 사이에 await 가 없어 락이 필요 없다."""

    async def invoke(self, node: NodeSpec, task: TaskRequest) -> TaskResult:
        """Execute one graph node.

        노드 하나를 실행합니다.

        Args:
            node: The graph node being executed.
            task: The task to hand to its agent.

        Returns:
            The agent's result.

        Raises:
            AaestroError: GRAPH/``GRAPH_002`` if the node declares no agent or
                the agent has no running container — 노드가 가리키는 대상이
                없으면 그래프가 진행할 수 없습니다.
        """
        if node.agent is None:
            # agent 가 없는 노드는 subgraph 다. 이 런타임은 에이전트 호출만
            # 담당하므로, 조용히 건너뛰는 대신 명확히 실패한다 —
            # 건너뛰면 그래프가 실행된 것처럼 보이면서 아무 일도 하지 않는다
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_002,
                message=f"node is not bound to an agent: {node.id}",
                details={"node_id": node.id, "graph_ref": node.graph},
            )

        name = agent_name(node.agent)
        client = self.clients.get(name)
        if client is None:
            raise AaestroError(
                category=ErrorCategory.GRAPH,
                code=ErrorCode.GRAPH_002,
                message=f"no running container for agent: {name}",
                agent=name,
                details={"node_id": node.id, "module_ref": node.agent},
            )

        self.invoked.append(node.id)
        log.info(
            "graph node invoking agent",
            agent=name,
            node_id=node.id,
            run_id=task.run_id,
            task_id=task.task_id,
        )
        running = InflightTask(node_id=node.id, task_id=task.task_id, agent=name, client=client)
        self.inflight.setdefault(task.run_id, {})[task.task_id] = running
        try:
            return await client.invoke(task)
        finally:
            tasks = self.inflight.get(task.run_id, {})
            tasks.pop(task.task_id, None)
            if not tasks:
                self.inflight.pop(task.run_id, None)

    async def steer(
        self, run_id: str, text: str, *, by: str, node: str | None = None
    ) -> SteerReceipt:
        """Forward an operator instruction to the task a run is executing right now.

        run 이 지금 실행 중인 노드 태스크에 운영자 지시를 전달합니다. 접수이지 반영이
        아닙니다 — 반영은 그 에이전트의 다음 턴 경계에서 일어납니다.

        Args:
            run_id: The run to steer.
            text: The instruction.
            by: Who issued it — 감사용.
            node: The node to steer. 생략하면 지금 실행 중인 노드 — 하나일 때만.

        Returns:
            Which task received it, and whether its executor supports steering.

        Raises:
            AaestroError: NOT_FOUND/``NF_001`` if the run has no task in flight here (끝났거나,
                노드 사이이거나, 이 프로세스가 구동하지 않는 run) or the named node is not
                running; VALIDATION/``VAL_002`` if several nodes are running and none was
                named — 아무 노드나 고르면 엉뚱한 에이전트의 방향이 바뀝니다 — or the named
                node has several tasks in flight (노드 이름으로 가를 수 없습니다).
                The agent's own refusals pass through unchanged.
        """
        running = [
            task
            for task in self.inflight.get(run_id, {}).values()
            if node is None or task.node_id == node
        ]
        if not running:
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND,
                code=ErrorCode.NF_001,
                message="run has no in-flight task to steer",
                details={"run_id": run_id, **({"node_id": node} if node else {})},
            )
        if len(running) > 1:
            raise _ambiguous(run_id, running, node=node)

        target = running[0]
        supported = await target.client.steer(target.task_id, text, by=by)
        log.info(
            "steer forwarded to agent",
            agent=target.agent,
            node_id=target.node_id,
            run_id=run_id,
            task_id=target.task_id,
            steered_by=by,
            text_chars=len(text),
            supported=supported,
        )
        return SteerReceipt(
            node_id=target.node_id,
            task_id=target.task_id,
            agent=target.agent,
            supported=supported,
        )

    async def cancel(self, node: NodeSpec, task_id: str) -> None:
        """Ask the node's agent to cancel a task the orchestrator stopped waiting for.

        orchestrator 가 먼저 끊은 태스크의 취소를 담당 에이전트에게 알립니다. 떠 있는
        컨테이너가 없으면 취소할 대상도 없습니다.

        Raises:
            AaestroError: On transport or non-2xx responses from the Control API.
        """
        if node.agent is None:
            return
        client = self.clients.get(agent_name(node.agent))
        if client is None:
            return
        await client.cancel(task_id)


def _ambiguous(run_id: str, running: list[InflightTask], *, node: str | None) -> AaestroError:
    """대상이 하나로 정해지지 않는 steer — 호출자가 풀 수 있는 것과 없는 것을 가른다."""
    nodes = sorted({task.node_id for task in running})
    if node is None and len(nodes) > 1:
        message = "several nodes are running, name the node to steer"
    else:
        # 노드를 지목해도 좁혀지지 않는다 — "노드를 지목하라" 고 답하면 풀 수 없는 안내가 된다
        message = "the node has several tasks in flight and cannot be steered by node"
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=message,
        details={
            "run_id": run_id,
            "running": nodes,
            "task_ids": sorted(task.task_id for task in running),
        },
    )


__all__ = ["ControlNodeRuntime", "InflightTask", "SteerReceipt"]
