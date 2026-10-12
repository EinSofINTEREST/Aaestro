"""Graph topology schema and deploy-time validation.

그래프 토폴로지 스키마와 배포 시점 검증. 그래프는 에이전트를 잇고 분리하는
배선 모듈이며, 연결 변경은 이 YAML 수정만으로 완료되어야 한다.

검증 실패는 전부 ``GRAPH_001`` 로 배포를 중단시킨다 — 잘못된 토폴로지로는
컨테이너를 기동하지 않는다.
"""

from __future__ import annotations

import importlib
from enum import StrEnum
from typing import TYPE_CHECKING, Annotated, Any, Literal, Protocol

import structlog
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from aaestro.core.conditions import Predicate, is_import_ref, parse_condition
from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    printable_label,
    repeated_positions,
)
from aaestro.core.manifest import AgentName, MemorySpec, ModelOverride, SemVer

if TYPE_CHECKING:
    from collections.abc import Mapping

    from aaestro.modules.registry import ModuleRegistry

START = "START"
END = "END"
RESERVED_NODE_IDS = frozenset({START, END})

DEFAULT_MAX_FAILURE_STREAK = 5
DEFAULT_IDLE_MIN_DELAY_S = 30.0
DEFAULT_IDLE_MAX_DELAY_S = 600.0

_IMPORT_REF_SEPARATOR = ":"

log = structlog.get_logger(__name__)


class GraphMode(StrEnum):
    """그래프 실행 모드.

    mission 은 목표 달성 후 종료하고, service 는 무한히 반복한다.
    """

    MISSION = "mission"
    SERVICE = "service"


def _topology_error(message: str, **details: Any) -> AaestroError:
    """토폴로지 검증 실패를 ``GRAPH_001`` 로 만든다."""
    return AaestroError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_001,
        message=message,
        details=details,
    )


class NodeSpec(BaseModel):
    """A graph node bound to an agent or a subgraph.

    그래프 노드. 에이전트 또는 서브그래프 중 정확히 하나를 참조한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    agent: str | None = None
    graph: str | None = None
    input_map: dict[str, Any] = Field(default_factory=dict)
    output_map: dict[str, str] = Field(default_factory=dict)
    retry: int = 0
    timeout_s: float | None = None
    model: ModelOverride | None = None
    """이 노드에서만 에이전트 manifest 의 ``spec.model`` 을 덮는 부분 선언."""

    @field_validator("retry")
    @classmethod
    def _non_negative_retry(cls, value: int) -> int:
        """음수 재시도는 의미가 없다."""
        if value < 0:
            raise ValueError("retry must be >= 0")
        return value

    @field_validator("timeout_s")
    @classmethod
    def _positive_timeout(cls, value: float | None) -> float | None:
        """0 이하 timeout 은 즉시 만료를 뜻해 노드를 실행할 수 없게 만든다."""
        if value is not None and value <= 0:
            raise ValueError("timeout_s must be > 0")
        return value

    @field_validator("id")
    @classmethod
    def _reject_reserved_ids(cls, value: str) -> str:
        """START/END 는 예약어이므로 노드 id 로 쓸 수 없다."""
        if value in RESERVED_NODE_IDS:
            raise ValueError("node id is reserved")
        if not value:
            raise ValueError("node id must not be empty")
        return value

    @model_validator(mode="after")
    def _exactly_one_target(self) -> NodeSpec:
        """노드는 에이전트 또는 서브그래프 중 하나만 참조한다."""
        if (self.agent is None) == (self.graph is None):
            raise ValueError("node requires exactly one of 'agent' or 'graph'")
        if self.graph is not None and self.model is not None:
            # 서브그래프 노드에는 모델을 고를 에이전트가 없다 — 적어도 아무 효과가 없다
            raise ValueError("subgraph node must not declare 'model'")
        return self

    @property
    def is_subgraph(self) -> bool:
        """서브그래프 노드인지."""
        return self.graph is not None

    @property
    def ref(self) -> str:
        """참조 문자열 — 에이전트 또는 그래프 ref."""
        return self.agent if self.agent is not None else str(self.graph)


class EdgeSpec(BaseModel):
    """A directed edge, optionally conditional.

    방향 간선. ``condition`` 이 있으면 조건부 라우팅이다 — 선언식(``state.approved``) 이거나,
    deprecated 인 조건 함수 import ref(``aaestro.graphs.conditions:draft_approved``) 다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    source: str = Field(alias="from")
    target: str = Field(alias="to")
    condition: str | None = None
    max_iterations: int | None = None

    @field_validator("condition")
    @classmethod
    def _parsable_condition(cls, value: str | None) -> str | None:
        """선언식은 읽는 시점에 문법을 본다 — import ref 의 해석은 배포 검증이 한다."""
        if value is not None and not is_import_ref(value):
            try:
                parse_condition(value)
            except AaestroError as err:
                raise ValueError(err.message) from err
        return value

    @field_validator("max_iterations")
    @classmethod
    def _positive_iterations(cls, value: int | None) -> int | None:
        """0 이하는 의미가 없고, 음수는 truthy 라 상한 검사를 조용히 통과시킨다."""
        if value is not None and value < 1:
            raise ValueError("max_iterations must be >= 1")
        return value


