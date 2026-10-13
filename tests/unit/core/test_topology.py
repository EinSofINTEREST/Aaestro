"""Unit tests for graph topology schema and deploy-time validation.

검증 규칙 하나당 최소 한 개의 실패 케이스를 둔다 — 배포를 막아야 할 그래프가
통과하는 일이 없도록.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.topology import (
    GraphMode,
    GraphTopology,
    IdlePolicy,
    resolve_import_ref,
    validate_topology,
)
from tests.fixtures.topologies import (
    condition_ref,
    make_mission,
    make_service,
    mission_dict,
    service_dict,
)


def assert_graph_001(exc_info: pytest.ExceptionInfo[AaestroError]) -> None:
    """토폴로지 검증 실패는 전부 GRAPH_001 로 보고된다."""
    assert exc_info.value.code == "GRAPH_001"
    assert exc_info.value.category is ErrorCategory.GRAPH


def test_valid_mission_topology_passes():
    validate_topology(make_mission())


def test_valid_service_topology_passes():
    validate_topology(make_service())


def test_topology_is_frozen():
    topology = make_mission()

    with pytest.raises(ValidationError):
        topology.metadata.name = "other"  # type: ignore[misc]


# --- 스키마 수준 규칙 -------------------------------------------------------


@pytest.mark.parametrize("reserved", ["START", "END"])
def test_reserved_node_id_is_rejected(reserved):
    with pytest.raises(ValidationError, match="reserved"):
        GraphTopology.model_validate(
            mission_dict(nodes=[{"id": reserved, "agent": "agents/a@0.1.0"}])
        )


def test_duplicate_node_id_is_rejected():
    with pytest.raises(ValidationError, match="duplicate node id"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[
                    {"id": "planner", "agent": "agents/a@0.1.0"},
                    {"id": "planner", "agent": "agents/b@0.1.0"},
                ]
            )
        )


def test_empty_node_list_is_rejected():
    with pytest.raises(ValidationError, match="at least one node"):
        GraphTopology.model_validate(mission_dict(nodes=[]))


def test_node_requires_exactly_one_target():
    with pytest.raises(ValidationError, match="exactly one of 'agent' or 'graph'"):
        GraphTopology.model_validate(mission_dict(nodes=[{"id": "n"}]))


def test_node_rejects_both_agent_and_graph():
    with pytest.raises(ValidationError, match="exactly one of 'agent' or 'graph'"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[{"id": "n", "agent": "agents/a@0.1.0", "graph": "graphs/sub@1.0.0"}]
            )
        )


def test_self_connection_is_rejected():
    with pytest.raises(ValidationError, match="caller and callee must differ"):
        GraphTopology.model_validate(
            mission_dict(connections=[{"caller": "planner", "callee": "planner"}])
        )


def test_service_mode_requires_idle_policy():
    """service.idle 미선언 시 검증 실패 — 04 Graph Rules 3."""
    raw = service_dict()
    del raw["spec"]["service"]

    with pytest.raises(ValidationError, match="requires 'service.idle'"):
        GraphTopology.model_validate(raw)


def test_mission_mode_rejects_service_settings():
    with pytest.raises(ValidationError, match="only valid in service mode"):
        GraphTopology.model_validate(
            mission_dict(service={"idle": {"min_delay_s": 1, "max_delay_s": 2}})
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [("min_delay_s", 0), ("max_delay_s", 1), ("multiplier", 1.0)],
)
def test_invalid_idle_policy_is_rejected(field, value):
    kwargs = {"min_delay_s": 30.0, "max_delay_s": 600.0, "multiplier": 2.0, field: value}

    with pytest.raises(ValidationError):
        IdlePolicy(**kwargs)


def test_idle_backoff_progresses_and_clamps():
    policy = IdlePolicy(min_delay_s=30, max_delay_s=240, multiplier=2.0)

    assert [policy.delay_for(s) for s in range(5)] == [30, 60, 120, 240, 240]


def test_idle_delay_rejects_negative_streak():
    with pytest.raises(ValueError, match="streak must be >= 0"):
        IdlePolicy().delay_for(-1)


def test_max_failure_streak_must_be_positive():
    with pytest.raises(ValidationError, match="max_failure_streak must be >= 1"):
        GraphTopology.model_validate(
            service_dict(
                service={"idle": {"min_delay_s": 1, "max_delay_s": 2}, "max_failure_streak": 0}
            )
        )


# --- 검증기 규칙 ------------------------------------------------------------


def test_dangling_edge_target_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "ghost"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "dangling edge to" in exc_info.value.message


def test_dangling_edge_source_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "ghost", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "dangling edge from" in exc_info.value.message


def test_edge_out_of_end_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
            {"from": "END", "to": "planner"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "END must not have outgoing edges" in exc_info.value.message


def test_edge_into_start_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "START"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "START must not have incoming edges" in exc_info.value.message


def test_unreachable_node_is_rejected():
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0"},
            {"id": "orphan", "agent": "agents/orphan@0.1.0"},
        ],
        edges=[{"from": "START", "to": "planner"}, {"from": "planner", "to": "END"}],
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "unreachable from START" in exc_info.value.message


def test_mission_without_end_reachability_is_rejected():
    """mission 은 END 도달 필수 — 04 Graph Rules 3."""
    topology = make_mission(
        nodes=[{"id": "planner", "agent": "agents/planner@0.1.0"}],
        edges=[{"from": "START", "to": "planner"}],
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "must be able to reach END" in exc_info.value.message


def test_mission_cycle_without_max_iterations_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "planner"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "requires 'max_iterations'" in exc_info.value.message


def test_mission_cycle_with_max_iterations_passes():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {
                "from": "researcher",
                "to": "planner",
                "condition": condition_ref(),
                "max_iterations": 3,
            },
            {"from": "researcher", "to": "END"},
        ]
    )

    validate_topology(topology)


# --- 병렬 분기 (#313) -----------------------------------------------------------------


@pytest.mark.parametrize("source", ["START", "planner"])
def test_unconditional_fan_out_is_rejected(source):
    """조건 없는 edge 둘은 LangGraph 가 병렬로 돌려 병합 규칙 없는 채널에서 run 이 죽는다."""
    edges = [
        {"from": "START", "to": "planner"},
        {"from": "planner", "to": "researcher"},
        {"from": "researcher", "to": "END"},
    ]
    edges.append({"from": source, "to": "researcher" if source == "START" else "END"})
    topology = make_mission(edges=edges)

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "parallel branches are not supported" in exc_info.value.message
    assert exc_info.value.details["node_id"] == source


def test_a_second_fallback_beside_conditions_is_rejected():
    """라우터는 첫 번째 기본 경로만 쓴다 — 두 번째는 조용히 버려진다."""
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher", "condition": condition_ref()},
            {"from": "planner", "to": "END"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert exc_info.value.details["node_id"] == "planner"


def test_a_conditional_branch_with_a_fallback_is_not_a_fan_out():
    """조건 edge 가 있는 노드는 라우터가 한 곳만 고른다 — 조건 없는 edge 는 기본 경로다."""
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher", "condition": condition_ref()},
            {"from": "planner", "to": "END"},
            {"from": "researcher", "to": "END"},
        ]
    )

    validate_topology(topology)


def test_service_graph_must_reach_end():
    """service 도 END 로 끝나야 한다 — 반복은 그래프가 아니라 runner 가 소유한다.

    그래프가 스스로 순환하면 한 번의 실행이 끝나지 않아 ServiceRunner 의
    iteration 경계가 성립하지 않는다.
    """
    topology = make_service(
        edges=[
            {"from": "START", "to": "watcher"},
            {"from": "watcher", "to": "notifier"},
            {"from": "notifier", "to": "watcher"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "service graph must be able to reach END" in exc_info.value.message


def test_service_cycle_requires_max_iterations():
    """그래프 안에 남긴 순환은 mission 과 같은 상한 규칙을 받는다."""
    topology = make_service(
        edges=[
            {"from": "START", "to": "watcher"},
            {"from": "watcher", "to": "notifier"},
            {"from": "notifier", "to": "watcher", "condition": condition_ref()},
            {"from": "notifier", "to": "END", "condition": condition_ref()},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "max_iterations" in exc_info.value.message


def test_connection_caller_must_be_a_node():
    topology = make_mission(connections=[{"caller": "ghost", "callee": "planner"}])

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "connection caller is not a graph node" in exc_info.value.message


def test_connection_callee_must_be_a_node():
    topology = make_mission(connections=[{"caller": "planner", "callee": "ghost"}])

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "connection callee is not a graph node" in exc_info.value.message


def test_valid_connection_passes():
    validate_topology(make_mission(connections=[{"caller": "researcher", "callee": "planner"}]))


def test_unimportable_condition_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "researcher", "condition": "aaestro.graphs.nope:fn"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "cannot import module" in exc_info.value.message


def test_missing_condition_attribute_is_rejected():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {
                "from": "planner",
                "to": "researcher",
                "condition": "aaestro.graphs.conditions:no_such_fn",
            },
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert "attribute not found" in exc_info.value.message


def test_input_map_unknown_state_field_is_rejected():
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"q": "state.nonexistent"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology, state_fields=frozenset({"query", "plan"}))

    assert_graph_001(exc_info)
    assert "unknown state field" in exc_info.value.message


def test_input_map_known_state_field_passes():
    validate_topology(make_mission(), state_fields=frozenset({"query", "plan"}))


def test_input_map_literal_source_is_not_checked():
    """state. 접두사가 없으면 리터럴이므로 schema 대조 대상이 아니다."""
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "input_map": {"mode": "fast"}},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    validate_topology(topology, state_fields=frozenset({"query"}))


# --- ref 해석 ---------------------------------------------------------------


def test_resolve_import_ref_returns_attribute():
    fn = resolve_import_ref("aaestro.graphs.conditions:needs_research")

    assert fn({"needs_research": True}) is True


def test_resolve_import_ref_rejects_missing_separator():
    with pytest.raises(AaestroError) as exc_info:
        resolve_import_ref("aaestro.graphs.conditions.needs_research")

    assert "invalid import ref" in exc_info.value.message


# --- 노드 조회 --------------------------------------------------------------


def test_node_lookup():
    topology = make_mission()

    assert topology.node("planner").ref == "agents/planner@0.1.0"
    assert topology.node_ids == frozenset({"planner", "researcher"})
    assert topology.mode is GraphMode.MISSION


def test_node_lookup_missing_raises_key_error():
    with pytest.raises(KeyError):
        make_mission().node("ghost")


def test_subgraph_node_ref():
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0"},
            {"id": "review", "graph": "graphs/sub-review@1.0.0"},
        ],
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "review"},
            {"from": "review", "to": "END"},
        ],
    )

    review = topology.node("review")
    assert review.is_subgraph is True
    assert review.ref == "graphs/sub-review@1.0.0"


@pytest.mark.parametrize(
    "ref",
    ["os:system", "subprocess:run", "aaestro.graphsx.evil:fn", "tests.fixtures.anything:fn"],
)
def test_an_import_ref_outside_the_graphs_package_is_refused_before_import(ref, monkeypatch):
    """검증이 import 하는 순간 모듈 최상위 코드가 돈다 — YAML 문자열 하나로 돌면 안 된다."""
    imported = []
    monkeypatch.setattr(
        "aaestro.core.topology.importlib.import_module", lambda name: imported.append(name)
    )

    with pytest.raises(AaestroError) as exc_info:
        resolve_import_ref(ref)

    assert_graph_001(exc_info)
    assert imported == []


def test_a_declarative_condition_outside_the_grammar_is_refused_when_the_graph_is_read():
    with pytest.raises(ValidationError, match="invalid condition"):
        make_mission(
            edges=[
                {"from": "START", "to": "planner"},
                {"from": "planner", "to": "researcher", "condition": "len(state.plan) > 0"},
                {"from": "researcher", "to": "END"},
            ]
        )


def test_a_condition_import_ref_is_still_honoured_but_flagged_deprecated(monkeypatch):
    """한 버전은 그대로 동작한다 — 대신 운영자가 옮길 곳을 알게 한다."""
    warnings = []
    monkeypatch.setattr(
        "aaestro.core.topology.log.warning", lambda event, **fields: warnings.append(fields)
    )

    validate_topology(
        make_mission(
            edges=[
                {"from": "START", "to": "planner"},
                {"from": "planner", "to": "researcher", "condition": condition_ref()},
                {"from": "planner", "to": "END"},
                {"from": "researcher", "to": "END"},
            ]
        )
    )

    assert [w["condition"] for w in warnings] == [condition_ref()]


def test_import_ref_with_empty_module_is_wrapped_as_graph_001():
    """빈 모듈명은 ValueError 를 내지만, 계약상 GRAPH_001 로 보고돼야 한다."""
    with pytest.raises(AaestroError) as exc_info:
        resolve_import_ref(":attr")

    assert_graph_001(exc_info)


@pytest.mark.parametrize("value", [0, -1, -5])
def test_non_positive_max_iterations_is_rejected(value):
    """음수는 truthy 라 상한 검사를 조용히 통과시킨다 — 스키마에서 막는다."""
    with pytest.raises(ValidationError, match="max_iterations must be >= 1"):
        GraphTopology.model_validate(
            mission_dict(
                edges=[
                    {"from": "START", "to": "planner"},
                    {"from": "planner", "to": "researcher"},
                    {"from": "researcher", "to": "planner", "max_iterations": value},
                    {"from": "researcher", "to": "END"},
                ]
            )
        )


def test_negative_retry_is_rejected():
    with pytest.raises(ValidationError, match="retry must be >= 0"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[
                    {"id": "planner", "agent": "agents/planner@0.1.0", "retry": -1},
                    {"id": "researcher", "agent": "agents/researcher@0.1.0"},
                ]
            )
        )


@pytest.mark.parametrize("value", [0, -1.5])
def test_non_positive_node_timeout_is_rejected(value):
    with pytest.raises(ValidationError, match="timeout_s must be > 0"):
        GraphTopology.model_validate(
            mission_dict(
                nodes=[
                    {"id": "planner", "agent": "agents/planner@0.1.0", "timeout_s": value},
                    {"id": "researcher", "agent": "agents/researcher@0.1.0"},
                ]
            )
        )


def test_max_iterations_outside_the_cycle_does_not_satisfy_the_rule():
    """무관한 edge 에 붙은 상한은 실제 순환을 제한하지 못한다."""
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner", "max_iterations": 3},  # cycle 밖
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "planner"},  # 실제 순환 — 상한 없음
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError) as exc_info:
        validate_topology(topology)

    assert_graph_001(exc_info)
    assert "max_iterations" in exc_info.value.message


def test_max_iterations_on_the_cycle_edge_satisfies_the_rule():
    validate_topology(
        make_mission(
            edges=[
                {"from": "START", "to": "planner"},
                {"from": "planner", "to": "researcher"},
                {
                    "from": "researcher",
                    "to": "planner",
                    "condition": condition_ref(),
                    "max_iterations": 3,
                },
                {"from": "researcher", "to": "END"},
            ]
        )
    )


def test_self_loop_requires_max_iterations():
    topology = make_mission(
        edges=[
            {"from": "START", "to": "planner"},
            {"from": "planner", "to": "planner"},
            {"from": "planner", "to": "researcher"},
            {"from": "researcher", "to": "END"},
        ]
    )

    with pytest.raises(AaestroError):
        validate_topology(topology)


def test_input_map_preserves_non_string_literals():
    """YAML 의 숫자/불리언 리터럴이 문자열로 강제되지 않아야 한다."""
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"depth": 2, "verbose": True, "q": "state.query"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    input_map = topology.node("planner").input_map
    assert input_map["depth"] == 2
    assert input_map["verbose"] is True


def test_input_map_validation_ignores_non_string_literals():
    """리터럴은 state 참조가 아니므로 schema 대조 대상이 아니다."""
    topology = make_mission(
        nodes=[
            {"id": "planner", "agent": "agents/planner@0.1.0", "input_map": {"depth": 2}},
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    validate_topology(topology, state_fields=frozenset({"query"}))


# --- 노드 model override (#358) ------------------------------------------------------


def test_a_yaml_node_model_keeps_only_the_keys_it_wrote():
    """YAML 로드 경로 — 쓴 키만 set 이어야 병합이 아래 계층을 덮지 않는다.

    ``fallbacks: []`` 는 "fallback 을 지운다" 는 명시이고, 안 쓴 키는 아래 계층 그대로다.
    """
    import yaml

    document = yaml.safe_load(
        "apiVersion: aaestro/v1\nkind: Graph\n"
        "metadata: {name: g, version: 1.0.0}\n"
        "spec:\n  mode: mission\n  goal: g\n"
        "  state: {schema: 'aaestro.graphs.schemas:ResearchState'}\n"
        "  nodes:\n"
        "    - id: planner\n      agent: agents/planner@0.1.0\n      model: {fallbacks: []}\n"
        "    - id: writer\n      agent: agents/writer@0.1.0\n"
        "      model: {name: m, max_tokens: 9}\n"
        "  edges:\n"
        "    - {from: START, to: planner}\n    - {from: planner, to: writer}\n"
        "    - {from: writer, to: END}\n"
    )

    topology = GraphTopology.model_validate(document)

    assert topology.node("planner").model.model_fields_set == {"fallbacks"}
    assert topology.node("writer").model.model_fields_set == {"name", "max_tokens"}


def test_model_overrides_keep_the_api_version_alias_when_dumped():
    """model_copy 가 alias 를 보존해야 override 를 입힌 토폴로지가 다시 YAML 로 읽힌다."""
    from aaestro.core.manifest import ModelOverride

    topology = make_mission().with_model_overrides({"planner": ModelOverride(name="m")})

    # Author 가 파일로 쓰는 것과 같은 덤프
    dumped = topology.model_dump(mode="json", by_alias=True, exclude_none=True)

    assert dumped["apiVersion"] == "aaestro/v1"
    assert "api_version" not in dumped
    assert GraphTopology.model_validate(dumped).node("planner").model.name == "m"
