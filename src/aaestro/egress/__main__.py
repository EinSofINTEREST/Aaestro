"""Runs the egress proxy — ``python -m aaestro.egress``.

두 창구를 연다: CONNECT 터널(외부 HTTPS, 목적지 단위 판정)과 provider 종단(모델 API, 키 주입).
프록시는 에이전트 네트워크와 외부 네트워크에 함께 붙는 **유일한** 컨테이너이고 비밀값을 모두 쥐므로,
그 밖의 일은 하지 않는다 (03 Egress 4).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Awaitable, Collection, Mapping
from typing import Any
from urllib.parse import urlsplit

import structlog
import uvicorn

from aaestro.access.baselines import url_target
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.observability.metrics import DEFAULT_METRICS_PORT, Metrics, start_metrics_server

log = structlog.get_logger(__name__)

CONNECT_PORT_ENV = "AAESTRO_EGRESS_PORT"
PROVIDER_PORT_ENV = "AAESTRO_EGRESS_PROVIDER_PORT"
MODE_ENV = "AAESTRO_EGRESS_MODE"
PRIVATE_DESTINATIONS_ENV = "AAESTRO_EGRESS_PRIVATE_DESTINATIONS"
"""사설 주소로 풀려도 되는 목적지 — 쉼표로. 운영자가 명시한 것만 (예: 사내 provider 대역)."""
ANTHROPIC_UPSTREAM_ENV = "AAESTRO_EGRESS_ANTHROPIC_UPSTREAM"
PLAINTEXT_UPSTREAM_ENV = "AAESTRO_EGRESS_ALLOW_PLAINTEXT_UPSTREAM"
"""``true`` 이고 목적지를 사설로 명시했을 때만 평문 ``http`` 를 받는다.

