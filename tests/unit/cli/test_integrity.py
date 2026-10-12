"""Unit tests for data integrity checks.

기록과 실체가 어긋나면 조용히 자원이 새거나 재개가 불가능해진다.
"""

from __future__ import annotations

from aaestro.cli.integrity import (
    dangling_module_refs,
    ghost_containers,
    orphan_checkpoints,
)

# --- checkpoint orphan ---------------------------------------------------------


def test_run_without_a_checkpoint_is_reported():
    """checkpoint 없는 run 은 재개가 불가능하다."""
    found = orphan_checkpoints(runs=["run-1"], checkpoints=[])

    assert [d.kind for d in found] == ["run_without_checkpoint"]
    assert found[0].subject == "run-1"


def test_checkpoint_without_a_run_is_reported():
    """아무도 참조하지 않는 저장 공간이다."""
    found = orphan_checkpoints(runs=[], checkpoints=["run-9"])

    assert [d.kind for d in found] == ["checkpoint_without_run"]


def test_matched_pairs_report_nothing():
    assert orphan_checkpoints(runs=["run-1"], checkpoints=["run-1"]) == ()


def test_orphans_are_reported_in_stable_order():
    """출력 순서가 흔들리면 diff 로 비교할 수 없다."""
    found = orphan_checkpoints(runs=["b", "a"], checkpoints=[])

    assert [d.subject for d in found] == ["a", "b"]


def test_both_directions_are_reported_together():
    found = orphan_checkpoints(runs=["only-run"], checkpoints=["only-ckpt"])

    assert {d.kind for d in found} == {"run_without_checkpoint", "checkpoint_without_run"}


# --- module ref ----------------------------------------------------------------


def test_unresolvable_deployed_ref_is_reported():
    """게시된 버전이 지워지면 재배포가 실패한다."""
    found = dangling_module_refs(
        {"research-pipeline": ["skillsets/gone@1.0.0"]}, ["skillsets/here@1.0.0"]
    )

    assert [d.kind for d in found] == ["dangling_module_ref"]
    assert "research-pipeline" in found[0].detail


def test_resolvable_refs_report_nothing():
    assert dangling_module_refs({"g": ["skillsets/here@1.0.0"]}, ["skillsets/here@1.0.0"]) == ()


def test_refs_from_multiple_graphs_are_all_reported():
    found = dangling_module_refs({"a": ["skillsets/x@1.0.0"], "b": ["skillsets/y@1.0.0"]}, [])

    assert {d.subject for d in found} == {"skillsets/x@1.0.0", "skillsets/y@1.0.0"}


# --- container ------------------------------------------------------------------


def test_ghost_container_is_reported():
    """유령 컨테이너는 자원을 쓰면서 아무도 관리하지 않는다."""
    found = ghost_containers(running=["stray"], known=[])

    assert [d.kind for d in found] == ["ghost_container"]


def test_missing_container_is_reported():
    """그래프가 호출할 대상이 없다는 뜻이다."""
    found = ghost_containers(running=[], known=["planner"])

    assert [d.kind for d in found] == ["missing_container"]


def test_matching_containers_report_nothing():
    assert ghost_containers(running=["planner"], known=["planner"]) == ()
