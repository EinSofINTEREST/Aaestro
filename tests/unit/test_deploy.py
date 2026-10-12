"""Unit tests for deploy-time contract validation.

**하나라도 실패하면 컨테이너를 기동하지 않는다.** 10항목 (01 의 8 + 노드 템플릿 +
모델 provider) 각각의 실패 케이스와, 여러 항목이 동시에 실패할 때 전부 보고되는지를
검증한다 — 첫 실패에서 멈추면 운영자가 고칠 때마다 처음부터 다시 돌려야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.deploy import validate_deployment
from aaestro.modules.promptset import PromptsetManifest
from aaestro.orchestrator.topology import GraphTopology
from tests.fixtures.builders import make_manifest
from tests.fixtures.topologies import mission_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
# 이 테스트가 읽는 파일 전부 — 하나만 확인하면 나머지 부재 시 skip 대신
# FileNotFoundError 로 실패한다
REFERENCE_FILES = (
    *(f"agents/{name}/manifest.yaml" for name in ("planner", "researcher", "writer")),
    *(
        f"graphs/{name}.yaml"
        for name in ("research-pipeline", "feed-monitor", "memory-maintenance")
    ),
    "groups/research.yaml",
)
REFS = frozenset({"promptsets/test@0.1.0"})


def make_topology(agents, *, connections=None, edges=None):
    """지정한 에이전트들을 노드로 갖는 mission 토폴로지."""
    nodes = [{"id": name, "agent": f"agents/{name}@0.1.0"} for name in agents]
    if edges is None:
        chain: list[dict[str, str]] = [{"from": "START", "to": agents[0]}]
        chain.extend({"from": a, "to": b} for a, b in zip(agents, agents[1:], strict=False))
        chain.append({"from": agents[-1], "to": "END"})
    else:
        chain = [{"from": source, "to": target} for source, target in edges]

    overrides: dict = {"nodes": nodes, "edges": chain}
    if connections is not None:
        overrides["connections"] = [
            {"caller": caller, "callee": callee} for caller, callee in connections
        ]
    return GraphTopology.model_validate(mission_dict(**overrides))


def agent(name: str, **spec_extra) -> object:
    """레퍼런스 최소 manifest."""
    spec = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": "promptsets/test@0.1.0"},
        **spec_extra,
    }
    return make_manifest(metadata={"name": name, "version": "0.1.0"}, spec=spec)


def group(name: str, **spec_extra):
    """그룹 manifest."""
    from aaestro.core.manifest import GroupManifest

    return GroupManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Group",
            "metadata": {"name": name, "version": "0.1.0"},
            "spec": {"quotas": {"cpu": "8.0", "memory": "16Gi", "max_agents": 10}, **spec_extra},
        }
    )


def run(topologies=None, **overrides):
    """검증 실행 — 기본은 전부 통과하는 구성."""
    manifests = overrides.pop("manifests", None) or {"planner": agent("planner")}
    options = {
        "manifests": manifests,
        "resolvable_refs": REFS,
        **overrides,
    }
    return validate_deployment(topologies or [], **options)


# --- 정상 경로 ----------------------------------------------------------------


def test_valid_deployment_passes():
    report = run([make_topology(["planner"])])

    assert report.ok
    assert report.findings == ()


def test_empty_deployment_passes():
    assert run().ok


# --- 1. agent ref -------------------------------------------------------------


def test_unknown_agent_ref_is_reported():
    """dangling agent ref 로 기동하면 노드 실행 시점에야 실패한다."""
    report = run([make_topology(["absent"])])

    assert not report.ok
    assert "agent_refs" in report.checks()
    assert "MOD_001" in [str(c) for c in report.codes()]


def test_an_agent_ref_with_a_stale_version_is_reported():
    """버전을 안 보면 04 의 semver 고정이 무의미해진다.

    실제로 에이전트를 0.2.0 으로 bump 했는데 다른 그래프 두 개가 0.1.0 을
    가리킨 채 검증을 통과했다 (#149). 런타임에는 실제 manifest 가 떠서
    선언과 실행이 어긋난 채 굴러간다.
    """
    stale = GraphTopology.model_validate(
        mission_dict(
            nodes=[{"id": "planner", "agent": "agents/planner@9.9.9"}],
            edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
        )
    )

    report = run([stale])

    assert not report.ok
    assert "agent_refs" in report.checks()
    assert "MOD_002" in [str(c) for c in report.codes()]


def test_a_stale_version_is_distinguished_from_an_unknown_agent():
    """둘을 뭉개면 오타인지 버전 드리프트인지 구분되지 않는다."""
    stale = GraphTopology.model_validate(
        mission_dict(
            nodes=[{"id": "planner", "agent": "agents/planner@9.9.9"}],
            edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
        )
    )

    codes = {str(c) for c in run([stale]).codes()}
    unknown = {str(c) for c in run([make_topology(["absent"])]).codes()}

    assert "MOD_002" in codes
    assert "MOD_001" not in codes
    assert "MOD_001" in unknown


def test_the_stale_finding_names_the_declared_version():
    """무엇으로 고쳐야 하는지 없으면 운영자가 manifest 를 뒤져야 한다."""
    stale = GraphTopology.model_validate(
        mission_dict(
            nodes=[{"id": "planner", "agent": "agents/planner@9.9.9"}],
            edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
        )
    )

    finding = next(f for f in run([stale]).findings if f.check == "agent_refs")

    assert finding.details["declared_version"] == "0.1.0"
    assert "9.9.9" in finding.message


def test_a_matching_version_passes():
    """정합한 참조까지 걸리면 아무도 이 검증을 켜두지 않는다."""
    report = run([make_topology(["planner"])])

    assert "agent_refs" not in report.checks()


# --- 2. 모듈 ref --------------------------------------------------------------


def test_unresolvable_promptset_is_reported():
    report = run([make_topology(["planner"])], resolvable_refs=frozenset())

    assert "module_refs" in report.checks()


def test_unresolvable_skillset_is_reported():
    manifests = {"planner": agent("planner", skillsets=[{"ref": "skillsets/absent@1.0.0"}])}

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "module_refs" in report.checks()


def test_unresolvable_memoryset_is_reported():
    manifests = {
        "planner": agent(
            "planner",
            memory={"spaces": [{"ref": "memorysets/absent@1.0.0", "as": "longterm"}]},
        )
    }

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "module_refs" in report.checks()


# --- 3. 그룹 소속 --------------------------------------------------------------


def test_unknown_group_is_reported():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "absent"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
            },
        )
    }

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "groups" in report.checks()
    assert "CFG_002" in [str(c) for c in report.codes()]


def test_declared_group_passes():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "research"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
            },
        )
    }

    report = run(
        [make_topology(["planner"])], manifests=manifests, groups={"research": group("research")}
    )

    assert report.ok


# --- MCP 사이드카 (#282, #304) ----------------------------------------------------------


def test_a_sidecar_mcp_server_passes_deploy_validation():
    """runtime 이 사이드카를 띄운다 (#304) — 배포 검증은 더 이상 거절하지 않는다."""
    sidecar = {
        "name": "browser",
        "transport": "streamable-http",
        "sidecar": {"image": "mcp/x:1.0.0"},
    }
    manifests = {"planner": agent("planner", mcp={"servers": [sidecar]})}

    report = run([make_topology(["planner"])], manifests=manifests)

    assert report.ok, report.findings


# --- 4. env_allowlist ---------------------------------------------------------


def test_unresolvable_env_key_is_reported():
    """어느 스코프에서도 해석되지 않는 키로 기동하면 컨테이너가 그 값 없이 뜬다."""
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["MYSTERY_KEY"]})}

    report = run([make_topology(["planner"])], manifests=manifests)

    assert "env_allowlist" in report.checks()