프록시가 키를 실어 보내는 곳이므로 평문은 사설망의 테스트 대역에만 쓴다. 스위치 하나로 모든
목적지를 열면, 운영 설정에 실수로 들어갔을 때 공인 호스트로 자격이 평문으로 나간다 (#302)."""
ANTHROPIC_KEY_ENV = "ANTHROPIC_API_KEY"  # noqa: S105 — 키 이름이지 값이 아니다
PROXY_HELD_SECRETS = frozenset({ANTHROPIC_KEY_ENV})
"""프록시가 provider 호출에만 쓰는 비밀 — 원격 MCP 자격 목록에 올릴 수 없다."""
MCP_TOKENS_ENV = "AAESTRO_EGRESS_MCP_TOKENS"
"""원격 MCP 서버에 붙여도 되는 자격 이름 — 쉼표로. 선언의 ``auth.token_env`` 가 이 목록에 있을 때만
그 값을 보낸다 (모델 키 같은 다른 비밀을 선언으로 끌어내지 못하게)."""
REPO_ROOT_ENV = "AAESTRO_REPO_ROOT"
"""선언 루트 — 원격 MCP 서버의 주소·자격 이름을 에이전트 선언에서 읽는다. 없으면 MCP 종단이 없다."""
LOG_LEVEL_ENV = "AAESTRO_LOG_LEVEL"
LOG_FORMAT_ENV = "AAESTRO_LOG_FORMAT"
METRICS_PORT_ENV = "AAESTRO_METRICS_PORT"

DEFAULT_CONNECT_PORT = 8080
DEFAULT_PROVIDER_PORT = 8081
DEFAULT_ANTHROPIC_UPSTREAM = "https://api.anthropic.com"
MAX_PORT = 65536


def settings(environ: Mapping[str, str]) -> dict[str, Any]:
    """Validated process settings.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the registry settings or the mode are wrong.
    """
    from aaestro.access.client import ACCESS_URL_ENV, ENFORCER_TOKEN_ENV
    from aaestro.egress.connect import EgressMode

    url, token = environ.get(ACCESS_URL_ENV, ""), environ.get(ENFORCER_TOKEN_ENV, "")
    if not (url and token):
        # 판정 없이 뜨는 이그레스 프록시는 열린 문이다 — 기동하지 않는다
        raise _config("egress proxy requires the access registry URL and enforcer token",
                      [ACCESS_URL_ENV, ENFORCER_TOKEN_ENV])  # fmt: skip
    raw_mode = environ.get(MODE_ENV, EgressMode.ENFORCE.value)
    try:
        mode = EgressMode(raw_mode)
    except ValueError as err:
        raise _config(f"unknown egress mode: {raw_mode}", [MODE_ENV]) from err
    private = _names(environ.get(PRIVATE_DESTINATIONS_ENV, ""))
    return {
        "access_url": url,
        "enforcer_token": token,
        "mode": mode,
        "private_destinations": private,
        "connect_port": port_setting(environ, CONNECT_PORT_ENV, DEFAULT_CONNECT_PORT),
        "provider_port": port_setting(environ, PROVIDER_PORT_ENV, DEFAULT_PROVIDER_PORT),
        "anthropic_upstream": _upstream(environ, private),
        "anthropic_key": environ.get(ANTHROPIC_KEY_ENV, ""),
        "allow_plaintext": environ.get(PLAINTEXT_UPSTREAM_ENV, "").lower() == "true",
        "repo_root": environ.get(REPO_ROOT_ENV, ""),
        "mcp_tokens": _mcp_tokens(environ),
    }


def _mcp_tokens(environ: Mapping[str, str]) -> frozenset[str]:
    """원격 MCP 서버에 보낼 수 있는 자격 이름 — 프록시가 따로 쥐는 비밀은 올리지 못한다.

    목록이 유일한 문이다: 운영자가 실수로 모델 키나 레지스트리 자격 이름을 넣으면, 선언의
    ``auth.token_env`` 하나로 그 값이 선언된 MCP 호스트로 나간다.
    """
    names = _names(environ.get(MCP_TOKENS_ENV, ""))
    reserved = sorted(n for n in names if n in PROXY_HELD_SECRETS or n.startswith("AAESTRO_"))
    if reserved:
        raise _config(f"proxy-held secrets cannot be sent to mcp servers: {reserved}",
                      [MCP_TOKENS_ENV])  # fmt: skip
    return names


def _names(raw: str) -> frozenset[str]:
    return frozenset(entry.strip() for entry in raw.split(",") if entry.strip())


def port_setting(environ: Mapping[str, str], key: str, default: int) -> int:
    """A listener port from the environment — ``CFG_001`` unless it is an integer in 1..65535."""
    raw = environ.get(key, str(default))
    try:
        port = int(raw)
    except ValueError as err:
        raise _config(f"invalid port: {raw!r}", [key]) from err
    if not 0 < port < MAX_PORT:
        raise _config(f"port out of range: {raw!r}", [key])
    return port


def _upstream(environ: Mapping[str, str], private: frozenset[str]) -> str:
    """provider upstream — 프록시가 여기에 API 키를 실어 보내므로 평문 전송을 기본으로 막는다."""
    raw = environ.get(ANTHROPIC_UPSTREAM_ENV, DEFAULT_ANTHROPIC_UPSTREAM)
    parts = urlsplit(raw)
    try:
        port = parts.port
    except ValueError as err:
        raise _config(f"invalid provider upstream port: {raw!r}", [ANTHROPIC_UPSTREAM_ENV]) from err
    malformed = (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or port == 0
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    )
    if malformed:
        raise _config(
            "provider upstream must be an http(s) URL with a host and no credentials, query or "
            "fragment",
            [ANTHROPIC_UPSTREAM_ENV],
        )
    allowed = plaintext_allowed(
        environ.get(PLAINTEXT_UPSTREAM_ENV, "").lower() == "true", url_target(raw), private
    )
    if parts.scheme == "http" and not allowed:
        raise _config(
            "provider upstream must use https — the proxy sends the provider key to it; plain "
            "http is accepted only for a destination listed as private",
            [ANTHROPIC_UPSTREAM_ENV, PLAINTEXT_UPSTREAM_ENV, PRIVATE_DESTINATIONS_ENV],
        )
    return raw


def plaintext_allowed(switch: bool, target: str | None, private: Collection[str]) -> bool:
    """평문 ``http`` 로 보내도 되는가 — 스위치를 켰고, 목적지를 사설로 명시했을 때만 (#302).

    공인 호스트로의 평문은 설정을 어떻게 조합해도 열리지 않는다.
    """
    return switch and target is not None and target in private


def _config(message: str, keys: list[str]) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message=message,
        details={"settings": keys},
    )