class ConnectionSpec(BaseModel):
    """An allowlisted A2A peer call.

    A2A 호출 allowlist 항목. 방향은 선언의 문제이며 peer 간 우열은 없다 —
    역방향이 필요하면 별도로 선언한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    caller: str
    callee: str

    @model_validator(mode="after")
    def _reject_self_call(self) -> ConnectionSpec:
        """자기 자신에 대한 A2A 선언은 의미가 없다."""
        if self.caller == self.callee:
            raise ValueError("connection caller and callee must differ")
        return self


class IdlePolicy(BaseModel):
    """Exponential backoff while a service graph has no work.

    Service 그래프의 idle 정책. busy-loop 로 모델 호출을 낭비하지 않도록
    작업이 없으면 지수 백오프한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_delay_s: float = DEFAULT_IDLE_MIN_DELAY_S
    max_delay_s: float = DEFAULT_IDLE_MAX_DELAY_S
    multiplier: float = 2.0

    @model_validator(mode="after")
    def _check_bounds(self) -> IdlePolicy:
        """상한이 하한보다 작으면 백오프가 성립하지 않는다."""
        if self.min_delay_s <= 0:
            raise ValueError("idle min_delay_s must be positive")
        if self.max_delay_s < self.min_delay_s:
            raise ValueError("idle max_delay_s must be >= min_delay_s")
        if self.multiplier <= 1:
            raise ValueError("idle multiplier must be > 1")
        return self

    def delay_for(self, streak: int) -> float:
        """Compute the idle delay after ``streak`` consecutive idle iterations.

        연속 idle 횟수에 대한 대기 시간을 계산합니다. 0 이면 첫 idle 이므로
        ``min_delay_s`` 를 반환하고, 상한에서 고정됩니다.
        """
        if streak < 0:
            raise ValueError("idle streak must be >= 0")
        delay = self.min_delay_s * (self.multiplier**streak)
        return min(delay, self.max_delay_s)


class ServiceSpec(BaseModel):
    """Service-mode settings.

    상주형 실행 설정. idle 정책은 필수이며, 연속 실패 임계를 넘으면 정지한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    idle: IdlePolicy
    max_failure_streak: int = DEFAULT_MAX_FAILURE_STREAK

    @field_validator("max_failure_streak")
    @classmethod
    def _positive_streak(cls, value: int) -> int:
        """0 이면 첫 실패에 정지하므로 crash loop 방지 의미가 없다."""
        if value < 1:
            raise ValueError("max_failure_streak must be >= 1")
        return value


StateFieldType = Literal["string", "integer", "number", "boolean", "array", "object"]


class StateField(BaseModel):
    """One state field declared inline in the graph.

    그래프 YAML 안에 선언한 state 필드 하나 — promptset 변수 선언과 같은 어휘다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: StateFieldType
    required: bool = False
    default: Any = None
    description: str | None = None

    @model_validator(mode="after")
    def _required_has_no_default(self) -> StateField:
        """기본값이 있는 필수 필드는 필수가 아니다 — 둘 중 무엇을 뜻했는지 모호하다."""
        if self.required and self.default is not None:
            raise ValueError("a required state field cannot declare a default")
        return self


