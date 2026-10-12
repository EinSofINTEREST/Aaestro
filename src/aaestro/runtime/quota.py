"""Group resource quota validation.

그룹 리소스 quota 검증. 그룹 소속 에이전트의 리소스 합계가 그룹 상한을 넘으면
기동을 거부한다 (``RT_006``).

배포 검증과 기동 시 재검증 양쪽에서 같은 함수를 쓰도록 순수 함수로 유지한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import ResourceSpec

if TYPE_CHECKING:
    from collections.abc import Iterable

    from aaestro.core.manifest import AgentManifest, GroupManifest


@dataclass(frozen=True)
class ResourceTotals:
    """Aggregated resource demand.

    리소스 합계 — quota 대조의 단위.
    """

    cpu_cores: float = 0.0
    memory_bytes: int = 0
    agents: int = 0

    def plus(self, other: ResourceTotals) -> ResourceTotals:
        """두 합계를 더한다."""
        return ResourceTotals(
            cpu_cores=self.cpu_cores + other.cpu_cores,
            memory_bytes=self.memory_bytes + other.memory_bytes,
            agents=self.agents + other.agents,
        )


def demand_of(manifest: AgentManifest) -> ResourceTotals:
    """Compute one agent's resource demand, including replicas.

    에이전트 한 대(레플리카 포함)의 리소스 요구량을 계산합니다.
    """
    runtime = manifest.spec.runtime
    replicas = runtime.replicas
    if replicas < 1:
        # 조용히 1 로 보정하면 잘못된 manifest 가 quota 검증을 통과해버린다
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="runtime.replicas must be >= 1",
            agent=manifest.name,
            details={"replicas": replicas},
        )
    # MCP 사이드카도 이 에이전트 몫이다 (#304) — 레플리카 수와 무관하게 서버마다 하나.
    # 미선언은 에이전트와 같은 프레임워크 기본값이다 (02 Resources)
    sidecars = [
        server.sidecar.resources or ResourceSpec()
        for server in manifest.spec.mcp.servers
        if server.sidecar is not None
    ]
    return ResourceTotals(
        cpu_cores=runtime.resources.cpu_cores * replicas + sum(r.cpu_cores for r in sidecars),
        memory_bytes=runtime.resources.memory_bytes * replicas
        + sum(r.memory_bytes for r in sidecars),
        agents=replicas,
    )


def total_demand(manifests: Iterable[AgentManifest]) -> ResourceTotals:
    """Sum resource demand across agents.

    여러 에이전트의 리소스 요구량을 합산합니다.
    """
    totals = ResourceTotals()
    for manifest in manifests:
        totals = totals.plus(demand_of(manifest))
    return totals


def _quota_error(message: str, **details: object) -> AaestroError:
    """Quota 초과를 ``RT_006`` 으로 만든다 — 기동 거부."""
    return AaestroError(
        category=ErrorCategory.RUNTIME,
        code=ErrorCode.RT_006,
        message=message,
        details=dict(details),
    )


def check_group_quota(
    group: GroupManifest,
    members: Iterable[AgentManifest],
    *,
    candidate: AgentManifest | None = None,
) -> ResourceTotals:
    """Verify that a group's members fit within its quota.

    그룹 멤버의 리소스 합계가 quota 이내인지 검증합니다.
    ``candidate`` 를 주면 그 에이전트를 추가로 기동해도 되는지 판정합니다.

    Args:
        group: The group whose quota applies.
        members: Agents already counted against the quota.
        candidate: An agent about to start, if any.

    Returns:
        The resulting totals when the check passes.

    Raises:
        AaestroError: RUNTIME/``RT_006`` if any quota dimension is exceeded.
    """
    totals = total_demand(members)
    if candidate is not None:
        totals = totals.plus(demand_of(candidate))

    quotas = group.spec.quotas
    context: dict[str, object] = {"group": group.name}
    if candidate is not None:
        context["agent"] = candidate.name

    cpu_limit = quotas.cpu_cores
    if cpu_limit is not None and totals.cpu_cores > cpu_limit:
        raise _quota_error(
            "group cpu quota exceeded",
            **context,
            requested_cpu=totals.cpu_cores,
            quota_cpu=cpu_limit,
        )

    memory_limit = quotas.memory_bytes
    if memory_limit is not None and totals.memory_bytes > memory_limit:
        raise _quota_error(
            "group memory quota exceeded",
            **context,
            requested_memory_bytes=totals.memory_bytes,
            quota_memory_bytes=memory_limit,
        )

    if quotas.max_agents is not None and totals.agents > quotas.max_agents:
        raise _quota_error(
            "group agent count quota exceeded",
            **context,
            requested_agents=totals.agents,
            quota_max_agents=quotas.max_agents,
        )

    return totals


def check_host_capacity(
    manifests: Iterable[AgentManifest],
    *,
    cpu_cores: float | None = None,
    memory_bytes: int | None = None,
) -> ResourceTotals:
    """Verify that total demand fits the host.

    전체 리소스 합계가 호스트 한도 이내인지 검증합니다.

    Args:
        manifests: Every agent counted against the host.
        cpu_cores: Host CPU ceiling, unbounded when omitted.
        memory_bytes: Host memory ceiling, unbounded when omitted.

    Returns:
        The resulting totals when the check passes.

    Raises:
        AaestroError: RUNTIME/``RT_006`` if the host ceiling is exceeded.
    """
    totals = total_demand(manifests)

    if cpu_cores is not None and totals.cpu_cores > cpu_cores:
        raise _quota_error(
            "host cpu capacity exceeded",
            requested_cpu=totals.cpu_cores,
            capacity_cpu=cpu_cores,
        )

    if memory_bytes is not None and totals.memory_bytes > memory_bytes:
        raise _quota_error(
            "host memory capacity exceeded",
            requested_memory_bytes=totals.memory_bytes,
            capacity_memory_bytes=memory_bytes,
        )

    return totals
