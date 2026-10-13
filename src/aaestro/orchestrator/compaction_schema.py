"""Checks a compaction declaration against the graph state schema.

압축 선언과 state 스키마의 대조. 인라인 ``fields`` 는 토폴로지 모델이 읽을 때 닫지만,
``schema`` ref 는 모델을 import 해야 필드 타입을 알 수 있다 — 배포 검증과 run 시작이
같은 판정을 쓰도록 여기 한 곳에 둔다.
"""

from __future__ import annotations

import types
from collections.abc import Mapping
from typing import Any, Final, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter, ValidationError

from aaestro.orchestrator.compaction import CompactionPolicy, SummarizePolicy, summary_entry

_PROBE_REF: Final = "artifact://probe/probe"


def _list_item_annotation(annotation: Any) -> Any | None:
    """리스트 필드면 그 annotation, 아니면 None — ``list[str] | None`` 도 리스트로 본다."""
    if get_origin(annotation) in (Union, types.UnionType):
        candidates = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(candidates) != 1:
            return None
        annotation = candidates[0]
    if annotation is list or get_origin(annotation) is list:
        return annotation
    return None


def list_fields(schema: type[BaseModel]) -> frozenset[str]:
    """state 스키마의 리스트 필드 이름 — 압축 대상이 될 수 있는 것들."""
    return frozenset(
        name
        for name, declared in schema.model_fields.items()
        if _list_item_annotation(declared.annotation) is not None
    )


def compaction_problems(
    policies: Mapping[str, CompactionPolicy], schema: type[BaseModel]
) -> list[str]:
    """Check the compaction declaration against the resolved state schema.

    압축 선언을 해석된 state 스키마와 대조합니다 — 인라인 ``fields`` 는 토폴로지 모델이
    이미 닫지만, ``schema`` ref 는 모델을 import 해야 필드 타입을 알 수 있습니다.

    Returns:
        One message per violation; empty when the declaration is sound.
    """
    problems: list[str] = []
    for name, policy in policies.items():
        declared = schema.model_fields.get(name)
        if declared is None:
            problems.append(f"compaction targets an undeclared state field: {name}")
            continue
        annotation = _list_item_annotation(declared.annotation)
        if annotation is None:
            problems.append(f"compaction targets a non-array state field: {name}")
            continue
        if isinstance(policy, SummarizePolicy) and not _admits_summary(annotation):
            # 요약 항목은 객체다 — list[str] 필드에 넣으면 다음 state 검증이 GRAPH_003 으로 깨진다
            problems.append(f"summarize needs a list field whose items may be objects: {name}")
    return problems


def _admits_summary(annotation: Any) -> bool:
    """이 리스트 타입이 요약 항목(객체)을 담을 수 있는지."""
    probe = [summary_entry("probe", artifact_ref=_PROBE_REF, replaced=1)]
    try:
        TypeAdapter(annotation).validate_python(probe)
    except ValidationError:
        return False
    return True


__all__ = ["compaction_problems", "list_fields"]
