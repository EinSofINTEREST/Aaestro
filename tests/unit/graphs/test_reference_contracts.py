"""Reference artifacts agree with each other.

promptset 의 변수 타입이 state schema 와 어긋나면 그래프는 **한 번도 성공하지
못한다** — `feed-monitor` 가 정확히 그 상태였다 (#201).

E2E 가 iteration 회차만 세면 이것을 놓친다: 회차는 실패해도 올라간다.
여기서는 아티팩트끼리의 계약을 **선언 수준에서** 대조한다.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
import yaml

from aaestro.core.topology import GraphTopology
from aaestro.modules.promptset import PromptsetManifest
from aaestro.orchestrator.state import resolve_state_schema

REPO_ROOT = Path(__file__).resolve().parents[3]


def graphs() -> list[GraphTopology]:
    return [
        GraphTopology.model_validate(yaml.safe_load(path.read_text("utf-8")))
        for path in sorted((REPO_ROOT / "graphs").glob("*.yaml"))
    ]


def promptset_of(agent_ref: str) -> PromptsetManifest:
    """에이전트가 선언한 promptset 을 읽는다."""
    name = agent_ref.split("/")[-1].split("@")[0]
    manifest = yaml.safe_load((REPO_ROOT / "agents" / name / "manifest.yaml").read_text("utf-8"))
    _, rest = manifest["spec"]["promptset"]["ref"].split("/", 1)
    module, version = rest.split("@")
    path = REPO_ROOT / "modules/promptsets" / module / version / "promptset.yaml"
    return PromptsetManifest.model_validate(yaml.safe_load(path.read_text("utf-8")))


def accepts_list(annotation: object) -> bool:
    """state 필드가 리스트를 담는가 — Optional 등 래핑을 벗겨 본다."""
    origin = typing.get_origin(annotation)
    if origin in (list, tuple):
        return True
    if origin is not None:
        return any(accepts_list(arg) for arg in typing.get_args(annotation))
    return False


def wiring() -> list[tuple[str, str, str, str, object]]:
    """(그래프, 노드, 변수, 선언 타입, state 필드 타입) 전수."""
    found = []
    for topology in graphs():
        schema = resolve_state_schema(topology.spec.state.schema_ref)
        for node in topology.spec.nodes:
            if node.agent is None:
                continue
            declared = promptset_of(node.agent).spec.templates.get(node.id)
            if declared is None:
                continue
            for variable, source in (node.input_map or {}).items():
                spec = declared.variables.get(variable)
                field = schema.model_fields.get(str(source).removeprefix("state."))
                if spec is None or field is None:
                    continue
                found.append((topology.name, node.id, variable, spec.type, field.annotation))
    return found


def test_the_reference_graphs_declare_wiring():
    """대조할 것이 없으면 이 파일은 아무 것도 증명하지 않는다."""
    assert wiring()


@pytest.mark.parametrize(
    ("graph", "node", "variable", "declared", "annotation"),
    wiring(),
    ids=lambda value: str(value),
)
def test_a_declared_variable_matches_the_state_field(
    graph: str, node: str, variable: str, declared: str, annotation: object
):
    """선언이 어긋나면 그 노드는 `MOD_004` 로 **매번** 실패한다."""
    if accepts_list(annotation):
        assert declared == "array", (
            f"{graph}/{node}: '{variable}' 는 state 가 리스트인데 "
            f"promptset 은 '{declared}' 로 선언한다"
        )
    else:
        assert declared != "array", (
            f"{graph}/{node}: '{variable}' 는 state 가 리스트가 아닌데 "
            f"promptset 은 'array' 로 선언한다"
        )


def test_every_wired_node_has_a_template():
    """노드 id 로 템플릿을 고르므로(04 호환성 3), 없으면 렌더 자체가 실패한다."""
    missing = [
        f"{topology.name}/{node.id}"
        for topology in graphs()
        for node in topology.spec.nodes
        if node.agent is not None and node.id not in promptset_of(node.agent).spec.templates
    ]

    assert not missing, f"템플릿이 없는 노드: {missing}"
