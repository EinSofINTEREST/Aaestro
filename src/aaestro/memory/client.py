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
from pydantic import ValidationError

from aaestro.core.errors import AaestroError, AaestroErrorPayload, ErrorCategory, ErrorCode
from aaestro.memory.entry import MemoryEntry, draft_from
from aaestro.memory.recall import (
    ScoredEntry,
    apply_budget,
    render_context,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.modules.memoryset import RecallSpec

MEMORY_TOKEN_ENV = "AAESTRO_MEMORY_TOKEN"  # noqa: S105 — env 키 이름이지 값이 아니다
MEMORY_URL_ENV = "AAESTRO_MEMORY_URL"
"""agentd 가 서비스에 닿기 위해 받는 값 — secrets 와 같은 주입 경로다."""
MEMORY_TOKENS_PATH_ENV = "AAESTRO_MEMORY_TOKENS_PATH"
"""memory 서비스가 에이전트 토큰을 내놓는 파일 — 오케스트레이터가 읽어 에이전트에 나눠 준다."""

DEFAULT_TIMEOUT_S = 30.0


MAX_REPORTED_PROBLEMS = 10
"""형식 거절 하나에서 옮기는 위치 수의 상한 — 본문이 길어도 에러가 그만큼 길어지지 않는다."""

MAX_MESSAGE_CHARS = 500
MAX_DETAIL_KEYS = 16
MAX_DETAIL_CHARS = 200
"""복원하는 payload 의 상한 — 응답 본문의 크기가 에러 · 로그의 크기가 되지 않게 한다."""

INVALID_REQUEST_STATUSES = frozenset({400, 422})
DENIED_STATUSES = frozenset({401, 403})
TRANSIENT_STATUSES = frozenset({408, 429})
SERVER_ERROR_FLOOR = 500


def _service_error(response: httpx.Response, *, agent: str | None) -> AaestroError:
    """서비스의 실패 응답을 구조화 에러로 옮긴다.

    서비스가 에러 payload 로 답했으면 **검증한 뒤 복원한다** — 상태 코드만 보고 다시 분류하면
    서버가 정한 code · category 가 사라져 호출자의 잘못 (4xx) 이 장애로 보인다. 다만 응답은
    네트워크 너머에서 온 것이다 (프록시, 오설정 URL 의 다른 서비스): 아는 code 만 받고, 글과
    details 는 상한 안에서만 옮기고, ``agent`` · ``task_id`` 는 본문 것을 쓰지 않는다.

    payload 가 아닌 응답 (프레임워크의 형식 거절, 프록시의 오류 페이지) 은 상태 코드로 가른다.
    그때 본문의 글은 싣지 않는다 — 형식 거절의 본문은 거절된 입력을 되돌려 준다.
    """
    status = response.status_code
    body = _json(response)
    payload = _error_payload(body)
    if payload is not None:
        return AaestroError(
            category=payload.category,
            code=payload.code,
            message=payload.message[:MAX_MESSAGE_CHARS],
            agent=agent,
            # 4xx 는 호출자가 고쳐야 하는 것이다 — 본문이 다시 걸라고 해도 같은 요청은 같은 답이다
            retryable=payload.retryable and status >= SERVER_ERROR_FLOOR,
            details=_bounded(payload.details),
        )
    return _by_status(status, body, agent=agent)


def _by_status(status: int, body: Any, *, agent: str | None) -> AaestroError:
    """payload 없는 실패를 상태 코드로 가른다."""
    if status in INVALID_REQUEST_STATUSES:
        return AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="memory service rejected the request",
            agent=agent,
            details={"status": status, "problems": _problems(body)},
        )
    if status in DENIED_STATUSES:
        return AaestroError(
            category=ErrorCategory.MEMORY,
            code=ErrorCode.MEM_001,
            message="memory service denied the request",
            agent=agent,
            details={"status": status},
        )
    # 그 밖의 4xx (404 · 405 …) 는 요청 형식이 아니라 닿은 곳이 틀린 것이다 (오설정 URL · 프록시) —
    # 호출자 입력의 잘못으로도, 다시 걸면 풀릴 장애로도 읽히면 안 된다
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=ErrorCode.MEM_004,
        message="memory service failed",
        agent=agent,
        retryable=status >= SERVER_ERROR_FLOOR or status in TRANSIENT_STATUSES,
        details={"status": status},
    )