class StateSpec(BaseModel):
    """Graph state schema binding.

    그래프 state 스키마 바인딩 — 둘 중 하나로 선언한다:

    - ``fields``: 그래프 YAML 안의 필드 선언. ``src/`` 를 고치지 않고 새 그래프를 만든다 (#316)
    - ``schema``: ``aaestro.graphs`` 의 pydantic 모델 import ref (deprecated — 한 버전 유지)
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    schema_ref: str | None = Field(default=None, alias="schema")
    declared_fields: dict[str, StateField] | None = Field(default=None, alias="fields")
    checkpointer: str = "default"

    @model_validator(mode="after")
    def _exactly_one_schema(self) -> StateSpec:
        if (self.schema_ref is None) == (self.declared_fields is None):
            raise ValueError("state declares exactly one of 'fields' or 'schema'")
        for name in self.declared_fields or {}:
            # _run_id / _iterations 같은 예약 채널과 겹치면 프레임워크 값이 덮인다
            if not name.isidentifier() or name.startswith("_"):
                # 필드 이름은 매핑의 키다 — 키는 구조라 내보낸다 (길이 · 문자는 정제한다)
                raise ValueError(
                    f"state field name '{printable_label(name)}' must be an identifier "
                    "not starting with '_'"
                )
        return self


class GraphMetadata(BaseModel):
    """Graph identity.

    그래프 식별 정보. 토폴로지 변경 시 version 을 bump 한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: AgentName
    version: SemVer
    description: str | None = None


class GraphSpec(BaseModel):
    """The graph wiring body.

    그래프 배선 본문. 노드 추가/제거와 edge 연결/분리가 전부 여기서 이루어진다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: GraphMode = GraphMode.MISSION
    goal: str
    state: StateSpec
    nodes: tuple[NodeSpec, ...]
    edges: tuple[EdgeSpec, ...]
    connections: tuple[ConnectionSpec, ...] = ()
    service: ServiceSpec | None = None
    memory: MemorySpec = Field(default_factory=MemorySpec)

    @field_validator("nodes")
    @classmethod
    def _unique_node_ids(cls, value: tuple[NodeSpec, ...]) -> tuple[NodeSpec, ...]:
        """노드 id 중복 금지 — 라우팅이 모호해진다."""
        if not value:
            raise ValueError("graph must declare at least one node")
        repeated = repeated_positions([node.id for node in value])
        if repeated:
            raise ValueError(f"duplicate node id at index {repeated}")
        return value

    @model_validator(mode="after")
    def _check_mode_requirements(self) -> GraphSpec:
        """모드별 필수 선언을 확인한다."""
        if self.mode is GraphMode.SERVICE and self.service is None:
            raise ValueError("service mode requires 'service.idle' policy")
        if self.mode is GraphMode.MISSION and self.service is not None:
            raise ValueError("'service' settings are only valid in service mode")
        return self


class GraphTopology(BaseModel):
    """A deployable graph module.

    배포 가능한 그래프 모듈. 배포 시 :func:`validate_topology` 로 검증된 뒤에만
    컨테이너가 기동된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Graph"]
    metadata: GraphMetadata
    spec: GraphSpec

    @property
    def name(self) -> str:
        """그래프 이름."""
        return self.metadata.name

    @property
    def mode(self) -> GraphMode:
        """실행 모드."""
        return self.spec.mode

    @property
    def node_ids(self) -> frozenset[str]:
        """선언된 노드 id 집합."""
        return frozenset(n.id for n in self.spec.nodes)

    def with_model_overrides(self, overrides: Mapping[str, ModelOverride]) -> GraphTopology:
        """Layer deployment model overrides onto agent nodes.

        배포 계층의 모델 override 를 노드에 입힌 새 토폴로지를 돌려줍니다.
        노드에 이미 override 가 있으면 그 위에 덮습니다 (배포가 노드를 이긴다).

        Args:
            overrides: Node id to the override for that node.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` if a key is not an agent node of this graph.
        """
        if not overrides:
            return self
        agent_nodes = {n.id for n in self.spec.nodes if not n.is_subgraph}
        unknown = sorted(set(overrides) - agent_nodes)
        if unknown:
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="model override names a node this graph does not have",
                details={"graph": self.name, "node_ids": unknown},
            )
        nodes = tuple(
            node
            if node.id not in overrides
            else node.model_copy(
                update={
                    "model": overrides[node.id]
                    if node.model is None
                    else node.model.merged(overrides[node.id])
                }
            )
            for node in self.spec.nodes
        )
        return self.model_copy(update={"spec": self.spec.model_copy(update={"nodes": nodes})})

    def node(self, node_id: str) -> NodeSpec:
        """Look up a node by id.

        id 로 노드를 찾습니다.

        Raises:
            KeyError: If no node with that id is declared.
        """
        for candidate in self.spec.nodes:
            if candidate.id == node_id:
                return candidate
        raise KeyError(node_id)


