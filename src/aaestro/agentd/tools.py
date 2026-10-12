"""Tool execution registry.

이름으로 tool 을 찾아 실행한다. 세 출처가 하나의 계약 뒤에 모인다:

- **skillset tool** — 로드된 함수를 직접 호출
- **MCP tool** — 소유 세션으로 위임 (``mcp__{server}__{tool}``)
- **framework tool** — ``memory_search`` / ``ask_peer`` 처럼 프레임워크가 제공

실패는 출처에 맞는 코드로 변환한다 (05 Layer Rules): 재시도·알림 전략이
출처에 따라 다르기 때문이다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.tools import is_mcp_tool
from aaestro.memory.tool import MEMORY_SEARCH_TOOL, run_memory_search
from aaestro.protocols.a2a.tool import ASK_PEER_TOOL, run_ask_peer

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from aaestro.core.agent import MemoryAccess
    from aaestro.core.skill import SkillContext
    from aaestro.modules.skillset import LoadedSkillset
    from aaestro.protocols.a2a.client import A2AClient
    from aaestro.protocols.mcp.client import McpClient


def unknown_tool(name: str, agent: str) -> AaestroError:
    """등록되지 않은 tool 호출 — 모델이 본 적 없는 이름을 지어낸 경우다."""
    return AaestroError(
        category=ErrorCategory.MCP if is_mcp_tool(name) else ErrorCategory.MODULE,
        code=ErrorCode.MCP_002 if is_mcp_tool(name) else ErrorCode.MOD_001,
        message=f"unknown tool: {name}",
        agent=agent,
        details={"tool": name},
    )


@dataclass
class AgentToolRegistry:
    """The ``ToolRegistry`` implementation agentd serves.

    기동이 만든 자원을 실행 가능한 registry 로 묶습니다. 어느 출처의 tool 인지는
    **이름이 정합니다** — MCP 는 네임스페이스 접두사로 구분됩니다.
    """

    agent: str
    skillsets: Sequence[LoadedSkillset] = ()
    mcp: McpClient | None = None
    memory: MemoryAccess | None = None
    peers: A2AClient | None = None
    """peer 위임 창구 — 미주입 시 `ask_peer` 는 등록되지 않은 tool 이다."""
    _skills: dict[str, Any] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        for skillset in self.skillsets:
            for skill in skillset.skills:
                self._skills[skill.spec.name] = skill

    def timeout_for(self, name: str) -> float:
        """선언된 tool 상한 — 미선언이면 0 이고 호출자가 기본값을 고른다."""
        skill = self._skills.get(name)
        return skill.timeout_s if skill is not None else 0.0

    async def call(self, name: str, arguments: Mapping[str, Any], ctx: SkillContext) -> Any:
        """Execute one tool.

        tool 을 실행합니다. 실패는 **호출자(executor)가 출처에 맞는 코드로**
        변환하므로, 여기서는 도메인 예외를 그대로 던집니다 (04 Skill Rules 5).

        Args:
            name: The tool name as the model called it.
            arguments: Arguments the model supplied.
            ctx: Skill context — 로거와 자원 접근 창구입니다.

        Returns:
            The tool result.

        Raises:
            AaestroError: MCP/``MCP_002`` or MODULE/``MOD_001`` if the name is
                not registered — 모델이 본 적 없는 이름을 지어낸 경우입니다.
        """
        if name == MEMORY_SEARCH_TOOL:
            if self.memory is None:
                raise unknown_tool(name, self.agent)
            return await run_memory_search(self.memory, dict(arguments))

        if name == ASK_PEER_TOOL:
            if self.peers is None:
                raise unknown_tool(name, self.agent)
            return await run_ask_peer(self.peers, ctx, dict(arguments))

        if is_mcp_tool(name):
            if self.mcp is None:
                raise unknown_tool(name, self.agent)
            return await self.mcp.call_tool(name, arguments)

        skill = self._skills.get(name)
        if skill is None:
            raise unknown_tool(name, self.agent)
        return await skill.fn(ctx, **arguments)


__all__ = ["AgentToolRegistry", "unknown_tool"]
