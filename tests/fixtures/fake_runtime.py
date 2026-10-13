"""Fake node runtime for graph-level tests.

그래프 라우팅을 컨테이너 없이 검증하기 위한 runtime 대역.
스크립트된 출력을 노드별로 반환하며, 호출 순서를 기록한다.
"""

from __future__ import annotations

from typing import Any

from aaestro.core.agent import TaskRequest, TaskResult
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.topology import NodeSpec


class FakeRuntime:
    """노드 호출을 기록하고 스크립트된 결과를 반환하는 runtime 대역."""

    def __init__(self) -> None:
        self._outputs: dict[str, dict[str, Any]] = {}
        self._failures: dict[str, AaestroError] = {}
        self._fail_once: set[str] = set()
        self.invoked: list[str] = []
        self.tasks: list[TaskRequest] = []

    def script(self, node_id: str, *, output: dict[str, Any] | None = None) -> FakeRuntime:
        """노드의 성공 출력을 지정한다."""
        self._outputs[node_id] = output or {}
        return self

    def fail(
        self, node_id: str, *, error: AaestroError | None = None, once: bool = False
    ) -> FakeRuntime:
        """노드가 실패하도록 지정한다. ``once`` 면 첫 호출만 실패한다."""
        self._failures[node_id] = error or AaestroError(
            category=ErrorCategory.MODEL,
            code=ErrorCode.LLM_003,
            message="provider server error",
            retryable=True,
        )
        if once:
            self._fail_once.add(node_id)
        return self

    async def invoke(self, node: NodeSpec, task: TaskRequest) -> TaskResult:
        """노드 태스크를 실행한 것처럼 결과를 반환한다."""
        self.invoked.append(node.id)
        self.tasks.append(task)

        failure = self._failures.get(node.id)
        if failure is not None:
            if node.id in self._fail_once:
                del self._failures[node.id]
                self._fail_once.discard(node.id)
            return TaskResult.failed(task, failure)

        return TaskResult.completed(task, output=self._outputs.get(node.id, {}))
