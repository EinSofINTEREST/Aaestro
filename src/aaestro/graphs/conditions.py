"""Conditional edge functions.

조건부 간선의 조건 함수. 토폴로지의 ``edges[].condition`` ref 대상이며,
state 를 읽고 분기 여부만 판정한다 (부수효과 금지).
"""

from __future__ import annotations

from typing import Any


def needs_research(state: dict[str, Any]) -> bool:
    """계획 결과가 추가 리서치를 요구하는지."""
    return bool(state.get("needs_research", False))


def plan_only(state: dict[str, Any]) -> bool:
    """계획만으로 목표가 충족되는지 — ``needs_research`` 의 여집합."""
    return not needs_research(state)


def has_new_items(state: dict[str, Any]) -> bool:
    """감시 대상에 신규 항목이 있는지."""
    return bool(state.get("new_items"))


def idle(state: dict[str, Any]) -> bool:
    """처리할 작업이 없는지 — service 그래프의 idle 분기 조건."""
    return not has_new_items(state)


def needs_compaction(state: dict[str, Any]) -> bool:
    """compaction 이 필요한 space 가 남아있는지 — 유지보수 그래프의 분기 조건."""
    return bool(state.get("pending_spaces"))


def maintenance_idle(state: dict[str, Any]) -> bool:
    """압축할 space 가 없는지 — ``needs_compaction`` 의 여집합."""
    return not needs_compaction(state)


def draft_approved(state: dict[str, Any]) -> bool:
    """검토를 통과했는지 — 통과하면 순환을 벗어난다."""
    return bool(state.get("approved"))


def needs_revision(state: dict[str, Any]) -> bool:
    """다시 써야 하는지 — 04 는 이 순환에 max_iterations 를 요구한다."""
    return not draft_approved(state)
