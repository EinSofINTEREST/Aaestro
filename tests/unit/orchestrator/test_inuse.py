"""Running runs pin their declarations (#249 review)."""

from __future__ import annotations

from pathlib import Path

from aaestro.catalog import Catalog
from aaestro.orchestrator.inuse import run_backed
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord

REPO_ROOT = Path(__file__).resolve().parents[3]


def store_with(status: str) -> InMemoryRunStore:
    store = InMemoryRunStore()
    store.upsert(RunRecord(run_id="r1", graph="research-pipeline", mode="mission", status=status))
    return store


def test_a_running_run_pins_its_graph_and_agents():
    in_use = run_backed(store_with("running"), Catalog.under(REPO_ROOT))

    assert in_use("graph", "research-pipeline")
    assert in_use("agent", "planner") and in_use("agent", "writer")
    assert not in_use("agent", "echo")
    assert not in_use("graph", "feed-monitor")


def test_a_draining_run_still_pins():
    assert run_backed(store_with("draining"), Catalog.under(REPO_ROOT))(
        "graph", "research-pipeline"
    )


def test_a_finished_run_releases():
    in_use = run_backed(store_with("completed"), Catalog.under(REPO_ROOT))

    assert not in_use("graph", "research-pipeline") and not in_use("agent", "planner")
