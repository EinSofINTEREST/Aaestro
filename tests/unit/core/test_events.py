"""Unit tests for streaming task event models."""

from __future__ import annotations

from pydantic import TypeAdapter

from aaestro.core.agent import ModelUsage, TaskStatus
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.events import (
    DoneEvent,
    ErrorEvent,
    EventType,
    SteerEvent,
    TaskEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)

_adapter: TypeAdapter[TaskEvent] = TypeAdapter(TaskEvent)


def test_event_types_cover_documented_units():
    """이벤트 단위: token / tool_call / tool_result / steer / done / error — 02 stream 계약."""
    assert {e.value for e in EventType} == {
        "token",
        "tool_call",
        "tool_result",
        "steer",
        "done",
        "error",
    }


def test_token_event_discriminates():
    event = _adapter.validate_python({"task_id": "t1", "type": "token", "text": "hi"})

    assert isinstance(event, TokenEvent)
    assert event.text == "hi"


def test_tool_call_event_discriminates():
    event = _adapter.validate_python(
        {"task_id": "t1", "type": "tool_call", "tool": "mcp__fs__read_file", "turn": 2}
    )

    assert isinstance(event, ToolCallEvent)
    assert event.tool == "mcp__fs__read_file"
    assert event.turn == 2


def test_steer_event_discriminates_and_survives_the_wire():
    """/stream 은 JSON 으로 흘린다 — 소비자가 판별 유니온으로 다시 읽을 수 있어야 한다."""
    sent = SteerEvent(task_id="t1", text="narrow it down", by="alice", turn=3)

    received = _adapter.validate_json(sent.model_dump_json())

    assert isinstance(received, SteerEvent)
    assert (received.text, received.by, received.turn) == ("narrow it down", "alice", 3)


def test_tool_result_event_carries_error_payload():
    payload = AaestroError(
        category=ErrorCategory.MCP, code=ErrorCode.MCP_003, message="tool call failed"
    ).payload()

    event = ToolResultEvent(task_id="t1", tool="x", error=payload, duration_ms=12)

    assert isinstance(_adapter.validate_python(event.model_dump()), ToolResultEvent)
    assert event.error is not None
    assert event.error.code == "MCP_003"


def test_done_event_defaults_to_completed():
    event = DoneEvent(task_id="t1", output={"report": "x"}, usage=ModelUsage(input_tokens=3))

    assert event.status is TaskStatus.COMPLETED
    assert event.usage.input_tokens == 3


def test_error_event_requires_payload():
    payload = AaestroError(
        category=ErrorCategory.MODEL, code=ErrorCode.LLM_005, message="max turns exceeded"
    ).payload()

    event = _adapter.validate_python({"task_id": "t1", "type": "error", "error": payload})

    assert isinstance(event, ErrorEvent)
    assert event.error.code == "LLM_005"