def test_local_secret_resolves_the_key():
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["LOCAL_KEY"]})}

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        local_secrets={"planner": frozenset({"LOCAL_KEY"})},
    )

    assert report.ok


def test_group_secret_resolves_the_key():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "research"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
                "runtime": {"env_allowlist": ["SEARCH_API_KEY"]},
            },
        )
    }

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        groups={"research": group("research", secrets=["SEARCH_API_KEY"])},
    )

    assert report.ok


def test_global_secret_resolves_the_key():
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["ANTHROPIC_API_KEY"]})}

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        global_secrets={"ANTHROPIC_API_KEY"},
    )

    assert report.ok


def test_non_member_cannot_use_a_group_secret():
    """그룹 키는 멤버에게만 제공된다 — 비멤버가 allowlist 에 넣어도 해석되지 않는다."""
    manifests = {"planner": agent("planner", runtime={"env_allowlist": ["SEARCH_API_KEY"]})}

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        groups={"research": group("research", secrets=["SEARCH_API_KEY"])},
    )

    assert "env_allowlist" in report.checks()


# --- 5. connections -----------------------------------------------------------


def test_connection_to_a_non_node_is_reported():
    topology = make_topology(["planner"], connections=[("planner", "ghost")])

    report = run([topology])

    assert "connections" in report.checks()


