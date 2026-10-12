"""Remote MCP termination — tools decided one by one, credentials held by the proxy (#282).

에이전트는 원격 MCP 서버를 프록시의 ``/mcp/{server}`` 로 부르고 자격 자리에 **자기 신원**을 싣는다.
프록시는:

1. 신원으로 에이전트를 알고, **그 에이전트의 선언**에서 서버 주소와 자격 이름을 찾는다 — 주소를
   에이전트가 고르지 못한다 (프록시의 자격을 임의의 서버로 보내지 못하게)
2. 서버 호스트를 ``egress`` 로, ``tools/call`` 마다 도구를 ``mcp_tool`` (``server/tool``) 로
   판정한다
3. 신원을 떼고 운영자가 허용한 자격만 붙여, 확인한 주소로 보낸다

그래서 한 도구를 회수해도 같은 서버의 다른 도구는 계속 되고, 자격은 에이전트 env 에 없다.

에이전트 전용 사이드카(#304)도 같은 길이다. 주소는 runtime 이 띄운 이름에서 나오고, 사이드카는
에이전트마다의 사이드카 네트워크에 있어 프록시만 닿는다. 바깥 목적지가 아니므로 ``egress`` 판정은
없고 도구마다의 ``mcp_tool`` 판정만 있다. 자격을 싣지 않으므로(선언이 ``auth`` 를 막는다) 평문이다 —
다만 사설 주소로 풀릴 때만 보낸다.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from aaestro.access.baselines import url_target
from aaestro.access.client import DecisionSource
from aaestro.access.model import ResourceKind
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import McpTransport, sidecar_url
from aaestro.egress.connect import UNKNOWN_IDENTITY, EgressMode, is_public, resolve

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from aaestro.access.client import Verdict
    from aaestro.catalog import Catalog

log = structlog.get_logger(__name__)

MCP_PREFIX = "/mcp"
SESSION_HEADER = "mcp-session-id"
SESSION_MAC_CHARS = 32
TOOLS_CALL = "tools/call"
DENIED_RPC_CODE = -32001
"""JSON-RPC 서버 정의 에러 대역 — 도구 판정 거부. 메시지에 aaestro 코드를 싣는다."""

UPSTREAM_TIMEOUT = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
"""읽기는 끝을 두지 않는다 — MCP 의 서버 알림 스트림(GET)은 세션 내내 열려 있다."""

_FORWARDED = frozenset(
    {"content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id"}
)
_RETURNED_EXCLUDED = frozenset(
    {"connection", "keep-alive", "transfer-encoding", "content-length", "server", "date"}
)


@dataclass(frozen=True)
class McpUpstream:
    """Where one agent's declared server lives, and what the proxy adds."""

    url: str
    target: str
    token: str | None
    sidecar: bool = False
    """에이전트 전용 사이드카 — 바깥 목적지가 아니라 egress 판정이 없다."""


