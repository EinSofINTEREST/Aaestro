"""Applies graph state compaction at a run boundary.

그래프 선언의 압축 정책을 **경계에서** 적용한다 — service 는 iteration 사이 (끝난
iteration 의 state 로 다음 iteration 의 입력을 만들 때), mission 은 순환 edge 로 노드에
다시 들어갈 때. 노드 실행 도중이나 매 노드마다 하지 않는다: 같은 state 를 여러 번 다시
쓰면 그것을 읽는 프롬프트의 앞부분이 매번 바뀐다.

실패는 state 를 건드리지 않는다. 요약 호출이든 원문 보존이든 실패하면 그 필드는
그대로 두고 다음 경계에서 다시 시도한다 — 압축은 run 을 실패시킬 이유가 아니다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from aaestro.core.agent import CONTROL_OVERHEAD_S
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.compaction import (
    SummarizePolicy,
    apply_deterministic,
    has_summary_shape,
    split_for_summary,
    summary_entry,
)
from aaestro.orchestrator.compaction_boundary import Boundary
from aaestro.orchestrator.compaction_ledger import CompactionLedger
from aaestro.orchestrator.compaction_schema import compaction_problems
from aaestro.orchestrator.state import resolve_graph_state
from aaestro.orchestrator.state_artifacts import (
    STATE_ARTIFACT_SETTING,
    artifact_key,
    encode_originals,
    is_own_reference,
)
from aaestro.orchestrator.summarizer import SUMMARY_TIMEOUT_S, StateSummarizer
from aaestro.orchestrator.telemetry import OrchestratorTelemetry
from aaestro.orchestrator.topology import NodeSpec

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.core.skill import ArtifactStore
    from aaestro.observability.metrics import Metrics
    from aaestro.orchestrator.builder import NodeRuntime
    from aaestro.orchestrator.compaction import CompactionPolicy
    from aaestro.orchestrator.topology import GraphTopology


def _compaction_error(code: ErrorCode, message: str, **details: Any) -> AaestroError:
    """압축 선언 · 실행의 문제를 GRAPH 카테고리로."""
    return AaestroError(category=ErrorCategory.GRAPH, code=code, message=message, details=details)


class StateCompactor:
    """Compacts the declared state fields of one graph.

    한 그래프의 압축 선언을 실행한다. ``compact`` 는 **바뀐 필드만** 돌려주고 예외를
    내지 않는다 (취소 제외) — 호출하는 경계가 그 결과를 state 에 병합한다.
    """

    def __init__(
        self,
        topology: GraphTopology,
        *,
        runtime: NodeRuntime,
        artifacts: ArtifactStore | None = None,
        metrics: Metrics | None = None,
        timeout_s: float = SUMMARY_TIMEOUT_S,
        control_overhead_s: float = CONTROL_OVERHEAD_S,
    ) -> None:
        self._graph = topology.name
        self._policies: dict[str, CompactionPolicy] = dict(topology.spec.state.compaction)
        self._artifacts = artifacts
        self._summarizer = StateSummarizer(
            runtime=runtime,
            graph=topology.name,
            # 노드 timeout 을 더 짧게 준 배포에서는 그것을 넘지 않는다
            timeout_s=min(timeout_s, SUMMARY_TIMEOUT_S),
            control_overhead_s=control_overhead_s,
        )
        self._summarizers = self._summarizer_nodes(topology)
        self._ledger = CompactionLedger(
            graph=topology.name,
            telemetry=(
                None
                if metrics is None
                else OrchestratorTelemetry(metrics, graph=topology.name, mode=topology.mode)
            ),
        )
        self._reject_unsound_declaration(topology)

    def _summarizer_nodes(self, topology: GraphTopology) -> dict[str, NodeSpec]:
        """summarize 필드 → 요약 호출에 쓸 노드 — 템플릿은 ``node_id`` 로 고른다 (02)."""
        nodes: dict[str, NodeSpec] = {}
        for name, policy in self._policies.items():
            if not isinstance(policy, SummarizePolicy):
                continue
            try:
                nodes[name] = NodeSpec(id=policy.template, agent=policy.compactor)
            except ValidationError as err:
                # 선언 값은 싣지 않는다 — 어느 필드가 왜 틀렸는지만
                raise _compaction_error(
                    ErrorCode.GRAPH_001,
                    "summarize compaction does not name a usable template",
                    graph=topology.name,
                    field=name,
                    problem=err.errors()[0]["msg"],
                ) from None
        return nodes

    def _reject_unsound_declaration(self, topology: GraphTopology) -> None:
        """잘못된 선언으로는 run 을 시작하지 않는다 — 경계마다 조용히 실패하는 것보다 낫다."""
        schema = resolve_graph_state(topology.spec.state, graph=topology.name)
        problems = compaction_problems(self._policies, schema)
        if problems:
            raise _compaction_error(ErrorCode.GRAPH_001, problems[0], graph=topology.name)
        if self._summarizers and self._artifacts is None:
            # 원문을 둘 곳이 없으면 요약은 원문을 잃는다
            raise _compaction_error(
                ErrorCode.GRAPH_001,
                "summarize compaction needs somewhere to keep the replaced originals — "
                f"set {STATE_ARTIFACT_SETTING}",
                graph=topology.name,
                fields=sorted(self._summarizers),
            )

    async def compact(self, state: Mapping[str, Any], boundary: Boundary) -> dict[str, Any]:
        """Compact every declared field once, at this boundary.

        선언된 필드를 이 경계에서 한 번씩 압축합니다.

        Args:
            state: The graph state at the boundary — 읽기만 합니다.
            boundary: Where in the run this is happening.

        Returns:
            The fields that changed, with their compacted lists. A field whose
            compaction failed is absent — state 는 그대로 남고 다음 경계에서 다시 시도합니다.
        """
        updates: dict[str, Any] = {}
        for name, policy in self._policies.items():
            items = state.get(name)
            if not isinstance(items, list):
                continue  # 아직 아무 노드도 채우지 않은 필드다
            if self._ledger.backing_off(name):
                self._ledger.size(name, items)
                continue
            try:
                compacted = await self._apply(name, policy, items, boundary)
            except Exception as err:
                # 경계의 최상위 수습 — 압축 실패가 방금 끝난 iteration 의 결과를 버리게 두지 않는다.
                # 취소(CancelledError)는 Exception 이 아니라 그대로 전파된다
                self._ledger.failed(name, policy, boundary, err)
                compacted = None
            else:
                if compacted is not None:
                    updates[name] = compacted
                    self._ledger.succeeded(name, policy, boundary, before=items, after=compacted)
            self._ledger.size(name, items if compacted is None else compacted)
        return updates

    async def _apply(
        self, name: str, policy: CompactionPolicy, items: list[Any], boundary: Boundary
    ) -> list[Any] | None:
        """필드 하나에 정책을 적용한다 — 바뀌지 않았으면 None."""
        if not isinstance(policy, SummarizePolicy):
            compacted = apply_deterministic(policy, items)
            # 세 전략 모두 항목을 빼기만 한다 — 길이가 같으면 이미 정책을 만족한 것이다
            return None if len(compacted) == len(items) else compacted

        prior = self._has_prior_summary(name, items, boundary)
        fold, keep = split_for_summary(policy, items, prior_summary=prior)
        if not fold:
            return None
        if self._artifacts is None:  # pragma: no cover - 생성자가 이미 거부한다
            raise _compaction_error(ErrorCode.GRAPH_001, "no artifact store for originals")
        # 원문 인코딩을 먼저 만든다 — 직렬화할 수 없는 항목은 요약 비용을 내기 전에 드러나야 한다
        data = encode_originals(
            fold,
            graph=self._graph,
            run_id=boundary.artifact_run,
            field=name,
            boundary=boundary.label,
        )
        summary = await self._summarizer.summarize(
            self._summarizers[name], fold, run_id=boundary.run_id, trace_id=boundary.trace_id
        )
        # 원문을 먼저 남긴다 — 저장이 실패하면 여기서 예외로 빠져 state 는 치환되지 않는다
        key = artifact_key(self._graph, boundary.artifact_run, name, boundary.label)
        ref = await self._artifacts.put(key, data)
        return [summary_entry(summary, artifact_ref=ref, replaced=len(fold)), *keep]

    def _has_prior_summary(self, name: str, items: list[Any], boundary: Boundary) -> bool:
        """맨 앞 항목이 이 필드의 앞선 압축이 남긴 요약인지.

        요약으로 대접하는 것은 **맨 앞 한 건** 이고, 그 참조가 이 run · 이 필드의 key 일
        때뿐이다. 모양만 같은 항목은 에이전트 산출물이다 — 원문으로 세어 접는다. 그러지
        않으면 그런 항목을 쌓는 것만으로 필드가 영영 접히지 않는다.
        """
        if not items or not has_summary_shape(items[0]):
            return False
        return is_own_reference(
            items[0].get("artifact_ref"),
            graph=self._graph,
            run_id=boundary.artifact_run,
            field=name,
        )


def build_state_compactor(
    topology: GraphTopology,
    *,
    runtime: NodeRuntime,
    artifacts: ArtifactStore | None = None,
    metrics: Metrics | None = None,
    timeout_s: float = SUMMARY_TIMEOUT_S,
) -> StateCompactor | None:
    """Build the compactor for a graph, or nothing when it declares no compaction.

    그래프의 압축기를 만듭니다. 선언이 없으면 None — 그 그래프는 압축 경로에 들어가지
    않고 변경 전과 똑같이 돕니다.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` if the declaration does not fit the state
            schema, or ``summarize`` is declared without an artifact store.
    """
    if not topology.spec.state.compaction:
        return None
    return StateCompactor(
        topology, runtime=runtime, artifacts=artifacts, metrics=metrics, timeout_s=timeout_s
    )


__all__ = [
    "Boundary",
    "StateCompactor",
    "build_state_compactor",
]
