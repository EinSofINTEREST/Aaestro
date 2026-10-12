"""Unit tests for the agentd startup sequence.

부분 기동 금지가 이 계층의 핵심 계약이다 — 필수 자원이 빠진 채 Ready 가 되면
모델이 없는 tool 을 부르다 런타임에 실패한다.
"""

from __future__ import annotations

import pytest

from aaestro.agentd.bootstrap import (
    Bootstrap,
    BootstrapResult,
    build_tool_registry,
)
from aaestro.core.agent import HealthState
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.core.skill import SkillSpec
from aaestro.core.tools import namespaced
from tests.fixtures.builders import make_manifest


class FakeLoader:
    """ref 로 스크립트된 객체를 돌려주는 로더 대역.

    스크립트가 없으면 tool 이 없는 빈 스킬셋을 돌려준다 — bare object 를 주면
    registry 구성 단계에서 AttributeError 로 터진다.
    """

    def __init__(self, results: dict[str, object] | None = None) -> None:
        self._results = results or {}
        self.loaded: list[str] = []

    def load(self, ref: str) -> object:
        self.loaded.append(ref)
        item = self._results.get(ref)
        if isinstance(item, Exception):
            raise item
        return item if item is not None else FakeSkillset(ref, [])


class FakeSkillset:
    """tool 스펙만 제공하는 스킬셋 대역."""

    def __init__(self, ref: str, names: list[str]) -> None:
        self.ref = ref
        self._specs = tuple(
            SkillSpec(name=n, description="d", parameters={"type": "object"}) for n in names
        )

    def tools(self) -> tuple[SkillSpec, ...]:
        return self._specs


class FakeMcp:
    """MCP 서버 기동을 스크립트하는 launcher 대역."""

    def __init__(self) -> None:
        self._tools: dict[str, list[str]] = {}
        self._errors: dict[str, Exception] = {}
        self.started: list[str] = []
        self.timeouts: list[float] = []

    def script(self, name: str, tools: list[str]) -> FakeMcp:
        self._tools[name] = tools
        return self

    def fail(self, name: str, error: Exception | None = None) -> FakeMcp:
        self._errors[name] = error or RuntimeError("server did not start")
        return self

    async def start(self, spec, *, timeout_s: float):
        self.started.append(spec.name)
        self.timeouts.append(timeout_s)
        error = self._errors.get(spec.name)
        if error is not None:
            raise error
        return self._tools.get(spec.name, [])

    def schemas(self) -> dict[str, dict]:
        """네임스페이스가 붙은 tool 이름 → input schema (``McpLauncher`` 계약)."""
        from aaestro.core.tools import namespaced

        return {
            namespaced(server, tool): {"type": "object", "properties": {"path": {}}}
            for server, tools in self._tools.items()
            for tool in tools
        }


def manifest_with(**spec_extra):
    """mcp/skillsets 만 바꿔 쓰는 manifest."""
    return make_manifest(
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            **spec_extra,
        }
    )


def stdio(name: str, *, optional: bool = False, allowed: list[str] | None = None):
    """stdio MCP 서버 선언."""
    spec: dict[str, object] = {
        "name": name,
        "transport": "stdio",
        "command": ["mcp-server"],
    }
    if optional:
        spec["optional"] = True
    if allowed is not None:
        spec["allowed_tools"] = allowed
    return spec


def bootstrap(manifest, *, mcp=None, promptsets=None, skillsets=None, timeout=15.0):
    """대역을 물린 Bootstrap."""
    return Bootstrap(
        manifest,
        promptset_loader=promptsets or FakeLoader(),
        skillset_loader=skillsets or FakeLoader(),
        mcp_launcher=mcp,
        mcp_timeout_s=timeout,
    )


# --- tool registry ----------------------------------------------------------


def test_mcp_tools_are_namespaced():
    """MCP tool 은 mcp__{server}__{tool} — skillset tool 과 충돌 방지."""
    assert namespaced("filesystem", "read_file") == "mcp__filesystem__read_file"


def test_registry_merges_skillset_and_mcp_tools():
    registry = build_tool_registry(
        [FakeSkillset("skillsets/web@0.1.0", ["search"])],
        {"fs": ("read_file",)},
        agent="a",
    )

    assert set(registry) == {"search", "mcp__fs__read_file"}


def test_duplicate_skillset_tool_names_are_rejected():
    """두 스킬셋이 같은 tool 이름을 주장하면 모델이 어느 쪽을 부를지 모호해진다."""
    with pytest.raises(AaestroError) as exc_info:
        build_tool_registry(
            [
                FakeSkillset("skillsets/a@0.1.0", ["search"]),
                FakeSkillset("skillsets/b@0.1.0", ["search"]),
            ],
            {},
            agent="a",
        )

    assert exc_info.value.code == "MOD_002"
    assert exc_info.value.details["tool"] == "search"


def test_duplicate_mcp_tool_names_are_rejected():
    with pytest.raises(AaestroError) as exc_info:
        build_tool_registry([], {"fs": ("read", "read")}, agent="a")

    assert exc_info.value.code == "MOD_002"


def test_namespacing_prevents_skillset_mcp_collision():
    """같은 이름이어도 네임스페이스 덕에 공존한다."""
    registry = build_tool_registry(
        [FakeSkillset("skillsets/a@0.1.0", ["read_file"])],
        {"fs": ("read_file",)},
        agent="a",
    )

    assert set(registry) == {"read_file", "mcp__fs__read_file"}


# --- 기동 시퀀스 ------------------------------------------------------------


