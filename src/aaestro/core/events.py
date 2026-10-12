"""Streaming task events.

스트리밍 실행에서 발행되는 이벤트 모델.
A2A 스트리밍 이벤트와 1:1 매핑을 유지한다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from aaestro.core.agent import ModelUsage, TaskStatus
from aaestro.core.errors import AaestroErrorPayload


class EventType(StrEnum):
    """이벤트 종류."""

    TOKEN = "token"  # noqa: S105 - 모델 출력 토큰, 자격증명 아님
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    DONE = "done"
    ERROR = "error"


class _BaseEvent(BaseModel):
    """이벤트 공통 필드."""

    model_config = ConfigDict(frozen=True)

    task_id: str
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TokenEvent(_BaseEvent):
    """모델이 생성한 토큰 조각."""

    type: Literal[EventType.TOKEN] = EventType.TOKEN
    text: str


class ToolCallEvent(_BaseEvent):
    """Tool 호출 개시 — ``tool`` 은 네임스페이스 포함 이름."""

    type: Literal[EventType.TOOL_CALL] = EventType.TOOL_CALL
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    turn: int = 0


class ToolResultEvent(_BaseEvent):
    """Tool 실행 결과."""

    type: Literal[EventType.TOOL_RESULT] = EventType.TOOL_RESULT
    tool: str
    result: Any = None
    duration_ms: int = 0
    turn: int = 0
    error: AaestroErrorPayload | None = None


class DoneEvent(_BaseEvent):
    """태스크 정상 종료."""

    type: Literal[EventType.DONE] = EventType.DONE
    status: TaskStatus = TaskStatus.COMPLETED
    output: dict[str, Any] = Field(default_factory=dict)
    usage: ModelUsage = Field(default_factory=ModelUsage)


class ErrorEvent(_BaseEvent):
    """태스크 실패 — 구조화 에러 payload 를 실어 보낸다."""

    type: Literal[EventType.ERROR] = EventType.ERROR
    error: AaestroErrorPayload


TaskEvent = Annotated[
    TokenEvent | ToolCallEvent | ToolResultEvent | DoneEvent | ErrorEvent,
    Field(discriminator="type"),
]
"""스트리밍 이벤트 유니온 — ``type`` 필드로 판별한다."""
