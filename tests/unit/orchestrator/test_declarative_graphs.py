"""A graph built without touching ``src/`` (#316).

04 는 "새 목표 = 기존 모듈 배선, ``src/`` 수정이 필요하면 프레임워크 결함" 이라 한다. state
모델과 조건 함수가 코드에만 있으면 새 그래프는 거의 항상 ``src/`` 를 고쳐야 했다. 레포의
research-pipeline 을 인라인 state 와 선언식 조건으로 다시 쓴 fixture 가 원본과 같게 도는지 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from aaestro.core.errors import AaestroError
from aaestro.core.topology import GraphTopology, StateSpec, validate_topology
from aaestro.graphs.schemas import ResearchState
from aaestro.orchestrator.builder import build_graph
from aaestro.orchestrator.state import resolve_graph_state, state_fields
from aaestro.orchestrator.submit import RunSubmitter
from tests.fixtures.fake_runtime import FakeRuntime

REPO_ROOT = Path(__file__).resolve().parents[3]


def load(path: Path) -> GraphTopology:
    return GraphTopology.model_validate(yaml.safe_load(path.read_text("utf-8")))


def declarative() -> GraphTopology:
    return load(REPO_ROOT / "tests" / "fixtures" / "graphs" / "research-pipeline-declarative.yaml")


def original() -> GraphTopology:
    return load(REPO_ROOT / "graphs" / "research-pipeline.yaml")


def test_the_declarative_rewrite_passes_validation_without_imports():
    topology = declarative()
    schema = resolve_graph_state(topology.spec.state, graph=topology.name)

    validate_topology(topology, state_fields=state_fields(schema))


def test_the_inline_state_matches_the_model_it_replaces():
    schema = resolve_graph_state(declarative().spec.state, graph="research-pipeline-declarative")

    assert state_fields(schema) == state_fields(ResearchState)
    assert schema(query="q").model_dump() == ResearchState(query="q").model_dump()
    assert schema.model_config.get("frozen") is True


@pytest.mark.parametrize("needs_research", [True, False])
async def test_the_rewrite_routes_like_the_original(needs_research):
    routes = []
    for topology in (original(), declarative()):
        runtime = (
            FakeRuntime()
            .script("planner", output={"plan": "P", "needs_research": needs_research})
            .script("researcher", output={"findings": ["f"]})
            .script("writer", output={"report": "R"})
        )
        final = await build_graph(topology, runtime).ainvoke({"query": "q", "_run_id": "r"})
        routes.append((runtime.invoked, final.get("report")))

    assert routes[0] == routes[1]


async def test_a_required_inline_field_is_enforced_at_submission():
    """필수 필드가 빠진 제출은 슬롯을 잡기 전에 GRAPH_003 으로 거부된다 — 모델과 같은 규칙."""
    with pytest.raises(AaestroError) as exc_info:
        await RunSubmitter(runtime=FakeRuntime()).submit(declarative(), {})

    assert exc_info.value.code == "GRAPH_003"


def test_the_same_declaration_yields_the_same_model():
    """빌드마다 새 클래스를 만들면 캐시·비교가 매번 어긋난다."""
    state = declarative().spec.state

    assert resolve_graph_state(state, graph="g") is resolve_graph_state(state, graph="g")


@pytest.mark.parametrize(
    "state",
    [
        {},  # 둘 다 없음
        {"schema": "aaestro.graphs.schemas:ResearchState", "fields": {"q": {"type": "string"}}},
        {"fields": {"_run_id": {"type": "string"}}},  # 예약 채널과 겹침
        {"fields": {"query": {"type": "string", "required": True, "default": "x"}}},
        {"fields": {"query": {"type": "text"}}},
    ],
)
def test_an_ambiguous_state_declaration_is_refused(state):
    with pytest.raises(ValidationError):
        StateSpec.model_validate(state)