ImportRef = Annotated[str, Field(pattern=r"^[\w.]+:[\w.]+$")]
"""Importable reference — ``module.path:attribute``."""


IMPORTABLE_PACKAGE = "aaestro.graphs"
"""그래프 선언이 import 할 수 있는 유일한 패키지 — state 모델과 조건 함수를 두는 곳."""


def _importable(module_path: str) -> bool:
    return module_path == IMPORTABLE_PACKAGE or module_path.startswith(f"{IMPORTABLE_PACKAGE}.")


def resolve_import_ref(ref: str) -> Any:
    """Import an object from a ``module:attribute`` reference.

    ``module:attribute`` 형식의 참조를 import 합니다.

    Args:
        ref: Importable reference such as ``aaestro.graphs.conditions:needs_research``.

    Returns:
        The imported attribute.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` if the reference cannot be imported.
    """
    if _IMPORT_REF_SEPARATOR not in ref:
        raise _topology_error(f"invalid import ref: {ref}", ref=ref)

    module_path, _, attribute = ref.partition(_IMPORT_REF_SEPARATOR)
    if not _importable(module_path):
        # 검증 단계에서 import 하는 순간 모듈 최상위 코드가 돈다 — 저장된 YAML 문자열 하나로
        # 임의 모듈을 실행시킬 수 없게, 그래프 계약을 두는 패키지만 연다 (#316)
        raise _topology_error(
            f"import ref outside {IMPORTABLE_PACKAGE}: {ref}", ref=ref, allowed=IMPORTABLE_PACKAGE
        )
    try:
        module = importlib.import_module(module_path)
    except (ImportError, ValueError) as err:
        # 빈 모듈명(":attr") 은 ValueError 를 내므로 함께 감싼다 —
        # 토폴로지 검증 실패는 예외 종류와 무관하게 GRAPH_001 이어야 한다
        raise _topology_error(f"cannot import module for ref: {ref}", ref=ref) from err

    try:
        return getattr(module, attribute)
    except AttributeError as err:
        raise _topology_error(f"attribute not found for ref: {ref}", ref=ref) from err


def _check_edge_endpoints(topology: GraphTopology) -> None:
    """dangling edge 검출 — from/to 가 노드 또는 START/END 여야 한다."""
    valid = topology.node_ids | RESERVED_NODE_IDS
    for edge in topology.spec.edges:
        for role, endpoint in (("from", edge.source), ("to", edge.target)):
            if endpoint not in valid:
                raise _topology_error(
                    f"dangling edge {role}: {endpoint}",
                    graph=topology.name,
                    edge=f"{edge.source}->{edge.target}",
                )
        if edge.source == END:
            raise _topology_error(
                "END must not have outgoing edges",
                graph=topology.name,
                edge=f"{edge.source}->{edge.target}",
            )
        if edge.target == START:
            raise _topology_error(
                "START must not have incoming edges",
                graph=topology.name,
                edge=f"{edge.source}->{edge.target}",
            )


def _adjacency(topology: GraphTopology) -> dict[str, set[str]]:
    """노드 id → 도달 가능한 다음 노드 집합."""
    graph: dict[str, set[str]] = {node_id: set() for node_id in topology.node_ids}
    graph[START] = set()
    graph[END] = set()
    for edge in topology.spec.edges:
        graph[edge.source].add(edge.target)
    return graph


