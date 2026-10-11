"""Per-node model resolution — manifest, node override, deployment override (#358)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from malkuth.catalog import Catalog
from malkuth.core.errors import ErrorCode, MalkuthError
from malkuth.orchestrator.models import CatalogModels, resolve_node_model, with_recorded_overrides
from malkuth.orchestrator.topology import NodeSpec
from tests.fixtures.topologies import make_mission


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    path = tmp_path / "agents" / "planner" / "manifest.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "malkuth/v1",
                "kind": "Agent",
                "metadata": {"name": "planner", "version": "0.1.0"},
                "spec": {
                    "model": {
                        "provider": "anthropic",
                        "name": "claude-sonnet-5",
                        "effort": "high",
                        "fallbacks": [
                            {
                                "provider": "anthropic",
                                "name": "claude-haiku-4-5",
                                "on": ["rate_limit"],
                            }
                        ],
                    },
                    "promptset": {"ref": "promptsets/planner@0.1.0"},
                },
            }
        ),
        encoding="utf-8",
    )
    return Catalog.under(tmp_path)


def test_a_node_without_override_runs_the_manifest_model(catalog):
    node = NodeSpec(id="p", agent="agents/planner@0.1.0")

    model = resolve_node_model(catalog, node)

    assert model is not None and model.name == "claude-sonnet-5"
    assert [f.name for f in model.fallbacks] == ["claude-haiku-4-5"]


def test_a_node_override_replaces_only_the_keys_it_names(catalog):
    node = NodeSpec.model_validate(
        {"id": "p", "agent": "agents/planner@0.1.0", "model": {"name": "claude-opus-5"}}
    )

    model = CatalogModels(catalog)(node)

    assert model is not None
    assert (model.name, model.effort) == ("claude-opus-5", "high")
    assert [f.name for f in model.fallbacks] == ["claude-haiku-4-5"]


def test_a_subgraph_node_has_no_model(catalog):
    node = NodeSpec(id="review", graph="graphs/sub-review@1.0.0")

    assert resolve_node_model(catalog, node) is None


def test_recorded_overrides_layer_over_the_node_and_keep_explicit_empties(catalog):
    topology = make_mission(
        nodes=[
            {
                "id": "planner",
                "agent": "agents/planner@0.1.0",
                "input_map": {"query": "state.query"},
                "model": {"name": "claude-opus-5"},
            },
            {"id": "researcher", "agent": "agents/researcher@0.1.0"},
        ]
    )

    layered = with_recorded_overrides(topology, {"planner": {"fallbacks": []}})
    model = resolve_node_model(catalog, layered.node("planner"))

    # 노드 override 는 남고, 배포가 명시한 빈 fallbacks 가 manifest 값을 지운다
    assert model is not None
    assert (model.name, model.fallbacks) == ("claude-opus-5", ())


def test_recorded_overrides_for_a_vanished_node_are_val_002():
    with pytest.raises(MalkuthError) as excinfo:
        with_recorded_overrides(make_mission(), {"ghost": {"name": "m"}})

    assert excinfo.value.code == ErrorCode.VAL_002


def test_no_recorded_overrides_leaves_the_topology_alone():
    topology = make_mission()

    assert with_recorded_overrides(topology, {}) is topology
