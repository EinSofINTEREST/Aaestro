"""Memory entry model.

메모리 항목 모델. **append-only** 가 이 계층의 계약이다 — 항목을 고치는 대신
``supersedes`` 로 새 항목을 쓴다. 과거 기록이 사후에 바뀌면 provenance 가
거짓말이 된다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from aaestro.modules.memoryset import MemoryKind

MAX_CONTENT_BYTES = 8 * 1024
"""``content`` 상한 — 초과분은 artifact 저장소에 두고 artifact_ref 로 참조한다."""


class MemorySource(BaseModel):
    """Where an entry came from.

    항목의 출처. provenance 없는 기억은 저장할 수 없다 — 나중에 그 기억을
    믿어도 되는지 판단할 근거가 사라진다.
    """

    model_config = ConfigDict(frozen=True)

    agent: str
    run_id: str | None = None
    task_id: str | None = None
    node_id: str | None = None


class MemoryEntry(BaseModel):
    """One stored memory.

    저장된 기억 하나. 검색/임베딩 대상은 ``content`` 다.
    """

    model_config = ConfigDict(frozen=True)

    entry_id: str = Field(default_factory=lambda: str(uuid4()))
    space: str
    kind: MemoryKind
    content: str
    tags: tuple[str, ...] = ()
    source: MemorySource
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    supersedes: str | None = None

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        """시각은 tz-aware 여야 한다.

        naive 시각이 섞이면 provenance 가 어느 시간대의 것인지 모호해지고,
        aware 시각과의 뺄셈이 ``TypeError`` 로 터진다 — 인덱싱 지연 관측이
        저장소 데이터 때문에 실패하면 안 된다.

        Raises:
            ValueError: If the timestamp has no timezone.
        """
        if value.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return value

    def to_row(self) -> dict[str, Any]:
        """저장소 행 표현 — tags 는 검색 필터가 쓰도록 구분자로 이어 붙인다."""
        return {
            "entry_id": self.entry_id,
            "space": self.space,
            "kind": str(self.kind),
            "content": self.content,
            "tags": "\x1f".join(self.tags),
            "agent": self.source.agent,
            "run_id": self.source.run_id,
            "task_id": self.source.task_id,
            "node_id": self.source.node_id,
            "created_at": self.created_at.isoformat(),
            "importance": self.importance,
            "supersedes": self.supersedes,
        }

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> MemoryEntry:
        """저장소 행에서 항목을 복원한다."""
        return cls(
            entry_id=row["entry_id"],
            space=row["space"],
            kind=MemoryKind(row["kind"]),
            content=row["content"],
            tags=tuple(t for t in str(row["tags"]).split("\x1f") if t),
            source=MemorySource(
                agent=row["agent"],
                run_id=row["run_id"],
                task_id=row["task_id"],
                node_id=row["node_id"],
            ),
            created_at=datetime.fromisoformat(row["created_at"]),
            importance=row["importance"],
            supersedes=row["supersedes"],
        )


__all__ = ["MAX_CONTENT_BYTES", "MemoryEntry", "MemorySource"]
