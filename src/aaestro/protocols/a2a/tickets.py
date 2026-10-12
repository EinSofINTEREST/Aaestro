"""Call tickets — how a caller proves who it is to one callee (#281).

per-edge token 은 그래프의 모든 에이전트가 **같은 서명 키**를 쥐고 만든다 — 어느 에이전트든 다른
에이전트 행세로 토큰을 만들 수 있어 피호출자 쪽 검사가 경계가 되지 못한다. 레지스트리 모드에서는
호출자가 **자기 신원**으로 피호출자 하나에만 쓰는 표를 받고, 피호출자가 **자기 신원**으로 그 표를
레지스트리에 확인한다. 호출자 신원 자체는 피호출자에게 넘어가지 않는다 (03 Enforcement).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from aaestro.core.errors import ErrorCode
from aaestro.protocols.a2a.errors import a2a_error, unreachable

TICKET_HEADER = "x-aaestro-a2a-ticket"
RENEW_BEFORE_S = 30.0
"""만료 직전의 표로 부르면 피호출자에 닿을 때 만료될 수 있다 — 이만큼 남으면 새로 받는다."""
REQUEST_TIMEOUT_S = 5.0


@dataclass
class TicketSource:
    """Fetch and reuse tickets for the peers this agent calls.

    Attributes:
        agent: 이 에이전트 — 에러의 출처.
        base_url: control plane 주소.
        credential: 이 에이전트의 신원 — 레지스트리에만 보낸다.
    """

    agent: str
    base_url: str
    credential: str
    http: httpx.AsyncClient | None = None
    clock: Callable[[], float] = time.time
    _held: dict[str, tuple[str, float]] = field(default_factory=dict, init=False)
    _fetching: dict[str, asyncio.Lock] = field(default_factory=dict, init=False)

    async def ticket_for(self, callee: str) -> str:
        """A live ticket for ``callee``.

        Raises:
            AaestroError: A2A/``A2A_002`` (retryable) if the registry is unreachable,
                ``A2A_004`` if the registry refuses this agent or does not know the callee.
        """
        held = self._held.get(callee)
        if held is not None and held[1] - self.clock() > RENEW_BEFORE_S:
            return held[0]
        # 동시에 처음 부르면 모두가 새 표를 받아 레지스트리가 간선마다 남기는 수를 넘긴다 —
        # 넘친 옛 표는 지워져 진행 중 호출이 거부된다. 한 번만 받고 나눠 쓴다
        async with self._fetching.setdefault(callee, asyncio.Lock()):
            held = self._held.get(callee)
            if held is not None and held[1] - self.clock() > RENEW_BEFORE_S:
                return held[0]
            return await self._fetch(callee)

    async def _fetch(self, callee: str) -> str:
        try:
            response = await self._client().post(
                "/v1/access/a2a/tickets",
                json={"callee": callee},
                headers={"Authorization": f"Bearer {self.credential}"},
            )
        except httpx.TransportError as err:
            raise unreachable(self.agent, callee, reason="access registry unreachable") from err
        if response.status_code >= 500:
            raise unreachable(self.agent, callee, reason="access registry unavailable")
        if response.status_code != 201:
            # 신원이 폐기됐거나 없는 피호출자 — 다시 받아도 같다
            raise a2a_error(
                ErrorCode.A2A_004,
                f"a2a call ticket refused: {self.agent} -> {callee}",
                caller=self.agent,
                callee=callee,
                status=response.status_code,
            )
        body = response.json()
        self._held[callee] = (str(body["ticket"]), float(body["expires_at"]))
        return self._held[callee][0]

    async def aclose(self) -> None:
        if self.http is not None:
            await self.http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.http = httpx.AsyncClient(base_url=self.base_url, timeout=REQUEST_TIMEOUT_S)
        return self.http


__all__ = ["RENEW_BEFORE_S", "TICKET_HEADER", "TicketSource"]