def test_connection_callee_without_a2a_is_reported():
    """peer 호출을 받으려면 callee 가 a2a.enabled 여야 한다."""
    manifests = {
        "planner": agent("planner"),
        "researcher": agent("researcher", a2a={"enabled": False}),
    }
    topology = make_topology(["planner", "researcher"], connections=[("planner", "researcher")])

    report = run([topology], manifests=manifests)

    assert "A2A_004" in [str(c) for c in report.codes()]


def test_connection_with_enabled_callee_passes():
    manifests = {
        "planner": agent("planner"),
        "researcher": agent("researcher", a2a={"enabled": True}),
    }
    topology = make_topology(["planner", "researcher"], connections=[("planner", "researcher")])

    report = run([topology], manifests=manifests)

    assert report.ok


# --- 6. mode 규칙 -------------------------------------------------------------


def test_topology_violation_is_reported():
    """토폴로지 검증 실패를 배포 검증이 통과시키면 안 된다."""
    topology = make_topology(["planner"], edges=[("START", "planner")])  # END 미도달

    report = run([topology])

    assert "mode_rules" in report.checks()


# --- 7. A2A 포트 수용량 --------------------------------------------------------


def test_insufficient_port_range_is_reported():
    """범위가 모자라면 마지막 에이전트가 기동에 실패한다."""
    manifests = {name: agent(name, a2a={"enabled": True}) for name in ("planner", "researcher")}

    report = run(
        [make_topology(["planner", "researcher"])],
        manifests=manifests,
        a2a_port_range=(9100, 9100),
    )

    assert "a2a_ports" in report.checks()


def test_sufficient_port_range_passes():
    manifests = {name: agent(name, a2a={"enabled": True}) for name in ("planner", "researcher")}

    report = run(
        [make_topology(["planner", "researcher"])],
        manifests=manifests,
        a2a_port_range=(9100, 9199),
    )

    assert report.ok


def test_agents_without_a2a_do_not_consume_ports():
    manifests = {name: agent(name) for name in ("planner", "researcher")}

    report = run(
        [make_topology(["planner", "researcher"])],
        manifests=manifests,
        a2a_port_range=(9100, 9100),
    )

    assert report.ok


# --- 8. quota -----------------------------------------------------------------


def test_group_quota_excess_is_reported():
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "research"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/test@0.1.0"},
                "runtime": {"resources": {"cpu": "16.0", "memory": "1Gi"}},
            },
        )
    }

    report = run(
        [make_topology(["planner"])],
        manifests=manifests,
        groups={"research": group("research")},
    )

    assert "quotas" in report.checks()


def test_host_capacity_excess_is_reported():
    manifests = {
        "planner": agent("planner", runtime={"resources": {"cpu": "4.0", "memory": "1Gi"}})
    }

    report = run([make_topology(["planner"])], manifests=manifests, host_cpu_cores=1.0)

    assert "quotas" in report.checks()


def test_within_capacity_passes():
    manifests = {
        "planner": agent("planner", runtime={"resources": {"cpu": "1.0", "memory": "1Gi"}})
    }

    report = run([make_topology(["planner"])], manifests=manifests, host_cpu_cores=8.0)

    assert report.ok


# --- 결과 수집 ----------------------------------------------------------------


def test_every_failure_is_reported_together():
    """첫 실패에서 멈추면 운영자가 고칠 때마다 처음부터 다시 돌려야 한다."""
    manifests = {
        "planner": make_manifest(
            metadata={"name": "planner", "version": "0.1.0", "group": "absent"},
            spec={
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/absent@9.9.9"},
                "runtime": {"env_allowlist": ["MYSTERY_KEY"]},
            },
        )
    }
    topology = make_topology(["planner", "ghost-node"], connections=[("planner", "nowhere")])

    report = run([topology], manifests=manifests)

    assert set(report.checks()) >= {"agent_refs", "module_refs", "groups", "env_allowlist"}