def _reachable_from_start(topology: GraphTopology) -> set[str]:
    """START 에서 도달 가능한 노드 집합 (BFS)."""
    graph = _adjacency(topology)
    seen: set[str] = set()
    frontier = [START]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(graph.get(current, set()) - seen)
    return seen


def _check_reachability(topology: GraphTopology) -> None:
    """START 에서 모든 노드에 도달 가능해야 한다."""
    reachable = _reachable_from_start(topology)
    unreachable = topology.node_ids - reachable
    if unreachable:
        raise _topology_error(
            f"nodes unreachable from START: {sorted(unreachable)}",
            graph=topology.name,
        )


def _reaches(graph: dict[str, set[str]], source: str, target: str) -> bool:
    """``source`` 에서 ``target`` 으로 도달 가능한지 (BFS)."""
    seen: set[str] = set()
    frontier = [source]
    while frontier:
        current = frontier.pop()
        if current == target:
            return True
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(graph.get(current, set()) - seen)
    return False


def _cycle_edges(topology: GraphTopology) -> list[EdgeSpec]:
    """순환에 놓인 edge 목록 — target 에서 source 로 되돌아올 수 있는 edge."""
    graph = _adjacency(topology)
    return [e for e in topology.spec.edges if _reaches(graph, e.target, e.source)]


def _check_mode_topology(topology: GraphTopology) -> None:
    """모드별 토폴로지 규칙 — 두 모드 모두 END 도달과 순환 상한을 요구한다.

    Service 그래프의 "무한 반복" 은 그래프 안의 순환이 아니라 ``ServiceRunner`` 의
    iteration 루프가 담당한다. 그래프가 스스로 순환하면 한 번의 실행이 끝나지 않아
    iteration 경계 자체가 성립하지 않는다 — 그래서 END 요건은 모드와 무관하다.
    """
    if END not in _reachable_from_start(topology):
        raise _topology_error(
            f"{topology.spec.mode.value} graph must be able to reach END",
            graph=topology.name,
        )
    # 상한은 순환에 놓인 edge 에 있어야 한다 — 무관한 edge 에 붙은 값은
    # 실제 순환을 전혀 제한하지 못한 채 검사만 통과시킨다
    cycle_edges = _cycle_edges(topology)
    if cycle_edges and not any(e.max_iterations for e in cycle_edges):
        raise _topology_error(
            f"{topology.spec.mode.value} graph with a cycle requires "
            "'max_iterations' on a cycle edge",
            graph=topology.name,
        )


def _check_fan_out(topology: GraphTopology) -> None:
    """조건 없는 out-edge 는 노드당 하나 — 노드는 한 번에 한 길로만 나간다.

    - 조건 edge 가 없는 노드: 조건 없는 edge 가 둘이면 LangGraph 는 두 노드를 같은 superstep
      에서 병렬로 돌린다. 두 branch 가 같은 state 키(최소한 ``_iterations``)를 쓰는데 채널에
      병합 규칙이 없어 run 이 ``InvalidUpdateError`` 로 죽는다 (#313)
    - 조건 edge 가 있는 노드: 조건 없는 edge 는 기본 경로이고 라우터는 **첫 번째**만 쓴다 —
      두 번째는 조용히 버려진다

    어느 쪽이든 검증은 통과하고 run 에서야 드러나던 것을 여기로 당긴다.
    """
    unconditional: dict[str, list[str]] = {}
    for edge in topology.spec.edges:
        if edge.condition is None:
            unconditional.setdefault(edge.source, []).append(edge.target)
    for source, targets in unconditional.items():
        if len(targets) > 1:
            raise _topology_error(
                f"node has {len(targets)} edges without conditions — a node leaves by one "
                "path; parallel branches are not supported; give the edges conditions",
                graph=topology.name,
                node_id=source,
                targets=targets,
            )


def _check_connections(topology: GraphTopology) -> None:
    """A2A allowlist 의 caller/callee 가 모두 그래프 노드여야 한다."""
    for connection in topology.spec.connections:
        for role, node_id in (("caller", connection.caller), ("callee", connection.callee)):
            if node_id not in topology.node_ids:
                raise _topology_error(
                    f"connection {role} is not a graph node: {node_id}",
                    graph=topology.name,
                )