@dataclass(frozen=True)
class McpUpstreams:
    """Resolves (agent, server) from the declarations — never from the request.

    Attributes:
        catalog: 선언 — 요청마다 읽어 서버 추가·삭제가 재시작 없이 반영된다.
        environ: 자격 값의 원천 (프록시 프로세스 env).
        tokens: 원격 MCP 서버에 붙여도 되는 자격 이름 — 운영자가 명시한 것만. 선언이 모델 키 같은
            다른 비밀 이름을 ``auth.token_env`` 로 적어도 그 값을 외부로 보내지 않는다.
    """

    catalog: Catalog
    environ: Mapping[str, str]
    tokens: frozenset[str]

    def resolve(self, agent: str, server: str) -> McpUpstream | None:
        """The declared remote or sidecar server — None when the agent declares no such server.

        Raises:
            AaestroError: CONFIG/``CFG_002`` if the declared credential is not provided to the
                proxy.
        """
        try:
            manifest = self.catalog.agent(agent)
        except AaestroError as err:
            if err.code == ErrorCode.NF_001:
                return None
            raise
        declared = next((s for s in manifest.spec.mcp.servers if s.name == server), None)
        if declared is None or declared.transport is McpTransport.STDIO:
            return None
        if declared.sidecar is not None:
            # 주소는 선언이 아니라 runtime 의 이름 규칙에서 — 에이전트가 고르지 못한다
            url = sidecar_url(agent, declared)
            return McpUpstream(url=url, target=url_target(url) or "", token=None, sidecar=True)
        if declared.url is None:
            return None
        target = url_target(declared.url)
        if target is None:
            return None
        token = None
        if declared.auth is not None:
            name = declared.auth.token_env
            token = self.environ.get(name) if name in self.tokens else None
            if not token:
                raise AaestroError(
                    category=ErrorCategory.CONFIG,
                    code=ErrorCode.CFG_002,
                    message="mcp server credential is not provided to the egress proxy",
                    agent=agent,
                    details={"mcp_server": server, "token_env": name},
                )
        return McpUpstream(url=declared.url, target=target, token=token)


@dataclass(frozen=True)
class SessionSeal:
    """Binds an upstream MCP session id to the (agent, server) it was issued to.

    upstream 은 모든 에이전트에게서 **같은 프록시 자격**을 받으므로 세션 주인을 가리지 못한다.
    그래서 프록시가 돌려주는 세션 id 에 (에이전트, 서버) 로 서명을 붙이고, 들어오는 id 는 서명을
    확인해 벗긴다 — 다른 에이전트의 세션 id 를 알아내도 쓰지 못한다. 상태가 없으므로 같은 키라면
    프록시 재시작을 넘는다.
    """

    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))

    def seal(self, agent: str, server: str, session: str) -> str:
        return f"{session}.{self._mac(agent, server, session)}"

    def open(self, agent: str, server: str, sealed: str) -> str | None:
        session, _, mac = sealed.rpartition(".")
        if not session or not hmac.compare_digest(mac, self._mac(agent, server, session)):
            return None
        return session

    def _mac(self, agent: str, server: str, session: str) -> str:
        message = f"{agent}\n{server}\n{session}".encode()
        return hmac.new(self.key, message, hashlib.sha256).hexdigest()[:SESSION_MAC_CHARS]


