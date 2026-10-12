"""Model API termination — the proxy holds the provider key, not the agent (#293).

에이전트는 프록시를 provider base URL 로 부르고, API 키 자리에 **자기 신원**을 싣는다. 프록시는
신원으로 판정한 뒤 신원을 떼고 진짜 키를 붙여 provider 로 보낸다 — 에이전트 컨테이너에는 키가
없으므로 키 회수가 재배포 없이 된다 (02 Secrets Injection).

판정 대상은 provider 의 **논리 호스트**(``api.anthropic.com``)다: 운영자가 회수하는 것은
"모델 API" 이지 그 배치의 upstream 주소가 아니다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import structlog
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from aaestro.access.client import DecisionSource
from aaestro.access.model import ResourceKind
from aaestro.egress.connect import (
    UNKNOWN_IDENTITY,
    Decider,
    EgressMode,
    Resolver,
    is_public,
    resolve,
)

log = structlog.get_logger(__name__)

RESOLVE_TIMEOUT_S = 10.0
UPSTREAM_TIMEOUT_S = 600.0
"""모델 호출은 길다 — 스트리밍 응답이 끝날 때까지 기다린다."""

_HOP_BY_HOP = frozenset(
    {
        "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
        "trailer", "transfer-encoding", "upgrade", "host", "content-length",
    }
)  # fmt: skip
_IDENTITY_HEADERS = frozenset({"x-api-key", "authorization"})


@dataclass(frozen=True)
class Upstream:
    """Where a provider's calls go, and the key the proxy adds.

    Attributes:
        base_url: provider 주소 (예: ``https://api.anthropic.com``).
        logical_host: 판정 대상 이름.
        api_key: 프록시만 쥐는 키.
        key_header: 키를 싣는 헤더.
    """

    base_url: str
    logical_host: str
    api_key: str
    key_header: str = "x-api-key"
    endpoints: frozenset[tuple[str, str]] = frozenset()
    """키를 붙여 보내도 되는 ``(METHOD, path)`` — 모델 API 만. 여기 없는 경로는 보내지 않는다.

    호스트 단위 판정만으로 경로를 열어 두면 모델 호출을 허락받은 에이전트가 프록시의 키로
    provider 의 다른(상태를 바꾸는) API 까지 부른다."""


def create_provider_app(
    access: Decider,
    upstreams: Mapping[str, Upstream],
    *,
    mode: EgressMode = EgressMode.ENFORCE,
    http: httpx.AsyncClient | None = None,
    routers: Sequence[APIRouter] = (),
    resolver: Resolver = resolve,
) -> FastAPI:
    """Build the termination app — ``/{provider}/{path}`` forwards to that provider.

    ``routers`` 는 provider 경로보다 **먼저** 붙는다 — 원격 MCP 종단(``/mcp/{server}``)이
    ``mcp`` 라는 provider 로 잘못 잡히지 않게.
    """
    client = http or httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_S)
    app = FastAPI(title="Aaestro egress — provider termination")
    for router in routers:
        app.include_router(router)

    @app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def forward(provider: str, path: str, request: Request) -> Response:
        upstream = upstreams.get(provider)
        if upstream is None:
            return _error(404, "unknown provider")
        if (request.method.upper(), path.strip("/")) not in upstream.endpoints:
            log.warning(
                "provider endpoint not allowed",
                agent="",
                resource=ResourceKind.EGRESS.value,
                target=f"{upstream.logical_host}/{path}",
                error_code="ACC_001",
            )
            return _error(403, "provider endpoint not allowed through the proxy", code="ACC_001")
        credential = _credential(request)
        if not credential:
            return _error(401, "agent identity required")
        verdict = await access.decide(credential, ResourceKind.EGRESS, upstream.logical_host)
        refusal = _refusal(verdict, upstream.logical_host, mode)
        if refusal is not None:
            return refusal

        headers = {
            name: value
            for name, value in request.headers.items()
            if name.lower() not in _HOP_BY_HOP | _IDENTITY_HEADERS
        }
        headers[upstream.key_header] = upstream.api_key
        base = await _pinned_base(upstream.base_url, resolver, headers)
        if base is None:
            log.error("provider plaintext to a public address refused", agent=verdict.agent or "",
                      resource=ResourceKind.EGRESS.value, target=upstream.logical_host)  # fmt: skip
            return _error(502, "plain http is sent only to an upstream that resolves privately")
        outbound = client.build_request(
            request.method,
            f"{base}/{path}",
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
        try:
            answer = await client.send(outbound, stream=True)
        except httpx.HTTPError as err:
            log.warning(
                "provider upstream unreachable",
                agent=verdict.agent or "",
                resource=ResourceKind.EGRESS.value,
                target=upstream.logical_host,
                exc_info=err,
            )
            return _error(502, "provider unreachable")
        return StreamingResponse(
            answer.aiter_raw(),
            status_code=answer.status_code,
            headers={k: v for k, v in answer.headers.items() if k.lower() not in _HOP_BY_HOP},
            background=BackgroundTask(answer.aclose),
        )

    return app


async def _pinned_base(base_url: str, resolver: Resolver, headers: dict[str, str]) -> str | None:
    """요청을 보낼 base URL — 평문이면 **사설로 풀린 주소**로 고정하고, 못 하면 None.

    기동 검증은 이름이 사설 목록에 있는지만 본다. 그 이름이 공인 주소로 풀리면 키가 평문으로
    나간다 — 보낼 때마다 풀어 사설 주소일 때만 그 주소로 붙는다 (#305 리뷰). https 는 그대로.
    """
    parts = urlsplit(base_url)
    if parts.scheme != "http" or not parts.hostname:
        return base_url.rstrip("/")
    port = parts.port or 80
    try:
        addresses = await asyncio.wait_for(resolver(parts.hostname, port), RESOLVE_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    private = [address for address in addresses if not is_public(address)]
    if not private:
        return None
    headers["host"] = parts.netloc
    host = f"[{private[0]}]" if ":" in private[0] else private[0]
    return urlunsplit(("http", f"{host}:{port}", parts.path.rstrip("/"), "", ""))


def _refusal(verdict: Any, target: str, mode: EgressMode) -> Response | None:
    fields = {
        "agent": verdict.agent or "",
        "resource": ResourceKind.EGRESS.value,
        "target": target,
        "decision": "allow" if verdict.allowed else "deny",
        "decision_source": verdict.source.value,
        "decided_by": verdict.decided_by,
    }
    if verdict.allowed:
        log.debug("provider call allowed", **fields)
        return None
    if verdict.source is DecisionSource.UNREACHABLE:
        log.warning("provider call undecidable", error_code="ACC_002", **fields)
        return _error(503, "access registry unreachable", code="ACC_002")
    if verdict.agent is None or verdict.decided_by == UNKNOWN_IDENTITY:
        log.warning("provider call identity refused", **fields)
        return _error(401, "agent identity refused")
    if mode is EgressMode.RECORD:
        log.warning("provider call denied but recorded only", **fields)
        return None
    log.info("provider call denied", error_code="ACC_001", **fields)
    return _error(403, "model API denied for this agent", code="ACC_001")


def _credential(request: Request) -> str:
    key = request.headers.get("x-api-key", "").strip()
    if key:
        return key
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _error(status: int, message: str, *, code: str | None = None) -> JSONResponse:
    # provider SDK 가 읽는 모양({"type":"error","error":{...}})에 aaestro 코드를 더한다
    return JSONResponse(
        status_code=status,
        content={
            "type": "error",
            "error": {"type": "permission_error", "message": message, "code": code},
        },
    )


ANTHROPIC_ENDPOINTS = frozenset(
    {
        ("POST", "v1/messages"),
        ("POST", "v1/messages/count_tokens"),
        ("GET", "v1/models"),
    }
)
"""에이전트가 모델을 쓰는 데 필요한 Anthropic API — 메시지 생성, 토큰 세기, 모델 목록."""


__all__ = ["ANTHROPIC_ENDPOINTS", "Upstream", "create_provider_app"]
