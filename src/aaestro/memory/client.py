"""HTTP-backed memory access for agents — the client side of the Memory Service.

에이전트 컨테이너는 이 클라이언트로만 메모리에 닿는다 — **DB 자격증명은
컨테이너에 들어가지 않는다** (09 Access Enforcement 1). 접근 범위는 runtime 이
발급한 불투명 토큰이 정하므로, 클라이언트는 자기 권한을 알 필요가 없다.

이 모듈은 agentd 가 import 한다 — 서버 코드(store · service · gate …)를 끌어오지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.entry import MemoryEntry
from aaestro.memory.recall import (
    ScoredEntry,
    apply_budget,
    render_context,
    resolve_corrections,
    superseded_ids,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.modules.memoryset import RecallSpec

MEMORY_TOKEN_ENV = "AAESTRO_MEMORY_TOKEN"  # noqa: S105 — env 키 이름이지 값이 아니다
MEMORY_URL_ENV = "AAESTRO_MEMORY_URL"
"""agentd 가 서비스에 닿기 위해 받는 값 — secrets 와 같은 주입 경로다."""

DEFAULT_TIMEOUT_S = 30.0


def _service_error(status: int, detail: str) -> AaestroError:
    """서비스 응답을 구조화 에러로 옮긴다 — 거부와 장애를 구분한다."""
    denied = status in (401, 403)
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_001 if denied else ErrorCode.MEM_004,
        message=detail or "memory service rejected the request",
        retryable=not denied and status >= 500,
        details={"status": status},
    )


@dataclass
class HttpMemoryAccess:
    """The ``MemoryAccess`` implementation talking to the Memory Service.

    ``AgentContext.memory`` 에 주입되는 클라이언트. 토큰을 헤더로 실어 보내고,
    서비스가 space 경계를 강제합니다.
    """

    base_url: str
    token: str
    client: httpx.AsyncClient | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    _owned: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    def _http(self) -> httpx.AsyncClient:
        """요청에 쓸 클라이언트 — 주입하지 않으면 **하나를 만들어 계속 쓴다**.

        요청마다 만들고 닫으면 auto-recall 과 ``memory_search`` 마다 커넥션을 새로 맺는다
        (#321). 만든 것은 이 인스턴스가 소유하고 :meth:`aclose` 가 닫는다.
        """
        if self.client is not None:
            return self.client
        if self._owned is None:
            self._owned = httpx.AsyncClient(timeout=self.timeout_s)
        return self._owned

    async def aclose(self) -> None:
        """Close the client this instance created — an injected client stays with its owner.

        스스로 만든 클라이언트만 닫습니다. 주입된 클라이언트는 주입한 쪽이 닫습니다.
        """
        if self._owned is not None:
            await self._owned.aclose()
            self._owned = None

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        """서비스에 요청하고 실패를 구조화 에러로 옮긴다.

        모든 호출이 이 하나를 지납니다 — 메서드마다 따로 변환하면 한쪽이
        빠져 같은 장애가 어떤 창구를 썼는지에 따라 다른 타입으로 나옵니다.
        """
        http = self._http()
        try:
            response = await http.request(
                method,
                f"{self.base_url.rstrip('/')}{path}",
                json=payload,
                headers={"authorization": f"Bearer {self.token}"},
            )
        except httpx.HTTPError as err:
            raise AaestroError(
                category=ErrorCategory.MEMORY,
                code=ErrorCode.MEM_004,
                message="memory service is unreachable",
                retryable=True,
                details={"cause": type(err).__name__},
            ) from err

        if response.status_code >= 400:
            raise _service_error(response.status_code, _detail(response))
        return response.json()

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST 요청 — 변환은 ``_request`` 가 맡는다."""
        return await self._request("POST", path, payload)

    async def search(self, query: str, **kwargs: Any) -> list[ScoredEntry]:
        """Search the spaces this agent may read.

        접근 범위는 **서비스가** 강제합니다 — 클라이언트가 경계를 다시 구현하면
        두 곳이 갈라집니다.

        Args:
            query: The search text.
            **kwargs: ``spaces`` / ``k`` / ``scan``.

        Returns:
            Scored entries, best-first.
        """
        payload: dict[str, Any] = {"query": query}
        for key in ("spaces", "k", "scan"):
            if key in kwargs and kwargs[key] is not None:
                payload[key] = kwargs[key]

        found = await self._post("/v1/search", payload)
        return [
            ScoredEntry(
                entry=MemoryEntry.model_validate(item["entry"]),
                score=item["score"],
                space=item["space"],
            )
            for item in found
        ]

    async def recall_for_task(self, query: str, *, policy: RecallSpec) -> str:
        """Recall once for a task entry, ready for prompt injection.

        태스크 진입 시 1회 회상해 프롬프트에 붙일 텍스트를 만듭니다.

        **검색은 서비스가 한다**: 인덱스는 서비스 쪽에 있으므로 여기서는
        정책(threshold / budget)만 적용합니다 — 경계를 클라이언트가 다시
        구현하면 두 곳이 갈라집니다.

        Args:
            query: The task text to recall against.
            policy: The memoryset recall policy (k / min_score / budget_tokens).

        Returns:
            The rendered context, or an empty string when auto-recall is off or
            nothing survived the threshold.
        """
        if not policy.auto:
            return ""

        found = await self.search(query, k=policy.k)
        # 정정 체인은 최신 항목만 주입한다 (09 Rule 4)
        current = resolve_corrections(found, superseded_ids(scored.entry for scored in found))
        selected = apply_budget(
            current, min_score=policy.min_score, budget_tokens=policy.budget_tokens
        )
        return render_context(selected)

    async def append(self, space: str, **kwargs: Any) -> MemoryEntry:
        """rw 권한이 있는 space 에만 추가합니다 — 아니면 ``MEM_001``."""
        entry: MemoryEntry = kwargs["entry"]
        stored = await self._post(
            "/v1/append", {"space": space, "entry": entry.model_dump(mode="json")}
        )
        return MemoryEntry.model_validate(stored)

    async def read(self, space: str, **kwargs: Any) -> list[MemoryEntry]:
        """Read a declared space, newest first.

        선언된 space 를 최신순으로 읽습니다 — 검색 없이 훑는 창구입니다.

        Args:
            space: The space alias to read.
            **kwargs: ``limit`` / ``kinds``.

        Returns:
            Stored entries, newest first.
        """
        payload: dict[str, Any] = {"space": space}
        for key in ("limit", "kinds"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]

        found = await self._post("/v1/read", payload)
        return [MemoryEntry.model_validate(item) for item in found]

    async def latest(self, space: str, entry_id: str) -> MemoryEntry | None:
        """정정 체인의 최신 항목 — 대체된 기억을 그대로 읽지 않기 위해서."""
        found = await self._post("/v1/latest", {"space": space, "entry_id": entry_id})
        return MemoryEntry.model_validate(found) if found is not None else None

    async def spaces(self) -> Sequence[dict[str, str]]:
        """이 에이전트가 닿을 수 있는 space — 자기 범위를 확인하는 창구."""
        listed: list[dict[str, str]] = await self._request("GET", "/v1/spaces")
        return listed


def _detail(response: httpx.Response) -> str:
    """응답 본문에서 사유를 뽑는다 — 없으면 빈 문자열."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(body, dict):
        return str(body.get("detail") or body.get("message") or "")
    return ""


__all__ = ["DEFAULT_TIMEOUT_S", "MEMORY_TOKEN_ENV", "MEMORY_URL_ENV", "HttpMemoryAccess"]
