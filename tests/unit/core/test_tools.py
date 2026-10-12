"""Tool namespace contract tests.

접두사 정의가 흩어지면 하나만 바뀌었을 때 **조용히 갈라진다** — MCP tool 실패가
``SKILL_001`` 로 잘못 변환되고, 메트릭의 출처 라벨도 함께 틀어진다.
이 테스트는 정의가 하나뿐임을 실제 사용처로 확인한다.
"""

from __future__ import annotations

import pytest

from aaestro.agentd.telemetry import SOURCE_MCP, SOURCE_SKILLSET, tool_source
from aaestro.core.tools import MCP_TOOL_PREFIX, is_mcp_tool, namespaced, split_namespaced


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("mcp__fs__read_file", True),
        ("mcp__fs", True),
        ("read_file", False),
        ("", False),
    ],
)
def test_is_mcp_tool_reads_the_prefix(name, expected):
    assert is_mcp_tool(name) is expected


def test_namespaced_round_trips_through_split():
    """만드는 쪽과 푸는 쪽이 같은 규약을 써야 한다."""
    name = namespaced("filesystem", "read_file")

    assert split_namespaced(name) == ("filesystem", "read_file")


def test_every_consumer_follows_a_changed_prefix(monkeypatch):
    """접두사를 바꾸면 모든 사용처가 함께 따라가야 한다.

    정의가 여러 곳에 있으면 이 테스트가 실패한다 — 한쪽만 바뀌기 때문이다.
    """
    monkeypatch.setattr("aaestro.core.tools.MCP_TOOL_PREFIX", "xmcp__")

    # 판별 / 생성 / 분해 / 메트릭 라벨 — 네 사용처가 모두 새 접두사를 본다
    assert is_mcp_tool("xmcp__fs__read")
    assert not is_mcp_tool(f"{MCP_TOOL_PREFIX}fs__read")
    assert namespaced("fs", "read") == "xmcp__fs__read"
    assert split_namespaced("xmcp__fs__read") == ("fs", "read")
    assert tool_source("xmcp__fs__read") == SOURCE_MCP
    assert tool_source("mcp__fs__read") == SOURCE_SKILLSET


def test_tool_error_routing_follows_the_same_prefix(monkeypatch):
    """05 의 재시도·알림 전략이 이 구분에 걸려 있다."""
    from aaestro.agentd.executor import _tool_error
    from aaestro.core.errors import ErrorCode
    from tests.fixtures.builders import make_task

    monkeypatch.setattr("aaestro.core.tools.MCP_TOOL_PREFIX", "xmcp__")
    task = make_task()

    mcp = _tool_error("xmcp__fs__read", task, "researcher", RuntimeError("boom"))
    skill = _tool_error("mcp__fs__read", task, "researcher", RuntimeError("boom"))

    assert mcp.code == ErrorCode.MCP_003
    assert skill.code == ErrorCode.SKILL_001
