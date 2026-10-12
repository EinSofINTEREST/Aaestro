"""Graph state schemas.

그래프 state pydantic 모델. 토폴로지의 ``state.schema`` ref 대상이다.
노드 산출물은 ``output_map`` 으로만 병합되며, 선언되지 않은 키는 반영되지 않는다.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PermissionsState(BaseModel):
    """State for the permissions graph — it runs no work, it only stands up the permission agent.

    권한 그래프의 state. 권한 에이전트는 A2A 요청만 받으므로 그래프 state 로 흐르는 값이 없다.
    """

    model_config = ConfigDict(frozen=True)


class ResearchState(BaseModel):
    """State for the reference research pipeline.

    레퍼런스 리서치 파이프라인의 state.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    plan: str | None = None
    needs_research: bool = True
    findings: list[str] = Field(default_factory=list)
    report: str | None = None


class FeedMonitorState(BaseModel):
    """State for the reference service graph.

    레퍼런스 상주 그래프의 state — iteration 간 연속성이 필요한 데이터를 담는다.
    """

    model_config = ConfigDict(frozen=True)

    feeds: list[str] = Field(default_factory=list)
    new_items: list[str] = Field(default_factory=list)
    seen_ids: list[str] = Field(default_factory=list)
    notified: int = 0


class MaintenanceState(BaseModel):
    """State for the memory maintenance service graph.

    메모리 유지보수 상주 그래프의 state. 프레임워크가 자기 메커니즘(에이전트 +
    그래프)으로 compaction 을 수행하므로, 대상 space 와 진행 상황이 iteration
    사이에 이어져야 한다.
    """

    model_config = ConfigDict(frozen=True)

    spaces: list[str] = Field(default_factory=list)
    """점검 대상 memory space 별칭."""

    pending_spaces: list[str] = Field(default_factory=list)
    """compaction trigger 에 도달해 압축이 필요한 space."""

    compacted: int = 0


class DraftReviewState(BaseModel):
    """State for the refinement-loop reference graph.

    재작업 순환 그래프의 state — 검토 의견이 다음 회차의 입력이 된다.
    """

    model_config = ConfigDict(frozen=True)

    query: str
    draft: str | None = None
    approved: bool = False
    notes: list[str] = Field(default_factory=list)
