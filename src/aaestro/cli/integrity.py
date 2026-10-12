"""Data integrity checks.

기록과 실체가 어긋나면 조용히 자원이 새거나 재개가 불가능해진다 —
정기 점검으로 드러낸다 (05 Data Integrity).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Discrepancy:
    """One mismatch between records and reality.

    기록과 실체의 불일치 하나.
    """

    kind: str
    subject: str
    detail: str


def orphan_checkpoints(runs: Iterable[str], checkpoints: Iterable[str]) -> tuple[Discrepancy, ...]:
    """Find runs and checkpoints that lost their counterpart.

    짝을 잃은 run 기록과 checkpoint 를 찾습니다.

    checkpoint 없는 run 은 **재개가 불가능**하고, run 없는 checkpoint 는
    아무도 참조하지 않는 저장 공간입니다.

    Args:
        runs: Recorded run ids.
        checkpoints: Run ids that have a checkpoint.

    Returns:
        The discrepancies, sorted for stable output.
    """
    recorded, stored = set(runs), set(checkpoints)

    found = [
        Discrepancy(
            kind="run_without_checkpoint",
            subject=run_id,
            detail="run cannot be resumed",
        )
        for run_id in sorted(recorded - stored)
    ]
    found.extend(
        Discrepancy(
            kind="checkpoint_without_run",
            subject=run_id,
            detail="checkpoint is unreferenced",
        )
        for run_id in sorted(stored - recorded)
    )
    return tuple(found)


def dangling_module_refs(
    deployed: Mapping[str, Iterable[str]], resolvable: Iterable[str]
) -> tuple[Discrepancy, ...]:
    """Find deployed refs the registry can no longer resolve.

    배포된 그래프가 참조하지만 registry 가 더 이상 해석하지 못하는 ref 를
    찾습니다 — 게시된 버전이 지워졌다는 뜻이므로 재배포가 실패합니다.

    Args:
        deployed: Graph name to the module refs it uses.
        resolvable: Refs the registry currently resolves.

    Returns:
        The discrepancies.
    """
    available = set(resolvable)
    return tuple(
        Discrepancy(
            kind="dangling_module_ref",
            subject=ref,
            detail=f"referenced by graph '{graph}'",
        )
        for graph, refs in sorted(deployed.items())
        for ref in sorted(set(refs) - available)
    )


def ghost_containers(running: Iterable[str], known: Iterable[str]) -> tuple[Discrepancy, ...]:
    """Find containers the runtime does not account for.

    runtime 이 모르는 컨테이너와, 알지만 떠 있지 않은 에이전트를 찾습니다.

    유령 컨테이너는 자원을 쓰면서 아무도 관리하지 않고, 반대쪽은 그래프가
    호출할 대상이 없다는 뜻입니다.

    Args:
        running: Agent names with a live container.
        known: Agent names the runtime tracks.

    Returns:
        The discrepancies.
    """
    live, tracked = set(running), set(known)

    found = [
        Discrepancy(
            kind="ghost_container",
            subject=agent,
            detail="container is running but untracked",
        )
        for agent in sorted(live - tracked)
    ]
    found.extend(
        Discrepancy(
            kind="missing_container",
            subject=agent,
            detail="agent is tracked but not running",
        )
        for agent in sorted(tracked - live)
    )
    return tuple(found)


__all__ = [
    "Discrepancy",
    "dangling_module_refs",
    "ghost_containers",
    "orphan_checkpoints",
]