def test_raise_if_failed_aborts_deployment():
    """미검증 상태로 컨테이너를 기동하지 않는다."""
    report = run([make_topology(["absent"])])

    with pytest.raises(AaestroError) as exc_info:
        report.raise_if_failed()

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.details["failures"]


def test_raise_if_failed_is_silent_when_valid():
    run([make_topology(["planner"])]).raise_if_failed()


# --- 실제 레퍼런스 배포 --------------------------------------------------------


@pytest.mark.skipif(
    not all((REPO_ROOT / relative).exists() for relative in REFERENCE_FILES),
    reason="reference artifacts are not present on this branch",
)
def test_reference_deployment_passes_every_check():
    """실제 배포되는 아티팩트가 10항목을 통과해야 한다 — 검증기가 만든 예제만
    통과하면 아무것도 증명하지 못한다."""
    import yaml

    from aaestro.core.manifest import AgentManifest, GroupManifest

    def load(relative: str) -> dict:
        document: dict = yaml.safe_load((REPO_ROOT / relative).read_text(encoding="utf-8"))
        return document

    manifests = {
        name: AgentManifest.model_validate(load(f"agents/{name}/manifest.yaml"))
        for name in ("planner", "researcher", "writer")
    }
    topologies = [
        GraphTopology.model_validate(load(f"graphs/{name}.yaml"))
        for name in ("research-pipeline", "feed-monitor", "memory-maintenance")
    ]
    refs = {m.spec.promptset.ref for m in manifests.values()}
    refs |= {s.ref for m in manifests.values() for s in m.spec.skillsets}
    refs |= {s.ref for m in manifests.values() for s in m.spec.memory.spaces}

    report = validate_deployment(
        topologies,
        manifests=manifests,
        groups={"research": GroupManifest.model_validate(load("groups/research.yaml"))},
        resolvable_refs=refs,
        global_secrets={"ANTHROPIC_API_KEY"},
        a2a_port_range=(9100, 9199),
        host_cpu_cores=8.0,
    )

    assert report.ok, [f"{f.check}: {f.message}" for f in report.findings]


def test_unaffiliated_agent_cannot_use_global_group_secrets():
    """미소속 에이전트를 예약 그룹 멤버로 취급하면 배포 검증만 통과하고
    기동에서 CFG_002 로 실패한다 — 런타임 해석과 어긋나면 안 된다."""
    manifests = {"solo": agent("solo", runtime={"env_allowlist": ["GROUP_ONLY_KEY"]})}
    reserved = group("global", secrets=["GROUP_ONLY_KEY"])

    report = run([], manifests=manifests, groups={"global": reserved})

    assert "env_allowlist" in report.checks()


def test_deploy_validation_agrees_with_runtime_resolution():
    """같은 manifest 를 두 경로가 다르게 판정하면 검증의 의미가 없다."""
    from aaestro.runtime.scope import ScopedSecrets

    manifest = agent("solo", runtime={"env_allowlist": ["GROUP_ONLY_KEY"]})
    reserved = group("global", secrets=["GROUP_ONLY_KEY"])

    report = run([], manifests={"solo": manifest}, groups={"global": reserved})
    scoped = ScopedSecrets.for_agent(
        manifest, groups={"global": reserved}, group_values={"GROUP_ONLY_KEY": "v"}
    )

    assert not report.ok
    with pytest.raises(AaestroError):
        scoped.resolve("GROUP_ONLY_KEY")


def test_reference_guard_lists_every_file_the_test_reads():
    """가드가 일부만 확인하면 나머지 부재 시 skip 대신 FileNotFoundError 로 실패한다."""
    assert len(REFERENCE_FILES) == 7


# --- 9. 노드 ↔ promptset 계약 (#260) ------------------------------------------------


def make_promptset(templates: dict) -> PromptsetManifest:
    return PromptsetManifest.model_validate(
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "test", "version": "0.1.0"},
            "spec": {"engine": "jinja2", "templates": templates},
        }
    )


