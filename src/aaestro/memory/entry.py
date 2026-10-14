"""Memory entry model.

메모리 항목 모델. **append-only** 가 이 계층의 계약이다 — 항목을 고치는 대신
``supersedes`` 로 새 항목을 쓴다. 과거 기록이 사후에 바뀌면 provenance 가
거짓말이 된다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Annotated, Any, Final
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictStr,
    ValidationError,
    field_validator,
)

from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    field_problems,
    raise_detached,
)
from aaestro.core.identifiers import MAX_IDENTIFIER_CHARS, identifier_problem
from aaestro.modules.memoryset import MemoryKind

if TYPE_CHECKING:
    from collections.abc import Mapping

MAX_CONTENT_BYTES: Final = 8 * 1024
"""``content`` 상한 (UTF-8 바이트) — 초과분은 artifact 저장소에 두고 artifact_ref 로 참조한다.

초안이 먼저 거절하고 (``VAL_002``) 저장소가 같은 상수로 한 번 더 막는다."""

MAX_TAG_CHARS: Final = 64
MAX_TAGS: Final = 32
"""태그 하나의 길이 · 한 항목의 태그 수 상한 — 태그는 필터 키로 저장소 행과 색인에 그대로 실린다."""

DEFAULT_IMPORTANCE: Final = 0.5
IMPORTANCE_RANGE = Field(ge=0.0, le=1.0)
"""``importance`` 의 범위 — 저장된 항목과 초안이 같은 범위를 쓴다."""


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
    importance: Annotated[float, IMPORTANCE_RANGE] = DEFAULT_IMPORTANCE
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


def _identifier(value: str | None) -> str | None:
    """호출자가 고른 식별자를 **성질로** 검증한다 — 규칙은 ``core.identifiers`` 하나다.

    사유의 이름만 메시지에 싣는다 — 값은 싣지 않는다.
    """
    if value is None:
        return None
    problem = identifier_problem(value)
    if problem is not None:
        raise ValueError(f"identifier is invalid: {problem.value}")
    return value


class DraftSource(BaseModel):
    """The caller's context for a new entry — never its identity.

    새 항목의 호출자 문맥. **에이전트 신원은 여기 없다** — 출처의 ``agent`` 는 서비스가 토큰에서
    채운다 (09 Entry Rules 4). 호출자가 고를 수 있으면 다른 에이전트의 기억으로 위조된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: StrictStr | None = None
    task_id: StrictStr | None = None
    node_id: StrictStr | None = None

    @field_validator("run_id", "task_id", "node_id")
    @classmethod
    def _well_formed(cls, value: str | None) -> str | None:
        return _identifier(value)


class MemoryDraft(BaseModel):
    """What a caller may say about a new entry — content only.

    호출자가 새 항목에 대해 말할 수 있는 것. ``entry_id`` · ``created_at`` · 출처의 신원은
    **서비스가 발급한다** (#456): ``entry_id`` 는 space 를 넘는 전역 키라, 호출자가 고르면 다른
    space 의 id 존재를 상태 코드로 알아내거나 남이 쓸 id 를 선점할 수 있다. 모르는 키는
    거부한다 — 옛 클라이언트가 보낸 ``entry_id`` 를 조용히 버리면 호출자는 자기가 고른 id 로
    정정하려다 실패한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: MemoryKind
    content: StrictStr
    tags: Annotated[tuple[StrictStr, ...], Field(max_length=MAX_TAGS)] = ()
    importance: Annotated[StrictFloat, IMPORTANCE_RANGE] = DEFAULT_IMPORTANCE
    supersedes: StrictStr | None = None
    source: DraftSource = DraftSource()

    @field_validator("supersedes")
    @classmethod
    def _well_formed(cls, value: str | None) -> str | None:
        return _identifier(value)

    @field_validator("content")
    @classmethod
    def _within_cap(cls, value: str) -> str:
        """저장소와 같은 상한 — 넘으면 저장 장애 (500) 가 아니라 호출자 잘못 (``VAL_002``) 이다."""
        if len(value.encode("utf-8")) > MAX_CONTENT_BYTES:
            raise ValueError("content exceeds the size cap")
        return value

    @field_validator("tags")
    @classmethod
    def _well_formed_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """태그는 식별자와 같은 성질 — 저장소의 구분자 (``\x1f``) 는 출력 불가라 자연히 막힌다."""
        for tag in value:
            problem = identifier_problem(tag, max_chars=MAX_TAG_CHARS)
            if problem is not None:
                raise ValueError(f"tag is invalid: {problem.value}")
        return value

    def issue(
        self, *, space: str, agent: str, run_id: str | None, created_at: datetime
    ) -> MemoryEntry:
        """Turn the draft into a stored entry with a service-issued id and provenance.

        서비스가 정한 값 (space id · 신원 · run · 시각) 과 새 id 로 항목을 만든다. ``run_id`` 는
        호출자의 값이 아니라 토큰이 아는 run 이다 — 일치 검사는 서비스가 먼저 한다.
        """
        return MemoryEntry(
            entry_id=str(uuid4()),
            space=space,
            kind=self.kind,
            content=self.content,
            tags=self.tags,
            source=MemorySource(
                agent=agent,
                run_id=run_id,
                task_id=self.source.task_id,
                node_id=self.source.node_id,
            ),
            created_at=created_at,
            importance=self.importance,
            supersedes=self.supersedes,
        )


def draft_from(fields: Mapping[str, Any], *, agent: str | None = None) -> MemoryDraft:
    """Validate append keyword arguments into a draft — the one shape every client accepts.

    ``append(space, **fields)`` 의 인자를 초안으로 검증한다. HTTP 클라이언트와 in-process 어댑터가
    같은 함수를 지나므로 같은 입력이 같은 결과를 낸다. 거절은 서비스의 형식 거절과 같은 ``VAL_002``
    이고, 받은 값은 싣지 않는다 (``field_problems``).

    Raises:
        AaestroError: VALIDATION/``VAL_002`` if a field is unknown or malformed.
    """
    try:
        return MemoryDraft.model_validate(dict(fields))
    except ValidationError as err:
        raise_detached(
            AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message="memory append failed validation",
                agent=agent,
                details={"errors": field_problems(err)},
            )
        )


__all__ = [
    "DEFAULT_IMPORTANCE",
    "IMPORTANCE_RANGE",
    "MAX_CONTENT_BYTES",
    "MAX_IDENTIFIER_CHARS",
    "MAX_TAGS",
    "MAX_TAG_CHARS",
    "DraftSource",
    "MemoryDraft",
    "MemoryEntry",
    "MemorySource",
    "draft_from",
]
