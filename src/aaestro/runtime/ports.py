"""A2A port allocation from the runtime's configured range.

Runtime 이 설정된 범위에서 A2A 포트를 할당한다.

03 Port Assignment 는 포트를 **runtime 이** 준다고 규정한다 — manifest 는
포트를 선언하지 않고, 에이전트 코드는 주입된 값을 읽을 뿐이다. 그 "runtime 이
준다"를 실제로 수행하는 곳이 여기다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

log = structlog.get_logger(__name__)


def _key(agent: str, replica: int) -> str:
    """할당 단위는 에이전트가 아니라 **레플리카**다.

    02 는 동일 manifest 의 N replica 기동을 허용한다 — 이름만으로 잡으면
    두 번째 레플리카가 첫 번째의 포트를 물려받아 둘 다 깨진다.
    """
    return f"{agent}/{replica}"


@dataclass
class A2APortAllocator:
    """Hands out A2A ports from a range, and takes them back.

    범위에서 A2A 포트를 내주고, 회수한다.

    Attributes:
        port_range: Inclusive ``(low, high)`` range this allocator owns —
            ``A2AConfig.port_range`` 가 그대로 들어온다.
        assigned: Currently held ports, keyed by ``agent/replica``.
    """

    port_range: tuple[int, int]
    assigned: dict[str, int] = field(default_factory=dict)

    def allocate(self, agent: str, *, replica: int = 0) -> int:
        """Reserve a port for one agent replica.

        레플리카 하나에 포트를 배정합니다. 같은 레플리카를 다시 요청하면
        **이미 준 포트를 그대로** 돌려줍니다 — 재기동이 범위를 갉아먹지
        않게 합니다.

        Args:
            agent: The agent name.
            replica: Replica index — 같은 이름의 다른 레플리카는 다른 포트를 받습니다.

        Returns:
            The reserved port.

        Raises:
            AaestroError: RUNTIME/``RT_001`` if the range is exhausted —
                조용히 겹쳐 주면 두 에이전트가 같은 포트를 열어 한쪽이
                기동에 실패하고, 원인이 포트라는 것이 드러나지 않는다.
        """
        key = _key(agent, replica)
        held = self.assigned.get(key)
        if held is not None:
            return held

        low, high = self.port_range
        taken = frozenset(self.assigned.values())
        for port in range(low, high + 1):
            if port in taken:
                continue
            self.assigned[key] = port
            log.debug("a2a port allocated", agent=agent, port=port)
            return port

        raise AaestroError(
            category=ErrorCategory.RUNTIME,
            code=ErrorCode.RT_001,
            message="a2a port range exhausted",
            agent=agent,
            details={
                "port_range": [low, high],
                "assigned": len(self.assigned),
                "replica": replica,
            },
        )

    def release(self, agent: str, *, replica: int = 0) -> None:
        """Return one replica's port to the range.

        레플리카의 포트를 범위로 돌려줍니다. 미할당 레플리카에 대해서는
        아무 일도 하지 않습니다 — 정지 경로가 할당 여부를 따로 기억하지
        않아도 되게 합니다.
        """
        self.assigned.pop(_key(agent, replica), None)


__all__ = ["A2APortAllocator"]
