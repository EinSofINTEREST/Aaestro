"""HTTP surface for the Memory Service.

에이전트는 이 표면을 통해서만 메모리에 닿는다 — **컨테이너에 DB 자격증명을
주지 않기 위해서다** (09 Access Enforcement 1). 저장소 자격증명은 이 프로세스
안에만 있고, 에이전트는 runtime 이 발급한 불투명 토큰만 제시한다.

토큰은 서버가 보관한다: 페이로드를 클라이언트에 넘기면 space 목록과 mode 를
위조할 수 있다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import MemoryMode
from aaestro.http_errors import request_validation_error, status_for
from aaestro.memory.client import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
from aaestro.memory.entry import MemoryEntry
from aaestro.memory.gate import Gate, TokenGate, unauthorized
from aaestro.modules.memoryset import ChunkSpec, MemoryKind

if TYPE_CHECKING:
    from aaestro.memory.index import IndexRegistry
    from aaestro.memory.recall import Recall
    from aaestro.memory.service import AccessToken, MemoryService


ACCESS_DENIED: dict[str, int] = {ErrorCode.MEM_001: status.HTTP_401_UNAUTHORIZED}
"""이 서비스만의 매핑 — 토큰이 안 통한 것과 서버 장애를 클라이언트가 갈라야 한다."""


@dataclass
class TokenRegistry:
    """Opaque memory tokens the service hands out.

    발급한 토큰을 서비스가 기억합니다 — 페이로드를 클라이언트에 넘기면
    space 목록과 mode 를 위조할 수 있습니다.
    """

    _tokens: dict[str, AccessToken] = field(default_factory=dict, init=False)

    def issue(self, access: AccessToken) -> str:
        """Mint an opaque token for one agent's access.

        Args:
            access: The assembled access token.

        Returns:
            The opaque string the agent presents.
        """
        secret = token_urlsafe(32)
        self._tokens[secret] = access
        return secret

    def resolve(self, secret: str | None) -> AccessToken:
        """Look up the access behind a presented token.

        Raises:
            AaestroError: MEMORY/``MEM_001`` if the token is absent or unknown.
        """
        if not secret:
            raise unauthorized("memory token is required")
        access = self._tokens.get(secret)
        if access is None:
            raise unauthorized("memory token is not recognised")
        return access

    def forget(self, secret: str) -> None:
        """토큰을 폐기한다 — 그룹 이동이나 재배포 시 즉시 무효화한다."""
        self._tokens.pop(secret, None)


# 융합은 space 당 vector k + lexical k 개 후보를 만든다 — 상한이 없으면 요청 하나가
# 저장소 질의와 색인 작업량을 임의로 키운다
MAX_SEARCH_K = 200


class SearchRequest(BaseModel):
    """검색 요청."""

    model_config = ConfigDict(frozen=True)

    query: str
    spaces: tuple[str, ...] | None = None
    k: int = Field(default=6, ge=1, le=MAX_SEARCH_K)


class AppendRequest(BaseModel):
    """항목 추가 요청."""

    model_config = ConfigDict(frozen=True)

    space: str
    entry: MemoryEntry


class ReadRequest(BaseModel):
    """space 훑기 요청."""

    model_config = ConfigDict(frozen=True)

    space: str
    limit: int = 100
    kinds: tuple[MemoryKind, ...] | None = None


class LatestRequest(BaseModel):
    """정정 체인의 최신 항목 요청."""

    model_config = ConfigDict(frozen=True)

    space: str
    entry_id: str


def create_app(
    service: MemoryService,
    recall: Recall,
    tokens: TokenRegistry | None,
    *,
    indexer: IndexRegistry | None = None,
    chunk: ChunkSpec | None = None,
    gate: Gate | None = None,
) -> FastAPI:
    """Build the Memory Service HTTP app.

    메모리 서비스의 HTTP 표면을 만듭니다.

    Args:
        service: The framework-side memory gateway.
        recall: Hybrid search over the space indexes.
        tokens: Opaque token registry — ``gate`` 가 없을 때의 입구.
        indexer: Index registry the write path feeds — 없으면 저장만 하고
            색인하지 않습니다 (읽기 전용 배치용).
        chunk: Chunking policy for indexed entries.
        gate: 입구 — 레지스트리 판정을 쓰려면 ``RegistryGate`` 를 넘긴다.

    Returns:
        The FastAPI application.
    """
    spec = chunk or ChunkSpec()
    if gate is None:
        if tokens is None:
            raise ValueError("memory service needs either issued tokens or a gate")
        gate = TokenGate(tokens)
    entrance: Gate = gate
    app = FastAPI(title="Aaestro Memory Service")

    @app.exception_handler(RequestValidationError)
    async def _bad_body(_request: Request, err: RequestValidationError) -> JSONResponse:
        """FastAPI 기본 422 는 받은 값을 응답에 되비춘다 — 필드와 사유만 남긴다 (#448)."""
        problem = request_validation_error(err)
        return JSONResponse(
            status_code=status_for(problem), content=problem.payload().model_dump(mode="json")
        )

    @app.exception_handler(AaestroError)
    async def _structured(_request: Request, err: AaestroError) -> JSONResponse:
        """구조화 에러를 상태코드로 옮긴다 — 매핑은 공용 규칙을 따른다 (#234).

        접근 거부(``MEM_001``)만 **401** 로 덮는다: 이 서비스는 불투명 토큰으로
        접근을 가르므로, 클라이언트가 "토큰이 안 통했다" 와 "서버가 아프다" 를
        구분해야 재시도 판단이 갈린다 (09 Access Enforcement).
        """
        http_status = status_for(err, overrides=ACCESS_DENIED)
        return JSONResponse(status_code=http_status, content=err.payload().model_dump(mode="json"))

    async def granted(
        request: Request, aliases: list[str] | None, *, write: bool
    ) -> tuple[AccessToken, list[str]]:
        """Bearer 토큰 → 지금 닿을 수 있는 권한, 그리고 대상 별칭.

        헤더를 직접 읽는다 — ``Depends`` 를 인자 기본값에 두면 lint 가 막고,
        ``Annotated`` 로만 쓰면 FastAPI 가 dataclass 를 쿼리 파라미터로 읽는다.

        별칭을 지정하지 않으면(검색 전체) 지금 허용된 것만 대상이다 — 회수된 space 하나가
        검색 전체를 실패시키지 않는다. 지정했으면 거부된 별칭이 그대로 ``MEM_001`` 로 간다.
        """
        header = request.headers.get("authorization", "")
        prefix = "bearer "
        presented = header[len(prefix) :] if header.lower().startswith(prefix) else None
        declared = await entrance.admit(presented)
        wanted = aliases if aliases is not None else [s.alias for s in declared.spaces]
        narrowed = await entrance.narrow(presented or "", declared, wanted, write=write)
        if aliases is None:
            wanted = list(dict.fromkeys(s.alias for s in narrowed.spaces))
        return narrowed, wanted

    @app.post("/v1/search")
    async def search(request: SearchRequest, http_request: Request) -> list[dict[str, Any]]:
        """Search the spaces this agent may read.

        선언되지 않은 space 요청은 별칭 해석에서 ``MEM_001`` 로 거부됩니다.
        """
        token, aliases = await granted(
            http_request, list(request.spaces) if request.spaces else None, write=False
        )
        resolved = service.searchable(token, aliases)
        try:
            found = recall.search(request.query, spaces=resolved, k=request.k)
        except AaestroError:
            service.record_search(token, aliases, status="failed")
            raise
        service.record_search(token, aliases)
        return [
            {
                "entry": scored.entry.model_dump(mode="json"),
                "space": scored.space,
                "score": scored.score,
            }
            for scored in found
        ]

    @app.post("/v1/append")
    async def append(request: AppendRequest, http_request: Request) -> dict[str, Any]:
        """rw 권한이 있는 space 에만 추가합니다.

        색인은 **저장과 분리된 큐**로 넘깁니다 (09 Write Path) — 여기서
        embedding 을 기다리면 append 지연이 모델 호출만큼 길어집니다.
        """
        token, _ = await granted(http_request, [request.space], write=True)
        stored = service.append(token, request.space, request.entry)
        if indexer is not None:
            # 저장만 하고 색인하지 않으면 그 기억은 검색에서 영원히 사라진다
            indexer.submit(stored, spec)
        return stored.model_dump(mode="json")

    @app.post("/v1/read")
    async def read(request: ReadRequest, http_request: Request) -> list[dict[str, Any]]:
        """선언된 space 를 최신순으로 읽습니다 — 검색 없이 훑는 창구."""
        token, _ = await granted(http_request, [request.space], write=False)
        found = service.read(token, request.space, limit=request.limit, kinds=request.kinds)
        return [entry.model_dump(mode="json") for entry in found]

    @app.post("/v1/latest")
    async def latest(request: LatestRequest, http_request: Request) -> dict[str, Any] | None:
        """``supersedes`` 체인의 최신 항목 — 정정된 기억을 그대로 읽지 않기 위해서."""
        token, _ = await granted(http_request, [request.space], write=False)
        found = service.latest(token, request.space, request.entry_id)
        return found.model_dump(mode="json") if found is not None else None

    @app.get("/v1/spaces")
    async def spaces(http_request: Request) -> list[dict[str, str]]:
        """이 토큰이 닿을 수 있는 space — 에이전트가 자기 범위를 확인한다.

        광고하는 mode 는 **실제 권한**이다 (`may_write`). global 은 권한이
        `writers` 에서 나오고 `mode` 는 기본값 rw 로 남으므로, `mode` 를 그대로
        내보내면 쓸 수 없는 space 가 rw 로 보인다 — 에이전트가 그것을 믿고
        쓰기를 시도하면 401 을 받는다.
        """
        readable, aliases = await granted(http_request, None, write=False)
        writable, _ = await granted(http_request, aliases, write=True)
        can_write = {s.alias for s in writable.spaces if s.may_write(writable.agent)}
        return [
            {
                "alias": space.alias,
                "scope": str(space.scope),
                "mode": str(MemoryMode.RW if space.alias in can_write else MemoryMode.RO),
            }
            for space in readable.spaces
        ]

    return app


__all__ = [
    "MEMORY_TOKEN_ENV",
    "MEMORY_URL_ENV",
    "AppendRequest",
    "LatestRequest",
    "ReadRequest",
    "SearchRequest",
    "TokenRegistry",
    "create_app",
]