async def test_startup_loads_modules_in_order():
    promptsets, skillsets = FakeLoader(), FakeLoader()
    manifest = manifest_with(skillsets=[{"ref": "skillsets/web-search@0.2.0"}])

    await bootstrap(manifest, promptsets=promptsets, skillsets=skillsets).run()

    assert promptsets.loaded == ["promptsets/test@0.1.0"]
    assert skillsets.loaded == ["skillsets/web-search@0.2.0"]


async def test_required_mcp_failure_aborts_startup():
    """부분 기동 금지 — 필수 서버가 실패하면 Ready 로 가지 않는다."""
    manifest = manifest_with(mcp={"servers": [stdio("filesystem")]})

    with pytest.raises(AaestroError) as exc_info:
        await bootstrap(manifest, mcp=FakeMcp().fail("filesystem")).run()

    assert exc_info.value.code == "MCP_001"
    assert exc_info.value.category is ErrorCategory.MCP
    assert exc_info.value.details["mcp_server"] == "filesystem"


async def test_optional_mcp_failure_continues_startup():
    """optional 서버 실패는 기동을 막지 않는다 (03 Startup Sequence)."""
    mcp = FakeMcp().script("fs", ["read"]).fail("browser")
    manifest = manifest_with(mcp={"servers": [stdio("fs"), stdio("browser", optional=True)]})

    result = await bootstrap(manifest, mcp=mcp).run()

    assert "fs" in result.mcp_servers
    assert result.degraded == ("browser",)


async def test_optional_failure_reports_degraded_health():
    """빠진 optional 자원이 health 에 드러나야 운영자가 안다."""
    manifest = manifest_with(mcp={"servers": [stdio("browser", optional=True)]})

    result = await bootstrap(manifest, mcp=FakeMcp().fail("browser")).run()

    assert result.health().status is HealthState.DEGRADED


async def test_healthy_startup_reports_healthy():
    manifest = manifest_with(mcp={"servers": [stdio("fs")]})

    result = await bootstrap(manifest, mcp=FakeMcp().script("fs", ["read"])).run()

    assert result.health().status is HealthState.HEALTHY


async def test_mcp_startup_uses_the_declared_timeout():
    """서버당 15s 상한 (03 Startup Sequence 3)."""
    mcp = FakeMcp().script("fs", [])
    manifest = manifest_with(mcp={"servers": [stdio("fs")]})

    await bootstrap(manifest, mcp=mcp, timeout=15.0).run()

    assert mcp.timeouts == [15.0]


async def test_allowed_tools_filters_what_is_bound():
    """서버가 노출하는 전체를 무조건 바인딩하지 않는다 (03 Tool Filtering)."""
    mcp = FakeMcp().script("fs", ["read_file", "write_file", "list_directory"])
    manifest = manifest_with(
        mcp={"servers": [stdio("fs", allowed=["read_file", "list_directory"])]}
    )

    result = await bootstrap(manifest, mcp=mcp).run()

    assert result.mcp_servers["fs"] == ("read_file", "list_directory")
    assert "mcp__fs__write_file" not in result.tools


async def test_no_allowed_tools_binds_everything():
    mcp = FakeMcp().script("fs", ["read_file", "write_file"])
    manifest = manifest_with(mcp={"servers": [stdio("fs")]})

    result = await bootstrap(manifest, mcp=mcp).run()

    assert result.mcp_servers["fs"] == ("read_file", "write_file")


async def test_startup_without_mcp_launcher_is_allowed():
    """MCP 를 선언하지 않은 에이전트도 기동된다."""
    result = await bootstrap(manifest_with()).run()

    assert result.mcp_servers == {}


async def test_module_load_failure_propagates():
    """로더가 보고한 MOD_* 를 그대로 전파한다 — 재변환하지 않는다."""
    error = AaestroError(category=ErrorCategory.MODULE, code="MOD_001", message="cannot resolve")

    with pytest.raises(AaestroError) as exc_info:
        await bootstrap(
            manifest_with(), promptsets=FakeLoader({"promptsets/test@0.1.0": error})
        ).run()

    assert exc_info.value.code == "MOD_001"


async def test_registry_includes_skillset_and_mcp_tools():
    mcp = FakeMcp().script("fs", ["read_file"])
    manifest = manifest_with(
        skillsets=[{"ref": "skillsets/web-search@0.2.0"}],
        mcp={"servers": [stdio("fs")]},
    )
    skillsets = FakeLoader(
        {"skillsets/web-search@0.2.0": FakeSkillset("skillsets/web-search@0.2.0", ["search"])}
    )

    result = await bootstrap(manifest, mcp=mcp, skillsets=skillsets).run()

    assert set(result.tools) == {"search", "mcp__fs__read_file"}


def test_empty_result_reports_healthy():
    assert BootstrapResult().health().status is HealthState.HEALTHY


# --- memory_search 배선 -------------------------------------------------------


async def test_startup_registers_memory_search_for_agents_with_memory():
    """파라미터만 만들고 기동 경로에 넘기지 않으면 실제로는 등록되지 않는다."""
    from aaestro.memory.tool import MEMORY_SEARCH_TOOL

    manifest = manifest_with(
        memory={"spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]}
    )

    result = await bootstrap(manifest).run()

    assert MEMORY_SEARCH_TOOL in result.tools


async def test_startup_omits_memory_search_without_declared_spaces():
    from aaestro.memory.tool import MEMORY_SEARCH_TOOL

    result = await bootstrap(manifest_with()).run()

    assert MEMORY_SEARCH_TOOL not in result.tools