def node_report(templates: dict, *, input_map: dict | None = None):
    """노드 하나짜리 그래프를 그 promptset 계약으로 검증한다."""
    topology = GraphTopology.model_validate(
        mission_dict(
            nodes=[{"id": "solo", "agent": "agents/solo@0.1.0", "input_map": input_map or {}}],
            edges=[{"from": "START", "to": "solo"}, {"from": "solo", "to": "END"}],
        )
    )
    return validate_deployment(
        [topology],
        manifests={"solo": make_manifest(name="solo")},
        resolvable_refs=REFS,
        promptsets={"promptsets/test@0.1.0": make_promptset(templates)},
    )


def test_a_node_without_a_matching_template_is_reported():
    """agentd 는 node_id 로 템플릿을 고른다 — 없으면 run 이 시작된 뒤 죽는다."""
    report = node_report({"default": {"file": "d.j2"}})

    assert not report.ok
    finding = next(f for f in report.findings if f.check == "node_templates")
    assert "solo" in finding.message


def test_a_node_that_does_not_supply_a_required_variable_is_reported():
    """#260 — 화면에서 만든 그래프가 여기서 걸린다. run 까지 가면 MOD_004 다."""
    report = node_report(
        {"solo": {"file": "s.j2", "variables": {"query": {"type": "string", "required": True}}}}
    )

    assert not report.ok
    finding = next(f for f in report.findings if f.check == "node_templates")
    assert finding.code == ErrorCode.MOD_004
    assert finding.details["missing"] == ["query"]


def test_supplying_the_variable_through_the_input_map_passes():
    report = node_report(
        {"solo": {"file": "s.j2", "variables": {"query": {"type": "string", "required": True}}}},
        input_map={"query": "state.query"},
    )

    assert report.ok, [f.message for f in report.findings]


def test_an_optional_variable_is_not_required():
    """기본값이 있는 변수까지 요구하면 정상 그래프가 막힌다 — 과잉 거절 방지."""
    report = node_report(
        {"solo": {"file": "s.j2", "variables": {"depth": {"type": "integer", "default": 2}}}}
    )

    assert report.ok, [f.message for f in report.findings]


def test_a_missing_default_template_does_not_fail_a_graph():
    """`default` 는 direct 요청의 계약이다 — 그래프 배선 검증이 그것으로 막지 않는다."""
    report = node_report({"solo": {"file": "s.j2"}})

    assert report.ok, [f.message for f in report.findings]


def test_the_check_is_silent_without_promptsets():
    """promptset 을 못 받으면 이 검사는 아무 것도 보지 못한다 — Author 가 채운다."""
    topology = GraphTopology.model_validate(
        mission_dict(
            nodes=[{"id": "solo", "agent": "agents/solo@0.1.0"}],
            edges=[{"from": "START", "to": "solo"}, {"from": "solo", "to": "END"}],
        )
    )

    report = validate_deployment(
        [topology], manifests={"solo": make_manifest(name="solo")}, resolvable_refs=REFS
    )

    assert report.ok


# --- 10. 모델 provider (#358) --------------------------------------------------

OPENAI = {"provider": "openai-compat", "name": "gpt-5"}


def test_an_openai_compat_primary_passes_when_the_proxy_terminates_it():
    """논리 호스트가 있는 provider 다 — 프록시 upstream 에 있으면 배포된다 (#361)."""
    planner = agent("planner", model=OPENAI)

    assert run(manifests={"planner": planner}).ok
    assert run(
        manifests={"planner": planner}, providers=frozenset({"anthropic", "openai-compat"})
    ).ok


def test_an_openai_compat_primary_the_proxy_does_not_terminate_is_val_002():
    planner = agent("planner", model=OPENAI)

    report = run(manifests={"planner": planner}, providers=frozenset({"anthropic"}))

    [finding] = model_findings(report)
    assert finding.code == ErrorCode.VAL_002
    assert "not an egress proxy upstream" in finding.message
    assert finding.details == {"agent": "planner", "provider": "openai-compat"}


def with_fallback(name: str, fallback: dict) -> object:
    return agent(
        name,
        model={
            "provider": "anthropic",
            "name": "claude-sonnet-5",
            "fallbacks": [{**fallback, "on": ["server_error"]}],
        },
    )


def model_findings(report):
    return [f for f in report.findings if f.check == "model_providers"]


