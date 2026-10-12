"""The refinement-loop reference graph.

04 는 순환 edge 를 허용하되 `max_iterations` 를 요구하는데, **어느 레퍼런스
그래프에도 순환이 없었다** — 그 규칙이 배포에서 한 번도 검증되지 않았다.

이 그래프는 그 공백을 메우면서, 04 의 다른 주장도 시험한다: 새 목표가
**모듈 조립만으로** 구성되는가 (#210).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from aaestro.core.agent import TaskResult
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.orchestrator.builder import build_graph
from aaestro.orchestrator.topology import GraphTopology

REPO_ROOT = Path(__file__).resolve().parents[3]


def topology() -> GraphTopology:
    return GraphTopology.model_validate(
        yaml.safe_load((REPO_ROOT / "graphs" / "draft-review.yaml").read_text("utf-8"))
    )


class Reviewer:
    """정해진 회차만큼 반려한 뒤 통과시키는 runtime 대역."""

    def __init__(self, rejections: int) -> None:
        self._left = rejections
        self.visited: list[str] = []

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        self.visited.append(node.id)
        if node.id == "drafter":
            return TaskResult.completed(task, output={"draft": "초안"})
        if self._left > 0:
            self._left -= 1
            return TaskResult.completed(task, output={"approved": False, "notes": ["더 써라"]})
        return TaskResult.completed(task, output={"approved": True, "notes": []})


async def test_an_approved_draft_ends_the_run():
    """통과하면 순환을 벗어난다 — 아니면 영원히 돈다."""
    runtime = Reviewer(rejections=0)

    final = await build_graph(topology(), runtime).ainvoke({"query": "왜 하늘은 파란가"})

    assert final["approved"] is True
    assert runtime.visited == ["drafter", "reviewer"]


async def test_a_rejected_draft_is_rewritten():
    """검토 의견을 안고 다시 쓴다 — 이 재진입이 순환의 이유다."""
    runtime = Reviewer(rejections=1)

    final = await build_graph(topology(), runtime).ainvoke({"query": "q"})

    assert final["approved"] is True
    assert runtime.visited == ["drafter", "reviewer", "drafter", "reviewer"]


async def test_the_review_notes_reach_the_next_draft():
    """의견이 전달되지 않으면 다시 쓰는 의미가 없다."""
    seen: list[Any] = []

    class Recording(Reviewer):
        async def invoke(self, node: Any, task: Any) -> TaskResult:
            if node.id == "drafter":
                seen.append(task.input.get("notes"))
            return await super().invoke(node, task)

    await build_graph(topology(), Recording(rejections=1)).ainvoke({"query": "q"})

    # 첫 회차는 비어 있고, 두 번째는 검토 의견을 받는다
    assert seen[0] in (None, [], ())
    assert seen[1] == ["더 써라"]


async def test_an_endless_rejection_stops_at_the_bound():
    """04 는 순환에 `max_iterations` 를 요구한다 — 없으면 run 이 끝나지 않는다."""
    runtime = Reviewer(rejections=99)

    with pytest.raises(AaestroError) as excinfo:
        await build_graph(topology(), runtime).ainvoke({"query": "q"})

    assert excinfo.value.code == ErrorCode.GRAPH_004


def test_the_cycle_declares_its_bound():
    """미명시 순환은 배포 검증에서 막혀야 한다 — 이 그래프가 그 규칙의 사례다."""
    cycles = [
        edge
        for edge in topology().spec.edges
        if edge.source == "reviewer" and edge.target == "drafter"
    ]

    assert cycles, "재작업 순환이 없으면 이 그래프는 mission 파이프라인과 다르지 않다"
    assert all(edge.max_iterations for edge in cycles)
