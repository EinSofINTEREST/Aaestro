"""Deploy-time contract validation.

배포 시점 계약 검증. **하나라도 실패하면 컨테이너를 기동하지 않는다**
(01 Contract Validation at Deploy Time).

검증 결과는 항목별로 모아 한 번에 보고한다 — 첫 실패에서 멈추면 운영자가
고칠 때마다 처음부터 다시 돌려야 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from pydantic import ValidationError

from aaestro.access.baselines import PROVIDER_HOSTS, EgressBaseline
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import RESERVED_GLOBAL_GROUP, agent_name, agent_ref_parts
from aaestro.modules.compatibility import check_promptset_templates
from aaestro.modules.promptset import LoadedPromptset
from aaestro.orchestrator.compaction import (
    SUMMARIZE_INPUT_KEY,
    SUMMARIZE_OUTPUT_KEY,
    SummarizePolicy,
)
from aaestro.orchestrator.compaction_schema import compaction_problems, list_fields
from aaestro.orchestrator.state import resolve_graph_state
from aaestro.orchestrator.state_artifacts import STATE_ARTIFACT_SETTING
from aaestro.orchestrator.topology import GraphMode, validate_topology
from aaestro.runtime.quota import check_group_quota, check_host_capacity

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from aaestro.core.manifest import AgentManifest, GroupManifest
    from aaestro.modules.promptset import PromptsetManifest
    from aaestro.orchestrator.topology import GraphTopology, NodeSpec

log = structlog.get_logger(__name__)


def _nodes_by_agent(topology: GraphTopology) -> dict[str, list[NodeSpec]]:
    """에이전트 이름 → 그 에이전트가 맡은 노드들. 한 에이전트가 여러 노드를 맡을 수 있다."""
    grouped: dict[str, list[NodeSpec]] = {}
    for node in topology.spec.nodes:
        if node.agent is None:
            continue
        grouped.setdefault(agent_name(node.agent), []).append(node)
    return grouped


_STATE_REF_PREFIX = "state."


def _accumulating_fields(topology: GraphTopology) -> frozenset[str]:
    """한 노드가 읽고 다시 쓰는 state 필드 — iteration 을 넘어 쌓일 수 있는 것들."""
    accumulating: set[str] = set()
    for node in topology.spec.nodes:
        read = {
            source.removeprefix(_STATE_REF_PREFIX).split(".", 1)[0]
            for source in node.input_map.values()
            if isinstance(source, str) and source.startswith(_STATE_REF_PREFIX)
        }
        accumulating.update(read & set(node.output_map))
    return frozenset(accumulating)


@dataclass(frozen=True)
class Finding:
    """One failed contract check.

    실패한 검증 하나. ``code`` 로 어느 카테고리의 문제인지 즉시 알 수 있다.
    """

    check: str
    code: ErrorCode
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidationReport:
    """The outcome of every deploy check.

    전체 검증 결과. 통과 여부와 함께 **모든** 실패 항목을 담는다.
    """

    findings: tuple[Finding, ...] = ()
    warnings: tuple[Finding, ...] = ()
    """배포를 막지 않는 지적 — ``ok`` 는 이것을 보지 않는다 (#371)."""

    @property
    def ok(self) -> bool:
        """전부 통과했는지."""
        return not self.findings

    def codes(self) -> tuple[ErrorCode, ...]:
        """실패한 항목의 에러 코드."""
        return tuple(f.code for f in self.findings)

    def checks(self) -> tuple[str, ...]:
        """실패한 검증 이름."""
        return tuple(f.check for f in self.findings)

    def raise_if_failed(self) -> None:
        """Abort deployment when any check failed.

        하나라도 실패했으면 배포를 중단합니다 — 미검증 상태로 컨테이너를
        기동하지 않습니다.

        Raises:
            AaestroError: CONFIG/``CFG_001`` carrying every finding.
        """
        if self.ok:
            return
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="deployment validation failed",
            details={
                "failures": [
                    {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
                    for f in self.findings
                ]
            },
        )


@dataclass
class DeployValidator:
    """Runs every deploy-time contract check.

    배포 시점 계약 검증 전체를 수행한다. 각 검증은 독립적으로 실행되어
    실패가 뒤의 검증을 가리지 않는다.

    Attributes:
        manifests: Agent name to its validated manifest.
        groups: Group name to its manifest.
        resolvable_refs: Module refs the registry can resolve.
        available_secrets: Secret keys resolvable per scope.
    """

    manifests: Mapping[str, AgentManifest]
    groups: Mapping[str, GroupManifest] = field(default_factory=dict)
    resolvable_refs: frozenset[str] = frozenset()
    promptsets: Mapping[str, PromptsetManifest] = field(default_factory=dict)
    """promptset ref → 선언. 노드 id 에 대응하는 템플릿과 그 필수 변수를 보는 데 쓴다.

    비어 있으면 그 검사는 아무 것도 보지 못한다 — 호출자가 반드시 채워야 하며,
    `Author.validate` 가 그 자리다 (#260).
    """
    local_secrets: Mapping[str, frozenset[str]] = field(default_factory=dict)
    global_secrets: frozenset[str] = frozenset()
    host_cpu_cores: float | None = None
    host_memory_bytes: int | None = None
    a2a_port_range: tuple[int, int] | None = None
    providers: frozenset[str] | None = None
    """이그레스 프록시 upstream 이름.

    None 이면 프록시가 없다 — 논리 호스트가 알려진 provider 면 통과한다.
    """
    state_artifacts: bool = False
    """state 압축 원문을 둘 저장소 (`orchestrator.state_artifact_root`) 가 설정됐는지.

    없으면 `summarize` 를 선언한 그래프를 거절한다 — 배포는 되고 run 시작에서야 거절되면
    컨테이너를 다 띄운 뒤에 안다 (#371).
    """

    def validate(self, topologies: Sequence[GraphTopology]) -> ValidationReport:
        """Run every contract check across the graphs being deployed.

        배포 대상 그래프 전체에 10항목 검증을 수행합니다 — 01 의 8항목에 노드 템플릿
        (#260) 과 모델 provider (#358) 검사를 더한 것입니다.

        Args:
            topologies: The graph topologies about to be deployed.

        Returns:
            The report — ``ok`` is False if any check failed.
        """
        findings: list[Finding] = []
        warnings: list[Finding] = []

        for topology in topologies:
            findings.extend(self._check_agent_refs(topology))
            findings.extend(self._check_connections(topology))
            findings.extend(self._check_mode_rules(topology))
            findings.extend(self._check_node_templates(topology))
            findings.extend(self._check_state_compaction(topology))
            warnings.extend(self._warn_unbounded_state(topology))

        findings.extend(self._check_module_refs())
        findings.extend(self._check_groups())
        findings.extend(self._check_env_allowlists())
        findings.extend(self._check_a2a_ports(topologies))
        findings.extend(self._check_quotas())
        findings.extend(self._check_model_providers(topologies))

        report = ValidationReport(findings=tuple(findings), warnings=tuple(warnings))
        if warnings:
            log.warning(
                "deployment validation raised warnings",
                graphs=len(topologies),
                warnings=len(warnings),
            )
        if report.ok:
            log.info("deployment validation passed", graphs=len(topologies))
        else:
            log.error(
                "deployment validation failed",
                graphs=len(topologies),
                failures=len(report.findings),
            )
        return report

    # --- 1. agent ref 해석 ------------------------------------------------

    def _check_agent_refs(self, topology: GraphTopology) -> list[Finding]:
        """그래프의 모든 agent ref 가 존재하는 manifest 를 가리키는지."""
        findings = []
        for node in topology.spec.nodes:
            if node.agent is None:
                continue
            name, version = agent_ref_parts(node.agent)
            manifest = self.manifests.get(name)
            if manifest is None:
                findings.append(
                    Finding(
                        check="agent_refs",
                        code=ErrorCode.MOD_001,
                        message=f"graph node references an unknown agent: {node.agent}",
                        details={
                            "graph": topology.metadata.name,
                            "node_id": node.id,
                            "module_ref": node.agent,
                        },
                    )
                )
                continue

            declared = manifest.metadata.version
            if version != declared:
                # 이름은 맞고 버전만 다른 경우를 따로 알린다 — "unknown agent" 로
                # 뭉개면 오타인지 버전 드리프트인지 구분되지 않는다
                findings.append(
                    Finding(
                        check="agent_refs",
                        code=ErrorCode.MOD_002,
                        message=(
                            f"graph node references agent version {version!r} "
                            f"but the manifest declares {declared!r}: {node.agent}"
                        ),
                        details={
                            "graph": topology.metadata.name,
                            "node_id": node.id,
                            "module_ref": node.agent,
                            "declared_version": declared,
                        },
                    )
                )
        return findings

    # --- 9. 노드 템플릿 · 입력 계약 -------------------------------------------

    def _check_node_templates(self, topology: GraphTopology) -> list[Finding]:
        """노드가 promptset 계약을 만족하는지 — 04 호환성 규칙 3 과 입력 계약.

        agentd 는 `task.node_id` 로 템플릿을 고르고 (없으면 `MOD_004`), 그 템플릿의
        필수 변수는 노드의 `input_map` 이 공급한다. 둘 중 하나라도 어긋나면
        **컨테이너를 다 띄운 뒤 run 에서** 죽는다 — 그것을 여기로 앞당긴다 (#260).
        """
        findings = []
        for bound_agent, nodes in _nodes_by_agent(topology).items():
            manifest = self.manifests.get(bound_agent)
            if manifest is None:
                continue  # agent_refs 가 이미 보고했다 — 같은 문제를 두 번 세지 않는다
            ref = manifest.spec.promptset.ref
            promptset = self.promptsets.get(ref)
            if promptset is None:
                continue  # module_refs 가 해석 실패를, Author 가 로드 실패를 보고한다
            try:
                # 규칙 3 의 판정은 이 헬퍼가 이미 구현한다 — 두 벌 두면 갈라진다.
                # `root` 는 이 규칙이 읽지 않으므로 선언만으로 감싼다. `accepts_direct`
                # 는 끈다: `default` 템플릿은 direct 요청의 계약이고, 그래프 run 을
                # 죽이는 것은 node_id 템플릿 쪽이다 (규칙 4 는 별개 문제)
                check_promptset_templates(
                    manifest,
                    LoadedPromptset(ref=ref, manifest=promptset, root=None),
                    [node.id for node in nodes],
                    accepts_direct=False,
                )
            except AaestroError as err:
                findings.append(
                    Finding(
                        check="node_templates",
                        # 헬퍼는 이 부류를 MOD_002 로 낸다 — 코드는 그대로 옮긴다
                        code=ErrorCode(err.code),
                        message=err.message,
                        details={
                            "graph": topology.metadata.name,
                            "agent": bound_agent,
                            **err.details,
                        },
                    )
                )
            # 템플릿이 있는 노드는 변수까지 본다 — 이름 문제 하나로 입력 문제가
            # 가려지면 고치고 다시 돌릴 때마다 새 문제가 하나씩 나온다
            findings.extend(self._check_node_inputs(topology, bound_agent, ref, promptset, nodes))
        return findings

    def _check_node_inputs(
        self,
        topology: GraphTopology,
        agent_name: str,
        ref: str,
        promptset: PromptsetManifest,
        nodes: Sequence[NodeSpec],
    ) -> list[Finding]:
        """노드의 `input_map` 이 템플릿의 필수 변수를 공급하는지."""
        findings = []
        for node in nodes:
            template = promptset.spec.templates.get(node.id)
            if template is None:
                continue  # 위에서 보고했다
            missing = sorted(
                variable
                for variable, spec in template.variables.items()
                if spec.required and variable not in node.input_map
            )
            if missing:
                findings.append(
                    Finding(
                        check="node_templates",
                        code=ErrorCode.MOD_004,
                        message=(
                            f"node {node.id!r} does not supply required template "
                            f"variables: {', '.join(missing)}"
                        ),
                        details={
                            "graph": topology.metadata.name,
                            "node_id": node.id,
                            "agent": agent_name,
                            "promptset": ref,
                            "missing": missing,
                        },
                    )
                )
        return findings

    # --- 11. state 압축 선언 ------------------------------------------------

    def _check_state_compaction(self, topology: GraphTopology) -> list[Finding]:
        """압축 선언이 state 스키마와 맞고, 요약을 쓸 에이전트와 템플릿이 실재하는지 (#371).

        어긋난 선언은 run 시작에서 거절되거나 경계마다 조용히 실패한다 — 그것을 배포로 앞당긴다.
        """
        policies = topology.spec.state.compaction
        if not policies:
            return []
        graph = topology.metadata.name
        try:
            schema = resolve_graph_state(topology.spec.state, graph=graph)
        except AaestroError as err:
            return [
                Finding(
                    check="state_compaction",
                    code=ErrorCode(err.code),
                    message=err.message,
                    details={"graph": graph, **err.details},
                )
            ]
        findings = [
            Finding(
                check="state_compaction",
                code=ErrorCode.GRAPH_001,
                message=problem,
                details={"graph": graph},
            )
            for problem in compaction_problems(policies, schema)
        ]
        summarized = sorted(n for n, p in policies.items() if isinstance(p, SummarizePolicy))
        if summarized and not self.state_artifacts:
            findings.append(
                Finding(
                    check="state_compaction",
                    code=ErrorCode.GRAPH_001,
                    message=(
                        "summarize compaction needs somewhere to keep the replaced originals — "
                        f"set {STATE_ARTIFACT_SETTING}"
                    ),
                    details={"graph": graph, "fields": summarized},
                )
            )
        for name, policy in policies.items():
            if isinstance(policy, SummarizePolicy):
                findings.extend(self._check_compactor(topology, name, policy))
        return findings

    def _check_compactor(
        self, topology: GraphTopology, state_field: str, policy: SummarizePolicy
    ) -> list[Finding]:
        """요약 에이전트가 이 그래프와 함께 뜨고, 그 promptset 이 요약 템플릿 계약을 갖는지."""
        details = {
            "graph": topology.metadata.name,
            "field": state_field,
            "module_ref": policy.compactor,
        }

        def finding(code: ErrorCode, message: str) -> list[Finding]:
            return [Finding(check="state_compaction", code=code, message=message, details=details)]

        name, version = agent_ref_parts(policy.compactor)
        manifest = self.manifests.get(name)
        if manifest is None:
            return finding(
                ErrorCode.MOD_001,
                f"state compactor references an unknown agent: {policy.compactor}",
            )
        if version != manifest.metadata.version:
            return finding(
                ErrorCode.MOD_002,
                f"state compactor references agent version {version!r} but the manifest "
                f"declares {manifest.metadata.version!r}: {policy.compactor}",
            )
        if policy.compactor not in {node.agent for node in topology.spec.nodes}:
            # 배포는 노드의 에이전트만 띄운다 — 노드가 아닌 에이전트는 요약할 때 떠 있지 않다
            return finding(
                ErrorCode.GRAPH_001,
                f"state compactor must be the agent of a node in this graph: {policy.compactor}",
            )
        promptset = self.promptsets.get(manifest.spec.promptset.ref)
        if promptset is None:
            return []  # module_refs 가 해석 실패를, Author 가 로드 실패를 보고한다
        template = promptset.spec.templates.get(policy.template)
        if template is None:
            # 노드 템플릿 검사가 같은 부류를 MOD_002 로 낸다 — 코드를 맞춘다
            return finding(
                ErrorCode.MOD_002,
                f"promptset does not declare the compaction template: {policy.template}",
            )
        required = {variable for variable, spec in template.variables.items() if spec.required}
        if (
            SUMMARIZE_INPUT_KEY not in template.variables
            or required - {SUMMARIZE_INPUT_KEY}
            or SUMMARIZE_OUTPUT_KEY not in template.output_keys
        ):
            return finding(
                ErrorCode.MOD_004,
                f"compaction template {policy.template!r} must take the variable "
                f"{SUMMARIZE_INPUT_KEY!r} (array) as its only required variable and declare "
                f"the output key {SUMMARIZE_OUTPUT_KEY!r}",
            )
        return []

    def _warn_unbounded_state(self, topology: GraphTopology) -> list[Finding]:
        """상주 그래프에서 자랄 수 있는 리스트 필드에 압축 선언이 없으면 경고한다 — 거절은 아니다.

        "자랄 수 있다" 는 **같은 노드가 읽고 쓰는** 리스트 필드다: 노드는 이전 값을 입력으로
        받아야 거기에 덧붙여 돌려줄 수 있다. 아무 노드도 쓰지 않는 필드 (run 입력) 와, 쓰는
        노드가 이전 값을 받지 않는 필드 (매 iteration 새로 만드는 값) 는 iteration 을 넘어
        쌓이지 않는다. 누적하는 필드에 선언이 없으면 state 가 프롬프트와 checkpoint 를 끝없이
        키운다.
        """
        if topology.spec.mode is not GraphMode.SERVICE:
            return []
        graph = topology.metadata.name
        try:
            schema = resolve_graph_state(topology.spec.state, graph=graph)
        except AaestroError:
            return []  # 스키마를 읽지 못하는 문제는 경고가 아니라 다른 검사의 실패다
        unbounded = sorted(
            (list_fields(schema) & _accumulating_fields(topology))
            - set(topology.spec.state.compaction)
        )
        if not unbounded:
            return []
        return [
            Finding(
                check="state_compaction",
                code=ErrorCode.GRAPH_001,
                message=(
                    "service graph accumulates list state fields without a compaction policy: "
                    f"{', '.join(unbounded)}"
                ),
                details={"graph": graph, "fields": unbounded},
            )
        ]

    # --- 2. 모듈 ref 해석 -------------------------------------------------

    def _check_module_refs(self) -> list[Finding]:
        """manifest 가 선언한 모든 모듈 ref 가 registry 에서 해석되는지."""
        findings = []
        for name, manifest in self.manifests.items():
            refs = [manifest.spec.promptset.ref]
            refs.extend(s.ref for s in manifest.spec.skillsets)
            refs.extend(s.ref for s in manifest.spec.memory.spaces)

            for ref in refs:
                if ref not in self.resolvable_refs:
                    findings.append(
                        Finding(
                            check="module_refs",
                            code=ErrorCode.MOD_001,
                            message=f"module ref cannot be resolved: {ref}",
                            details={"agent": name, "module_ref": ref},
                        )
                    )
        return findings

    # --- 3. 그룹 소속 -----------------------------------------------------

    def _check_groups(self) -> list[Finding]:
        """선언된 그룹이 존재하는지, 예약 그룹을 직접 선언하지 않았는지."""
        findings = []
        for name, manifest in self.manifests.items():
            declared = manifest.metadata.group
            if declared is None:
                continue
            if declared == RESERVED_GLOBAL_GROUP:
                findings.append(
                    Finding(
                        check="groups",
                        code=ErrorCode.CFG_002,
                        message="agents must not declare the reserved global group",
                        details={"agent": name, "group": declared},
                    )
                )
                continue
            if declared not in self.groups:
                findings.append(
                    Finding(
                        check="groups",
                        code=ErrorCode.CFG_002,
                        message=f"agent belongs to an unknown group: {declared}",
                        details={"agent": name, "group": declared},
                    )
                )
        return findings

    # --- 4. env_allowlist 해석 --------------------------------------------

    def _check_env_allowlists(self) -> list[Finding]:
        """각 env 키가 local > group > global 체인에서 해석되는지."""
        findings = []
        for name, manifest in self.manifests.items():
            local = self.local_secrets.get(name, frozenset())
            # metadata.group 을 쓴다 — manifest.group 은 미선언 시 예약 그룹
            # 'global' 로 기본값 처리되므로, 그걸 쓰면 미소속 에이전트가
            # groups['global'] 의 secrets 로 통과한다. 런타임 해석
            # (ScopedSecrets.for_agent) 은 미소속을 어떤 그룹 멤버로도 보지
            # 않으므로 배포 검증만 통과하고 기동에서 CFG_002 로 실패한다
            declared_group = manifest.metadata.group
            group = self.groups.get(declared_group) if declared_group else None
            group_keys = frozenset(group.spec.secrets) if group else frozenset()

            for key in manifest.spec.runtime.env_allowlist:
                if key in local or key in group_keys or key in self.global_secrets:
                    continue
                findings.append(
                    Finding(
                        check="env_allowlist",
                        code=ErrorCode.CFG_002,
                        message=f"env key cannot be resolved in any scope: {key}",
                        details={"agent": name, "env_key": key, "group": declared_group},
                    )
                )
        return findings

    # --- 5. connections --------------------------------------------------

    def _check_connections(self, topology: GraphTopology) -> list[Finding]:
        """allowlist 의 caller/callee 가 모두 그래프 노드인지, callee 가 A2A 를 여는지."""
        findings = []
        node_ids = {node.id for node in topology.spec.nodes}

        for connection in topology.spec.connections:
            for role, node_id in (("caller", connection.caller), ("callee", connection.callee)):
                if node_id not in node_ids:
                    findings.append(
                        Finding(
                            check="connections",
                            code=ErrorCode.GRAPH_001,
                            message=f"connection {role} is not a graph node: {node_id}",
                            details={"graph": topology.metadata.name, role: node_id},
                        )
                    )
            findings.extend(self._check_callee_accepts_a2a(topology, connection))
        return findings

    def _check_callee_accepts_a2a(self, topology: GraphTopology, connection: Any) -> list[Finding]:
        """Peer 호출을 받으려면 callee 가 ``a2a.enabled`` 여야 한다 (03 Rule 5)."""
        node = next((n for n in topology.spec.nodes if n.id == connection.callee), None)
        if node is None or node.agent is None:
            return []

        manifest = self.manifests.get(agent_name(node.agent))
        if manifest is None or manifest.spec.a2a.enabled:
            return []

        return [
            Finding(
                check="connections",
                code=ErrorCode.A2A_004,
                message=f"connection callee does not enable a2a: {connection.callee}",
                details={
                    "graph": topology.metadata.name,
                    "a2a_caller": connection.caller,
                    "a2a_callee": connection.callee,
                },
            )
        ]

    # --- 6. mode 별 토폴로지 규칙 ------------------------------------------

    def _check_mode_rules(self, topology: GraphTopology) -> list[Finding]:
        """mission 은 END 도달, service 는 idle 정책 — 검증기에 위임한다."""
        try:
            validate_topology(topology)
        except AaestroError as err:
            return [
                Finding(
                    check="mode_rules",
                    code=ErrorCode(err.code),
                    message=err.message,
                    details={"graph": topology.metadata.name, **err.details},
                )
            ]
        return []

    # --- 7. A2A 포트 수용량 --------------------------------------------

    def _check_a2a_ports(self, topologies: Sequence[GraphTopology]) -> list[Finding]:
        """Verify the port range can seat every A2A-enabled agent.

        포트는 manifest 가 아니라 **runtime 이 범위에서 할당**하므로
        (03 Rule 2), 검증할 것은 선언 충돌이 아니라 **수용량**이다 —
        범위가 모자라면 마지막 에이전트가 기동에 실패한다.
        """
        exposed = sorted(
            name for name, manifest in self.manifests.items() if manifest.spec.a2a.enabled
        )
        if not exposed or self.a2a_port_range is None:
            return []

        low, high = self.a2a_port_range
        available = high - low + 1
        if len(exposed) <= available:
            return []

        return [
            Finding(
                check="a2a_ports",
                code=ErrorCode.CFG_002,
                message="a2a port range cannot seat every exposed agent",
                details={
                    "port_range": [low, high],
                    "available": available,
                    "required": len(exposed),
                    "agents": exposed,
                },
            )
        ]

    # --- 8. quota ---------------------------------------------------------

    def _check_quotas(self) -> list[Finding]:
        """그룹별 합계가 quota 이내인지, 전체가 호스트 한도 이내인지."""
        findings = []

        for group_name, group in self.groups.items():
            members = [m for m in self.manifests.values() if m.group == group_name]
            try:
                check_group_quota(group, members)
            except AaestroError as err:
                findings.append(
                    Finding(
                        check="quotas",
                        code=ErrorCode(err.code),
                        message=err.message,
                        details={"group": group_name, **err.details},
                    )
                )

        try:
            check_host_capacity(
                self.manifests.values(),
                cpu_cores=self.host_cpu_cores,
                memory_bytes=self.host_memory_bytes,
            )
        except AaestroError as err:
            findings.append(
                Finding(
                    check="quotas",
                    code=ErrorCode(err.code),
                    message=err.message,
                    details=dict(err.details),
                )
            )
        return findings

    # --- 10. 모델 provider -------------------------------------------------

    def _check_model_providers(self, topologies: Sequence[GraphTopology]) -> list[Finding]:
        """Every model provider — primary and fallback — must be reachable.

        manifest 의 모든 provider 가 알려진 논리 호스트를 갖고 프록시 upstream 에 있는지,
        노드 override 로 해석된 모델도 같은지 본다. 런타임에 fallback 을 시도했다가
        프록시가 거절하는 것은 조용한 실패다 — 그것을 배포로 앞당긴다.
        """
        findings = []
        for name, manifest in self.manifests.items():
            for provider in sorted(manifest.spec.model.providers()):
                problem = self._provider_problem(provider)
                if problem is not None:
                    findings.append(
                        Finding(
                            check="model_providers",
                            code=ErrorCode.VAL_002,
                            message=problem,
                            details={"agent": name, "provider": provider},
                        )
                    )
        for topology in topologies:
            findings.extend(self._check_node_models(topology))
        return findings

    def _provider_problem(self, provider: str) -> str | None:
        """provider 하나가 배포 가능한지 — 문제가 있으면 그 메시지."""
        if provider not in PROVIDER_HOSTS:
            return f"unknown model provider: {provider}"
        if self.providers is not None and provider not in self.providers:
            return f"model provider is not an egress proxy upstream: {provider}"
        return None

    def _check_node_models(self, topology: GraphTopology) -> list[Finding]:
        """노드 override 로 해석된 모델의 provider 가 manifest 선언 안에 있는지."""
        findings = []
        for node in topology.spec.nodes:
            if node.agent is None or node.model is None:
                continue  # override 가 없으면 manifest 검사가 이미 본 것과 같다
            bound = agent_name(node.agent)
            manifest = self.manifests.get(bound)
            if manifest is None:
                continue  # agent_refs 가 이미 보고했다
            details = {"graph": topology.metadata.name, "node_id": node.id, "agent": bound}
            try:
                resolved = manifest.spec.model.overridden(node.model)
            except ValidationError as err:
                reasons = "; ".join(str(e["msg"]) for e in err.errors())
                findings.append(
                    Finding(
                        check="model_providers",
                        code=ErrorCode.VAL_002,
                        message=f"node model override does not resolve to a valid model: {reasons}",
                        details=details,
                    )
                )
                continue
            # 이그레스 baseline 은 manifest 에서만 도출된다 — manifest 밖 provider 로 바꾸면
            # 배포는 되지만 프록시가 그 호출을 거부해 run 에서 조용히 실패한다
            declared = EgressBaseline.declared(manifest)
            # manifest 가 선언한 provider 는 manifest 검사가 이미 봤다 — 같은 문제를 두 번 내지 않게
            for provider in sorted(resolved.providers() - manifest.spec.model.providers()):
                problem = self._provider_problem(provider)
                if problem is None and PROVIDER_HOSTS[provider] not in declared:
                    problem = (
                        "node model override uses a provider the agent manifest "
                        f"does not declare: {provider}"
                    )
                if problem is not None:
                    findings.append(
                        Finding(
                            check="model_providers",
                            code=ErrorCode.VAL_002,
                            message=problem,
                            details={**details, "provider": provider},
                        )
                    )
        return findings


def validate_deployment(
    topologies: Iterable[GraphTopology],
    *,
    manifests: Mapping[str, AgentManifest],
    groups: Mapping[str, GroupManifest] | None = None,
    resolvable_refs: Iterable[str] = (),
    promptsets: Mapping[str, PromptsetManifest] | None = None,
    local_secrets: Mapping[str, frozenset[str]] | None = None,
    global_secrets: Iterable[str] = (),
    host_cpu_cores: float | None = None,
    host_memory_bytes: int | None = None,
    a2a_port_range: tuple[int, int] | None = None,
    providers: frozenset[str] | None = None,
    state_artifacts: bool = False,
) -> ValidationReport:
    """Validate a deployment against every contract check.

    배포를 10항목 계약 검증으로 확인합니다.

    Args:
        topologies: Graphs about to be deployed.
        manifests: Agent name to manifest.
        groups: Group name to manifest.
        resolvable_refs: Module refs the registry resolves.
        promptsets: Promptset ref to its declaration — 노드 id 템플릿 검사 입력.
        local_secrets: Agent name to its locally available secret keys.
        global_secrets: Globally available secret keys.
        host_cpu_cores: Host CPU ceiling.
        host_memory_bytes: Host memory ceiling.
        a2a_port_range: The runtime's allocatable A2A port range.
        providers: Egress proxy upstream names — None when there is no proxy.
        state_artifacts: Whether ``orchestrator.state_artifact_root`` is configured —
            없으면 ``summarize`` 압축을 선언한 그래프를 거절합니다.

    Returns:
        The validation report.
    """
    validator = DeployValidator(
        manifests=manifests,
        groups=dict(groups or {}),
        resolvable_refs=frozenset(resolvable_refs),
        promptsets=dict(promptsets or {}),
        local_secrets=dict(local_secrets or {}),
        global_secrets=frozenset(global_secrets),
        host_cpu_cores=host_cpu_cores,
        host_memory_bytes=host_memory_bytes,
        a2a_port_range=a2a_port_range,
        providers=providers,
        state_artifacts=state_artifacts,
    )
    return validator.validate(list(topologies))


__all__ = [
    "DeployValidator",
    "Finding",
    "GraphMode",
    "ValidationReport",
    "validate_deployment",
]