def resolve_condition(condition: str) -> Predicate:
    """Turn an edge condition into a predicate over the graph state.

    edge 조건을 state 판정 함수로 만듭니다 — 선언식이면 파싱하고, import ref 면 import 합니다.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` if it can be neither parsed nor imported.
    """
    if is_import_ref(condition):
        function: Predicate = resolve_import_ref(condition)
        return function
    return parse_condition(condition)


def _check_conditions(topology: GraphTopology) -> None:
    """conditional edge 의 조건이 판정 함수가 되어야 한다 — import ref 는 deprecated 로 알린다."""
    for edge in topology.spec.edges:
        if edge.condition is None:
            continue
        resolve_condition(edge.condition)
        if is_import_ref(edge.condition):
            log.warning(
                "graph condition import ref is deprecated",
                graph=topology.name,
                edge=f"{edge.source}->{edge.target}",
                condition=edge.condition,
                replacement="a declarative condition such as state.<field>",
            )


def _check_input_maps(topology: GraphTopology, state_fields: frozenset[str]) -> None:
    """input_map 이 참조하는 state 키가 schema 에 존재해야 한다."""
    for node in topology.spec.nodes:
        for target_key, source in node.input_map.items():
            if not (isinstance(source, str) and source.startswith("state.")):
                continue
            field = source.removeprefix("state.").split(".", 1)[0]
            if field not in state_fields:
                raise _topology_error(
                    f"input_map references unknown state field: {source}",
                    graph=topology.name,
                    node_id=node.id,
                    details_key=target_key,
                )


def _check_refs(topology: GraphTopology, registry: ModuleRegistry) -> None:
    """노드가 참조하는 에이전트/서브그래프 ref 가 해석 가능해야 한다."""
    for node in topology.spec.nodes:
        try:
            registry.resolve(node.ref)
        except AaestroError as err:
            raise _topology_error(
                f"cannot resolve node ref: {node.ref}",
                graph=topology.name,
                node_id=node.id,
                module_ref=node.ref,
            ) from err


class SubgraphLoader(Protocol):
    """Resolves a graph ref to its topology.

    그래프 ref 를 토폴로지로 해석하는 계약 — 서브그래프 순환 검사에 사용한다.
    """

    def __call__(self, ref: str) -> GraphTopology:
        """참조를 해석해 토폴로지를 반환한다."""
        ...


def _check_subgraph_cycles(
    topology: GraphTopology,
    load: SubgraphLoader,
    seen: tuple[str, ...],
) -> None:
    """서브그래프 순환 참조를 차단한다."""
    for node in topology.spec.nodes:
        if not node.is_subgraph:
            continue
        ref = node.ref
        if ref in seen:
            raise _topology_error(
                f"subgraph cycle detected: {' -> '.join([*seen, ref])}",
                graph=topology.name,
                node_id=node.id,
            )
        child = load(ref)
        _check_subgraph_cycles(child, load, (*seen, ref))


def validate_topology(
    topology: GraphTopology,
    *,
    registry: ModuleRegistry | None = None,
    state_fields: frozenset[str] | None = None,
    load_subgraph: SubgraphLoader | None = None,
) -> None:
    """Validate a graph topology at deploy time.

    배포 시점에 그래프 토폴로지를 검증합니다. 하나라도 실패하면 배포를 중단해야
    하므로 첫 위반에서 즉시 ``GRAPH_001`` 을 발생시킵니다.

    Args:
        topology: The topology to validate.
        registry: Module registry used to resolve node refs; skipped when omitted.
        state_fields: Field names of the graph state schema; skipped when omitted.
        load_subgraph: Callable resolving a graph ref to a topology, for cycle checks.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` on the first rule violation.
    """
    _check_edge_endpoints(topology)
    _check_reachability(topology)
    _check_mode_topology(topology)
    _check_fan_out(topology)
    _check_connections(topology)
    _check_conditions(topology)

    if state_fields is not None:
        _check_input_maps(topology, state_fields)
    if registry is not None:
        _check_refs(topology, registry)
    if load_subgraph is not None:
        _check_subgraph_cycles(topology, load_subgraph, (topology.name,))