def _error_payload(body: Any) -> AaestroErrorPayload | None:
    """본문이 **이 프레임워크의** 에러 payload 인지 — 아니면 None.

    모양만 맞는 본문은 받지 않는다: ``code`` 가 아는 코드여야 한다 (``category`` 는 모델이 enum
    으로 검증한다).
    """
    if not isinstance(body, dict):
        return None
    try:
        payload = AaestroErrorPayload.model_validate(body)
    except ValidationError:
        return None
    if payload.code not in KNOWN_CODES:
        return None
    return payload


KNOWN_CODES = frozenset(code.value for code in ErrorCode)


def _bounded(details: dict[str, Any]) -> dict[str, Any]:
    """payload 의 details 에서 짧은 스칼라 항목만 옮긴다 — 중첩 값과 긴 글은 버린다."""
    kept: dict[str, Any] = {}
    for key, value in details.items():
        if len(kept) >= MAX_DETAIL_KEYS:
            break
        if len(key) > MAX_DETAIL_CHARS:
            continue
        if isinstance(value, str) and len(value) > MAX_DETAIL_CHARS:
            continue
        if value is None or isinstance(value, str | int | float | bool):
            kept[key] = value
    return kept


def _json(response: httpx.Response) -> Any:
    """응답 본문을 JSON 으로 읽는다 — JSON 이 아니면 None."""
    try:
        return response.json()
    except ValueError:
        return None


def _problems(body: Any) -> list[dict[str, str]]:
    """형식 거절 본문에서 **필드 위치와 오류 종류만** 뽑는다.

    ``input`` (거절된 값) 과 ``msg`` (값을 인용할 수 있는 글) 는 옮기지 않는다.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    if not isinstance(detail, list):
        return []
    problems: list[dict[str, str]] = []
    for item in detail[:MAX_REPORTED_PROBLEMS]:
        if not isinstance(item, dict):
            continue
        loc = item.get("loc")
        parts = loc if isinstance(loc, list) else []
        problems.append(
            {
                "loc": ".".join(str(part) for part in parts if isinstance(part, str | int)),
                "type": str(item.get("type", "")),
            }
        )
    return problems


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
    agent: str | None = None
    """이 클라이언트를 쓰는 에이전트 — 에러의 ``agent`` 에 싣는다 (응답 본문의 것은 쓰지 않는다)."""
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
            raise _service_error(response, agent=self.agent)
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
            **kwargs: ``spaces`` / ``k``.

        Returns:
            Scored entries, best-first.
        """
        payload: dict[str, Any] = {"query": query}
        for key in ("spaces", "k"):
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
        # 정정으로 대체된 항목은 서비스의 검색이 이미 걸러 보낸다 (09 Rule 4)
        selected = apply_budget(
            found, min_score=policy.min_score, budget_tokens=policy.budget_tokens
        )
        return render_context(selected)

    async def append(self, space: str, **kwargs: Any) -> MemoryEntry:
        """Append to a writable space — the service issues the id and provenance.

        rw 권한이 있는 space 에만 추가합니다 — 아니면 ``MEM_001``.

        Args:
            space: The space alias to write to.
            **kwargs: ``MemoryDraft`` 의 필드 — ``kind`` · ``content`` · ``tags`` ·
                ``importance`` · ``supersedes`` · ``source`` (run · task · node).

        Returns:
            The stored entry, with the id the service issued.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` before any request if a field is unknown
                (``entry_id`` 포함) or malformed.
        """
        draft = draft_from(kwargs, agent=self.agent)
        stored = await self._post(
            "/v1/append", {"space": space, "entry": draft.model_dump(mode="json")}
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


__all__ = [
    "DEFAULT_TIMEOUT_S",
    "MEMORY_TOKENS_PATH_ENV",
    "MEMORY_TOKEN_ENV",
    "MEMORY_URL_ENV",
    "HttpMemoryAccess",
]
