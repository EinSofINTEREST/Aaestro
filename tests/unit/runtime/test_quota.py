"""Unit tests for group quota and host capacity validation."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.manifest import GroupManifest
from aaestro.runtime.quota import (
    ResourceTotals,
    check_group_quota,
    check_host_capacity,
    demand_of,
    total_demand,
)
from tests.fixtures.builders import make_manifest

GIB = 1024**3


def agent(name: str = "worker", *, cpu: str = "1.0", memory: str = "1Gi", replicas: int = 1):
    """리소스만 바꿔 쓰는 에이전트 manifest."""
    return make_manifest(
        metadata={"name": name, "version": "0.1.0", "group": "research"},
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            "runtime": {
                "resources": {"cpu": cpu, "memory": memory},
                "replicas": replicas,
            },
        },
    )


def group(cpu: str | None = "8.0", memory: str | None = "16Gi", max_agents: int | None = 10):
    """quota 만 바꿔 쓰는 그룹 정의."""
    quotas: dict[str, object] = {}
    if cpu is not None:
        quotas["cpu"] = cpu
    if memory is not None:
        quotas["memory"] = memory
    if max_agents is not None:
        quotas["max_agents"] = max_agents
    return GroupManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": "research", "version": "0.1.0"},
            "spec": {"quotas": quotas},
        }
    )


def assert_rt_006(exc_info: pytest.ExceptionInfo[AaestroError]) -> None:
    """quota 초과는 RT_006 으로 기동을 거부한다."""
    assert exc_info.value.code == "RT_006"
    assert exc_info.value.category is ErrorCategory.RUNTIME


# --- 수요 계산 -------------------------------------------------------------


def test_demand_of_single_agent():
    totals = demand_of(agent(cpu="2.0", memory="4Gi"))

    assert totals == ResourceTotals(cpu_cores=2.0, memory_bytes=4 * GIB, agents=1)


def test_demand_multiplies_by_replicas():
    """레플리카는 각각 리소스를 점유한다."""
    totals = demand_of(agent(cpu="1.0", memory="1Gi", replicas=3))

    assert totals == ResourceTotals(cpu_cores=3.0, memory_bytes=3 * GIB, agents=3)


def test_sidecars_count_once_each_whatever_the_replicas():
    """MCP 사이드카는 에이전트 몫이다 (#304) — 레플리카마다가 아니라 서버마다 하나."""
    manifest = make_manifest(
        metadata={"name": "worker", "version": "0.1.0", "group": "research"},
        spec={
            "runtime": {"resources": {"cpu": "1.0", "memory": "1Gi"}, "replicas": 2},
            "mcp": {
                "servers": [
                    {
                        "name": "browser",
                        "transport": "streamable-http",
                        "sidecar": {
                            "image": "mcp/playwright:1.2.0",
                            "resources": {"cpu": "0.5", "memory": "512Mi"},
                        },
                    },
                    {
                        "name": "search",
                        "transport": "streamable-http",
                        "sidecar": {"image": "mcp/search:0.3.0"},
                    },
                    {"name": "corp", "transport": "streamable-http", "url": "https://x.example"},
                ]
            },
        },
    )

    totals = demand_of(manifest)

    # 레플리카 2 × (1.0, 1Gi) + browser (0.5, 512Mi) + search 기본값 (1.0, 1Gi); 원격 서버는 0
    assert totals == ResourceTotals(cpu_cores=3.5, memory_bytes=3 * GIB + GIB // 2, agents=2)


def test_total_demand_sums_across_agents():
    totals = total_demand([agent("a", cpu="1.0"), agent("b", cpu="2.5")])

    assert totals.cpu_cores == 3.5
    assert totals.agents == 2


def test_total_demand_of_nothing_is_zero():
    assert total_demand([]) == ResourceTotals()


# --- 그룹 quota ------------------------------------------------------------


def test_within_quota_passes():
    totals = check_group_quota(group(), [agent("a"), agent("b")])

    assert totals.cpu_cores == 2.0


def test_exactly_at_quota_passes():
    """경계값은 통과 — 초과일 때만 거부한다."""
    check_group_quota(group(cpu="2.0", memory="2Gi", max_agents=2), [agent("a"), agent("b")])


def test_cpu_quota_exceeded_is_rejected():
    with pytest.raises(AaestroError) as exc_info:
        check_group_quota(group(cpu="1.5"), [agent("a"), agent("b")])

    assert_rt_006(exc_info)
    assert "cpu quota" in exc_info.value.message
    assert exc_info.value.details["quota_cpu"] == 1.5


def test_memory_quota_exceeded_is_rejected():
    with pytest.raises(AaestroError) as exc_info:
        check_group_quota(group(cpu=None, memory="1Gi"), [agent("a"), agent("b")])

    assert_rt_006(exc_info)
    assert "memory quota" in exc_info.value.message


def test_max_agents_quota_exceeded_is_rejected():
    with pytest.raises(AaestroError) as exc_info:
        check_group_quota(group(cpu=None, memory=None, max_agents=1), [agent("a"), agent("b")])

    assert_rt_006(exc_info)
    assert "agent count quota" in exc_info.value.message


def test_candidate_is_counted_against_the_quota():
    """기동하려는 에이전트를 더해도 상한 이내여야 한다."""
    members = [agent("a"), agent("b")]

    check_group_quota(group(cpu="3.0"), members, candidate=agent("c"))

    with pytest.raises(AaestroError) as exc_info:
        check_group_quota(group(cpu="2.5"), members, candidate=agent("c"))

    assert exc_info.value.details["agent"] == "c"


def test_unset_quota_dimension_is_unbounded():
    check_group_quota(
        group(cpu=None, memory=None, max_agents=None), [agent(f"a{i}") for i in range(50)]
    )


def test_replicas_count_toward_max_agents():
    with pytest.raises(AaestroError) as exc_info:
        check_group_quota(group(cpu=None, memory=None, max_agents=2), [agent("a", replicas=3)])

    assert exc_info.value.details["requested_agents"] == 3


def test_quota_error_carries_group_context():
    with pytest.raises(AaestroError) as exc_info:
        check_group_quota(group(cpu="0.5"), [agent("a")])

    assert exc_info.value.details["group"] == "research"


# --- 호스트 한도 -----------------------------------------------------------


def test_host_capacity_within_limits_passes():
    totals = check_host_capacity([agent("a"), agent("b")], cpu_cores=4.0, memory_bytes=4 * GIB)

    assert totals.agents == 2


def test_host_cpu_capacity_exceeded_is_rejected():
    with pytest.raises(AaestroError) as exc_info:
        check_host_capacity([agent("a", cpu="4.0")], cpu_cores=2.0)

    assert_rt_006(exc_info)
    assert "host cpu capacity" in exc_info.value.message


def test_host_memory_capacity_exceeded_is_rejected():
    with pytest.raises(AaestroError) as exc_info:
        check_host_capacity([agent("a", memory="8Gi")], memory_bytes=4 * GIB)

    assert_rt_006(exc_info)
    assert "host memory capacity" in exc_info.value.message


def test_host_capacity_without_limits_is_unbounded():
    check_host_capacity([agent(f"a{i}", cpu="8.0") for i in range(10)])


@pytest.mark.parametrize("replicas", [0, -1])
def test_non_positive_replicas_is_a_config_error(replicas):
    """조용히 1 로 보정하면 잘못된 manifest 가 quota 검증을 통과한다."""
    with pytest.raises(AaestroError) as exc_info:
        demand_of(agent(replicas=replicas))

    assert exc_info.value.code == "VAL_002"
    assert exc_info.value.agent == "worker"


def test_single_replica_is_the_normal_case():
    assert demand_of(agent(replicas=1)).agents == 1