def main() -> None:
    from aaestro.observability.logging import configure

    configure(
        level=os.environ.get(LOG_LEVEL_ENV, "INFO"),
        json_output=os.environ.get(LOG_FORMAT_ENV, "json") == "json",
    )
    metrics = Metrics()
    config = settings(os.environ)
    start_metrics_server(
        port_setting(os.environ, METRICS_PORT_ENV, DEFAULT_METRICS_PORT), registry=metrics.registry
    )
    asyncio.run(run(config, metrics=metrics))


async def run(config: dict[str, Any], *, metrics: Metrics | None = None) -> None:
    """Serve both listeners and follow the registry's change feed until cancelled."""
    from aaestro.access.baselines import PROVIDER_HOSTS
    from aaestro.access.client import AccessClient
    from aaestro.egress.connect import ConnectProxy
    from aaestro.egress.providers import ANTHROPIC_ENDPOINTS, Upstream, create_provider_app

    access = AccessClient(
        base_url=config["access_url"],
        enforcer_token=config["enforcer_token"],
        component="egress",
        metrics=metrics,
    )
    upstreams = {}
    if config["anthropic_key"]:
        upstreams["anthropic"] = Upstream(
            base_url=config["anthropic_upstream"],
            logical_host=PROVIDER_HOSTS["anthropic"],
            api_key=config["anthropic_key"],
            endpoints=ANTHROPIC_ENDPOINTS,
        )
    proxy = ConnectProxy(
        access=access, mode=config["mode"], private_destinations=config["private_destinations"]
    )
    connect = await asyncio.start_server(proxy.handle, "0.0.0.0", config["connect_port"])  # noqa: S104
    provider = uvicorn.Server(
        uvicorn.Config(
            create_provider_app(
                access, upstreams, mode=config["mode"], routers=_mcp_routers(config, access)
            ),
            host="0.0.0.0",  # noqa: S104
            port=config["provider_port"],
            log_config=None,
        )
    )
    log.info(
        "egress proxy ready",
        mode=config["mode"].value,
        providers=sorted(upstreams),
        port=config["connect_port"],
    )
    try:
        async with connect:
            await supervise(access.watch(), connect.serve_forever(), provider.serve())
    finally:
        await access.aclose()


def _mcp_routers(config: Mapping[str, Any], access: Any) -> list[Any]:
    """원격 MCP 종단 — 선언 루트가 있을 때만. 없으면 원격 MCP 호출은 404 로 막힌다."""
    if not config["repo_root"]:
        return []
    from pathlib import Path

    from aaestro.catalog import Catalog
    from aaestro.egress.mcp import McpTermination, McpUpstreams, SessionSeal

    termination = McpTermination(
        access=access,
        upstreams=McpUpstreams(
            catalog=Catalog.under(Path(config["repo_root"])),
            environ=os.environ,
            tokens=config["mcp_tokens"],
        ),
        mode=config["mode"],
        private_destinations=config["private_destinations"],
        allow_plaintext=config["allow_plaintext"],
        # 재시작을 넘는 서명 키 — 프록시 비밀(레지스트리 자격)에서 용도를 붙여 끌어낸다
        sessions=SessionSeal(key=session_key(config["enforcer_token"])),
    )
    return [termination.router()]


def session_key(enforcer_token: str) -> bytes:
    return hashlib.sha256(b"aaestro-mcp-session\0" + enforcer_token.encode()).digest()


async def supervise(*parts: Awaitable[Any]) -> None:
    """Run the parts as one lifecycle — the first to end, for any reason, ends them all.

    판정 피드가 멈춘 채 두 창구가 계속 받으면 캐시된 허용이 회수를 모른 채 남는다. 창구 하나가
    닫혔는데 다른 쪽이 붙잡으면 프로세스가 끝나지 않는다. 그래서 하나라도 끝나면 나머지를 취소하고,
    끝난 쪽의 예외를 그대로 올려 컨테이너 재시작 정책에 맡긴다.
    """
    tasks = [asyncio.ensure_future(part) for part in parts]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        # 취소된 나머지의 결과는 버린다 — 알려야 할 실패는 먼저 끝난 쪽의 것이다
        await asyncio.gather(*tasks, return_exceptions=True)
    for task in done:
        failure = None if task.cancelled() else task.exception()
        if failure is not None:
            log.error("egress proxy stopping", reason="a listener or the access feed failed",
                      exc_info=failure)  # fmt: skip
            raise failure
    log.warning("egress proxy stopping", reason="a listener or the access feed ended")


if __name__ == "__main__":
    main()
