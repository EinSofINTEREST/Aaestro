"""Tool execution registry tests.

이름이 곧 출처 판별이다 — MCP 는 네임스페이스 접두사로 구분되고, 그 구분에
재시도·알림 전략이 걸려 있다 (05 Layer Rules).
"""

from __future__ import annotations

import pytest

from aaestro.agentd.tools import AgentToolRegistry
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.skill import SkillContext, SkillSpec
from aaestro.memory.tool import MEMORY_SEARCH_TOOL

SPEC = SkillSpec(
    name="search",
    description="검색한다",
    parameters={"type": "object", "properties": {"query": {"type": "string"}}},
)


class LoadedSkill:
    """로드된 skill 대역 — 실제 계약은 spec/fn/timeout_s 다."""

    def __init__(self, spec: SkillSpec, fn, timeout_s: float = 0.0) -> None:
        self.spec = spec
        self.fn = fn
        self.timeout_s = timeout_s


class Skillset:
    def __init__(self, *skills: LoadedSkill) -> None:
        self.skills = skills


class FakeMcp:
    """``McpClient`` 대역 — 미등록 tool 은 실제 클라이언트처럼 MCP_002 로 거부한다."""

    def __init__(self, known: tuple[str, ...] = ("mcp__filesystem__read_file",)) -> None:
        self._known = known
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name: str, arguments):
        if name not in self._known:
            raise AaestroError(
                category=ErrorCategory.MCP,
                code=ErrorCode.MCP_002,
                message=f"unknown tool: {name}",
            )
        self.calls.append((name, dict(arguments)))
        return "mcp-result"


class FakeMemory:
    async def search(self, query: str, **kwargs):
        return []


def ctx() -> SkillContext:
    return SkillContext(agent="researcher", task_id="t-1", run_id="r-1")


async def echo_skill(_ctx, query: str) -> str:
    return f"found:{query}"


def registry(**overrides):
    return AgentToolRegistry(
        agent="researcher",
        skillsets=overrides.pop("skillsets", [Skillset(LoadedSkill(SPEC, echo_skill))]),
        **overrides,
    )


# --- 라우팅 ------------------------------------------------------------------


async def test_skillset_tool_calls_the_loaded_function():
    result = await registry().call("search", {"query": "sidecar"}, ctx())

    assert result == "found:sidecar"


async def test_mcp_tool_is_delegated_to_the_session():
    mcp = FakeMcp()

    result = await registry(mcp=mcp).call("mcp__filesystem__read_file", {"path": "a"}, ctx())

    assert result == "mcp-result"
    assert mcp.calls == [("mcp__filesystem__read_file", {"path": "a"})]


async def test_memory_search_is_routed_to_the_framework_tool():
    result = await registry(memory=FakeMemory()).call(
        MEMORY_SEARCH_TOOL, {"query": "sidecar"}, ctx()
    )

    assert result == []


# --- 미등록 tool --------------------------------------------------------------


async def test_unknown_skillset_tool_is_mod_001():
    """모델이 본 적 없는 이름을 지어내면 그대로 드러나야 한다."""
    with pytest.raises(AaestroError) as exc_info:
        await registry().call("absent", {}, ctx())

    assert exc_info.value.code == ErrorCode.MOD_001


async def test_unknown_mcp_tool_reaches_the_session_which_rejects_it():
    """등록 여부 판단은 세션 소관이다 — registry 가 먼저 가로채면 이중 판정이 된다."""
    with pytest.raises(AaestroError) as exc_info:
        await registry(mcp=FakeMcp()).call("mcp__absent__read", {}, ctx())

    assert exc_info.value.code == ErrorCode.MCP_002


async def test_mcp_tool_without_a_session_is_mcp_002():
    with pytest.raises(AaestroError) as exc_info:
        await registry().call("mcp__filesystem__read_file", {}, ctx())

    assert exc_info.value.code == ErrorCode.MCP_002


async def test_memory_search_without_memory_is_rejected():
    """memory 가 없는데 등록만 되어 있으면 조용히 빈 결과를 주면 안 된다."""
    with pytest.raises(AaestroError):
        await registry().call(MEMORY_SEARCH_TOOL, {"query": "q"}, ctx())


# --- 상한 --------------------------------------------------------------------


def test_declared_timeout_is_reported():
    skillset = Skillset(LoadedSkill(SPEC, echo_skill, timeout_s=12.0))

    assert registry(skillsets=[skillset]).timeout_for("search") == 12.0


def test_undeclared_timeout_leaves_the_choice_to_the_caller():
    """0 이면 호출자가 기본값을 고른다 — 여기서 정하면 설정이 무시된다."""
    assert registry().timeout_for("search") == 0.0
    assert registry().timeout_for("mcp__fs__read") == 0.0


# --- 도메인 예외는 그대로 --------------------------------------------------------


async def test_skill_domain_errors_propagate_unchanged():
    """skill 은 도메인 예외를 그대로 던진다 — 변환은 executor 경계가 한다 (04)."""

    async def failing(_ctx, query: str) -> str:
        raise ValueError("도메인 실패")

    skillset = Skillset(LoadedSkill(SPEC, failing))

    with pytest.raises(ValueError, match="도메인 실패"):
        await registry(skillsets=[skillset]).call("search", {"query": "q"}, ctx())