@dataclass
class McpTermination:
    """The ``/mcp/{server}`` route — one agent's calls to one declared remote server."""

    access: Any
    upstreams: McpUpstreams
    mode: EgressMode = EgressMode.ENFORCE
    private_destinations: Collection[str] = ()
    allow_plaintext: bool = False
    http: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT)
    )
    resolver: Callable[[str, int], Awaitable[list[str]]] = resolve
    sessions: SessionSeal = field(default_factory=SessionSeal)

    def router(self) -> APIRouter:
        router = APIRouter()

        @router.api_route(f"{MCP_PREFIX}/{{server}}", methods=["GET", "POST", "DELETE"])
        async def terminate(server: str, request: Request) -> Response:
            return await self.handle(server, request)

        return router

    async def handle(self, server: str, request: Request) -> Response:
        credential = _bearer(request)
        if not credential:
            return _http_error(401, "agent identity required")
        agent, source = await self.access.identify(credential)
        if agent is None:
            if source is DecisionSource.UNREACHABLE:
                return _http_error(503, "access registry unreachable", code="ACC_002")
            return _http_error(401, "agent identity refused")
        try:
            upstream = self.upstreams.resolve(agent, server)
        except AaestroError as err:
            log.error("mcp credential unavailable", agent=agent, mcp_server=server,
                      error_code=err.code)  # fmt: skip
            return _http_error(502, err.message, code=err.code)
        if upstream is None:
            return _http_error(404, "no such remote mcp server declared for this agent")
        if not upstream.sidecar:
            verdict = await self.access.decide(credential, ResourceKind.EGRESS, upstream.target)
            refused = self._refusal(verdict, ResourceKind.EGRESS, upstream.target)
            if refused is not None:
                return _http_error(*refused)
        body = await request.body()
        denied = await self._decide_tools(credential, server, body)
        if denied is not None:
            return denied
        return await self._forward(request, (agent, server), upstream, body)

    async def _decide_tools(self, credential: str, server: str, body: bytes) -> Response | None:
        """``tools/call`` 마다 판정한다 — 하나라도 막히면 아무것도 보내지 않는다."""
        if not body:
            return None
        try:
            parsed = json.loads(body)
        except ValueError:
            return _http_error(400, "mcp request is not JSON")
        messages = parsed if isinstance(parsed, list) else [parsed]
        for message in messages:
            if not isinstance(message, dict) or message.get("method") != TOOLS_CALL:
                continue
            params = message.get("params")
            tool = params.get("name") if isinstance(params, dict) else None
            if not isinstance(tool, str) or not tool:
                return _http_error(400, "tools/call without a tool name")
            target = f"{server}/{tool}"
            verdict = await self.access.decide(credential, ResourceKind.MCP_TOOL, target)
            refused = self._refusal(verdict, ResourceKind.MCP_TOOL, target)
            if refused is None:
                continue
            if isinstance(parsed, dict) and "id" in parsed:
                # 요청 하나에는 JSON-RPC 에러로 답한다 — MCP 클라이언트가 그 호출의 실패로 받는다
                return _rpc_error(parsed["id"], refused)
            return _http_error(*refused)
        return None

    async def _forward(
        self, request: Request, owner: tuple[str, str], upstream: McpUpstream, body: bytes
    ) -> Response:
        agent, server = owner
        parts = urlsplit(upstream.url)
        # 사이드카에는 자격을 싣지 않는다 — 평문이어도 나갈 비밀이 없다. 사설 주소 요구는 아래에서
        plaintext = upstream.sidecar or (
            self.allow_plaintext and upstream.target in self.private_destinations
        )
        if parts.scheme == "http" and not plaintext:
            # 도구 인자와 결과도 민감하다 — 평문은 스위치를 켜고 사설로 명시한 목적지만.
            # 스위치 하나로 모든 서버를 열면 운영 설정의 실수가 공인 호스트로의 평문이 된다 (#302)
            log.error("mcp over plaintext refused", agent=agent,
                      resource=ResourceKind.EGRESS.value, target=upstream.target)  # fmt: skip
            return _http_error(502, "remote mcp server must use https")
        sealed = request.headers.get(SESSION_HEADER)
        session = self.sessions.open(agent, server, sealed) if sealed is not None else None
        if sealed is not None and session is None:
            # MCP 명세는 모르는 세션에 404 로 답하게 한다
            log.warning("mcp session not issued to this agent", agent=agent, mcp_server=server,
                        resource=ResourceKind.MCP_TOOL.value, target=server)  # fmt: skip
            return _http_error(404, "unknown mcp session")
        address = await self._address(parts, upstream.target)
        if address is None and parts.scheme == "http":
            log.error("mcp plaintext to a public address refused", agent=agent,
                      resource=ResourceKind.EGRESS.value, target=upstream.target)  # fmt: skip
            return _http_error(502, "plain http is sent only to a server that resolves privately")
        if address is None:
            log.warning("mcp upstream at a private address refused", agent=agent,
                        resource=ResourceKind.EGRESS.value, target=upstream.target,
                        error_code="ACC_001")  # fmt: skip
            return _http_error(403, "remote mcp server resolves to a private address", "ACC_001")
        outbound = self._outbound(request, parts, address, upstream, body)
        if session is not None:
            outbound.headers[SESSION_HEADER] = session
        try:
            answer = await self.http.send(outbound, stream=True)
        except httpx.HTTPError as err:
            log.warning("mcp upstream unreachable", agent=agent,
                        resource=ResourceKind.EGRESS.value, target=upstream.target,
                        exc_info=err)  # fmt: skip
            return _http_error(502, "remote mcp server unreachable")
        headers = {k: v for k, v in answer.headers.items() if k.lower() not in _RETURNED_EXCLUDED}
        issued = answer.headers.get(SESSION_HEADER)
        if issued is not None:
            headers = {k: v for k, v in headers.items() if k.lower() != SESSION_HEADER}
            headers[SESSION_HEADER] = self.sessions.seal(agent, server, issued)
        return StreamingResponse(
            answer.aiter_raw(),
            status_code=answer.status_code,
            headers=headers,
            background=BackgroundTask(answer.aclose),
        )

    async def _address(self, parts: Any, target: str) -> str | None:
        """연결할 주소 — 확인한 주소로 붙어 판정과 연결 사이에 이름이 바뀌지 못하게 한다."""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        try:
            addresses = await asyncio.wait_for(self.resolver(parts.hostname, port), 10.0)
        except (OSError, TimeoutError):
            return None
        if parts.scheme == "http":
            # 평문은 **실제로 사설 주소로 풀린** 곳에만 — 목록에 있는 이름이 공인 주소로 풀려도
            # 자격이 평문으로 나가지 않게 (#305 리뷰)
            private = [a for a in addresses if not is_public(a)]
            return private[0] if private else None
        if target in self.private_destinations:
            return addresses[0] if addresses else None
        public = [a for a in addresses if is_public(a)]
        return public[0] if public else None

    def _outbound(
        self, request: Request, parts: Any, address: str, upstream: McpUpstream, body: bytes
    ) -> httpx.Request:
        host = f"[{address}]" if ":" in address else address
        netloc = f"{host}:{parts.port}" if parts.port else host
        # 세션 id 는 서명을 벗긴 값으로 호출자가 따로 싣는다
        headers = {
            k: v
            for k, v in request.headers.items()
            if k.lower() in _FORWARDED and k.lower() != SESSION_HEADER
        }
        headers["host"] = parts.netloc
        if upstream.token is not None:
            headers["authorization"] = f"Bearer {upstream.token}"
        extensions = {"sni_hostname": parts.hostname} if parts.scheme == "https" else {}
        return self.http.build_request(
            request.method,
            urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, "")),
            headers=headers,
            content=body,
            extensions=extensions,
        )

    def _refusal(
        self, verdict: Verdict, kind: ResourceKind, target: str
    ) -> tuple[int, str, str | None] | None:
        fields = {
            "agent": verdict.agent or "",
            "resource": kind.value,
            "target": target,
            "decision": "allow" if verdict.allowed else "deny",
            "decision_source": verdict.source.value,
            "decided_by": verdict.decided_by,
        }
        if verdict.allowed:
            log.info("mcp call allowed", **fields)
            return None
        if verdict.source is DecisionSource.UNREACHABLE:
            log.warning("mcp call undecidable", error_code="ACC_002", **fields)
            return 503, "access registry unreachable", "ACC_002"
        if verdict.agent is None or verdict.decided_by == UNKNOWN_IDENTITY:
            log.warning("mcp call identity refused", **fields)
            return 401, "agent identity refused", None
        if self.mode is EgressMode.RECORD:
            log.warning("mcp call denied but recorded only", **fields)
            return None
        log.info("mcp call denied", error_code="ACC_001", **fields)
        return 403, f"{kind.value} denied: {target}", "ACC_001"


def _bearer(request: Request) -> str:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _http_error(status: int, message: str, code: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def _rpc_error(request_id: Any, refused: tuple[int, str, str | None]) -> JSONResponse:
    _status, message, code = refused
    return JSONResponse(
        status_code=200,
        content={
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": DENIED_RPC_CODE, "message": f"{code}: {message}"},
        },
    )


__all__ = ["MCP_PREFIX", "McpTermination", "McpUpstream", "McpUpstreams", "SessionSeal"]