def test_an_unknown_fallback_provider_is_rejected():
    report = run(manifests={"planner": with_fallback("planner", {"provider": "x", "name": "y"})})

    [finding] = model_findings(report)
    assert finding.code == ErrorCode.VAL_002
    assert "unknown model provider" in finding.message


def test_a_fallback_provider_the_proxy_does_not_terminate_is_rejected():
    planner = with_fallback("planner", OPENAI)

    report = run(manifests={"planner": planner}, providers=frozenset({"anthropic"}))
    accepted = run(
        manifests={"planner": planner}, providers=frozenset({"anthropic", "openai-compat"})
    )

    [finding] = model_findings(report)
    assert "not an egress proxy upstream" in finding.message
    assert finding.details["provider"] == "openai-compat"
    assert accepted.ok


def test_a_node_override_to_an_undeclared_provider_is_rejected():
    """이그레스 baseline 은 manifest 에서만 나온다 — 노드가 바꾼 provider 는 프록시가 거부한다."""
    topology = make_topology(["planner"])
    topology = topology.with_model_overrides({"planner": _override(OPENAI)})

    report = run([topology])

    [finding] = model_findings(report)
    assert finding.code == ErrorCode.VAL_002
    assert "does not declare" in finding.message
    assert finding.details["node_id"] == "planner"


def test_a_node_override_to_a_declared_fallback_provider_passes():
    topology = make_topology(["planner"]).with_model_overrides({"planner": _override(OPENAI)})
    planner = with_fallback("planner", {"provider": "openai-compat", "name": "gpt-5-mini"})

    assert run([topology], manifests={"planner": planner}).ok


def test_a_provider_the_manifest_already_reported_is_not_reported_again_per_node():
    """노드가 manifest 선언 provider 로 바꾸면 같은 문제는 manifest 검사 한 건으로 충분하다."""
    topology = make_topology(["planner"]).with_model_overrides({"planner": _override(OPENAI)})
    planner = with_fallback("planner", {"provider": "openai-compat", "name": "gpt-5-mini"})

    report = run([topology], manifests={"planner": planner}, providers=frozenset({"anthropic"}))

    [finding] = model_findings(report)
    assert finding.details == {"agent": "planner", "provider": "openai-compat"}


def test_a_node_override_that_does_not_resolve_is_reported():
    planner = with_fallback("planner", {"provider": "anthropic", "name": "claude-haiku-4-5"})
    topology = make_topology(["planner"]).with_model_overrides(
        {"planner": _override({"name": "claude-haiku-4-5"})}
    )

    [finding] = model_findings(run([topology], manifests={"planner": planner}))
    assert "does not resolve" in finding.message


def _override(data: dict):
    from aaestro.core.manifest import ModelOverride

    return ModelOverride.model_validate(data)


def test_deploy_overrides_layer_on_top_of_node_overrides():
    raw = mission_dict(
        nodes=[{"id": "planner", "agent": "agents/planner@0.1.0", "model": {"max_tokens": 1}}],
        edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
    )
    topology = GraphTopology.model_validate(raw)

    layered = topology.with_model_overrides({"planner": _override({"name": "claude-opus-5"})})

    node_model = layered.node("planner").model
    assert node_model is not None
    assert (node_model.name, node_model.max_tokens) == ("claude-opus-5", 1)
    assert topology.node("planner").model == _override({"max_tokens": 1})  # 원본은 그대로


def test_a_deploy_override_for_an_unknown_node_is_rejected():
    with pytest.raises(AaestroError) as exc_info:
        make_topology(["planner"]).with_model_overrides({"ghost": _override({"max_tokens": 1})})

    assert exc_info.value.code == ErrorCode.VAL_002
    assert exc_info.value.details["node_ids"] == ["ghost"]


def test_empty_deploy_overrides_return_the_same_topology():
    topology = make_topology(["planner"])

    assert topology.with_model_overrides({}) is topology


def test_a_subgraph_node_must_not_declare_a_model():
    raw = mission_dict(
        nodes=[{"id": "sub", "graph": "graphs/sub@1.0.0", "model": {"max_tokens": 1}}],
        edges=[{"from": "START", "to": "sub"}, {"from": "sub", "to": "END"}],
    )

    with pytest.raises(ValueError, match="subgraph node must not declare"):
        GraphTopology.model_validate(raw)
