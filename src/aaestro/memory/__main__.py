"""Runs the Memory Service.

Memory Service 를 프로세스로 실행한다 — ``python -m aaestro.memory``.

09 Access Enforcement 1 이 요구하는 배치다: **저장소 자격증명은 이 프로세스만
갖고**, 에이전트는 불투명 토큰으로 HTTP 를 통해서만 닿는다. 이 진입점이
없으면 그 배치를 컨테이너로 세울 수 없다 (#181).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
from typing import Any

import structlog
import uvicorn

from aaestro.config import load_config
from aaestro.core.startup import startup_guard
from aaestro.memory.bootstrap import MemoryDeployment, build_deployment
from aaestro.observability.metrics import Metrics, metrics_port_from_env, start_metrics_server

log = structlog.get_logger(__name__)

DEFAULT_PORT = 8090
DEFAULT_ROOT = "/repo"

PORT_ENV = "AAESTRO_MEMORY_PORT"
ROOT_ENV = "AAESTRO_REPO_ROOT"
ENVIRONMENT_ENV = "AAESTRO_ENV"
CONFIG_DIR_ENV = "AAESTRO_CONFIG_DIR"
LOG_LEVEL_ENV = "AAESTRO_LOG_LEVEL"
LOG_FORMAT_ENV = "AAESTRO_LOG_FORMAT"
METRICS_PORT_ENV = "AAESTRO_METRICS_PORT"
TOKENS_PATH_ENV = "AAESTRO_MEMORY_TOKENS_PATH"


def _setup_observability() -> Metrics:
    """Configure logging and expose metrics for this process.

    계측 코드가 있어도 여기서 registry 를 만들어 주입하지 않으면 런타임에서는
    아무 것도 흐르지 않는다 — agentd 와 같은 이유다.
    """
    from aaestro.observability.logging import configure

    configure(
        level=os.environ.get(LOG_LEVEL_ENV, "INFO"),
        json_output=os.environ.get(LOG_FORMAT_ENV, "json") == "json",
    )
    metrics = Metrics()
    start_metrics_server(
        metrics_port_from_env(METRICS_PORT_ENV),
        registry=metrics.registry,
    )
    return metrics


def _publish_tokens(tokens: dict[str, str], path: Path) -> None:
    """Write the issued tokens where the runtime can pick them up.

    발급한 토큰을 runtime 이 집어갈 수 있는 곳에 씁니다.

    토큰은 자격증명이 아니라 **범위**를 담은 불투명 문자열이지만, 남의 것을
    쥐면 그 에이전트의 space 에 닿는다 — 소유자만 읽도록 좁힌다.
    """
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(tokens, indent=2), encoding="utf-8")
    path.chmod(0o600)
    log.info("memory tokens published", **{"agents": len(tokens)})


def _access(metrics: Metrics) -> Any:
    """레지스트리 판정 클라이언트 — 주소와 강제 지점 토큰이 **둘 다** 있어야 켠다.

    하나만 있으면 설정 실수다. 조용히 토큰 모드로 뜨면 운영자는 실시간 회수가 된다고 믿는다.
    """
    from aaestro.access.client import ACCESS_URL_ENV, ENFORCER_TOKEN_ENV, AccessClient
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    url, token = os.environ.get(ACCESS_URL_ENV), os.environ.get(ENFORCER_TOKEN_ENV)
    if not url and not token:
        return None
    if not (url and token):
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="memory service registry mode needs both the access URL and enforcer token",
            details={"settings": [ACCESS_URL_ENV, ENFORCER_TOKEN_ENV]},
        )
    return AccessClient(base_url=url, enforcer_token=token, component="memory", metrics=metrics)


def main() -> None:
    """Run the Memory Service."""
    # 설정 로드 뒤의 기동 실패 (깨진 memoryset 선언 등) 도 같은 한 줄로
    with startup_guard(log, "memory service startup failed"):
        # 포트 설정 읽기 (CFG_001) 도 가드 안 — 로깅 설정이 그보다 먼저라 한 줄은 JSON 이다
        metrics = _setup_observability()
        _start(metrics)


def _start(metrics: Metrics) -> None:
    """설정을 읽고 조립해 서빙한다 — 여기서 올라온 ``AaestroError`` 는 ``main`` 의 가드가 받는다."""
    with startup_guard(log, "configuration rejected"):
        config = load_config(
            os.environ.get(ENVIRONMENT_ENV, "dev"),
            config_dir=os.environ.get(CONFIG_DIR_ENV, "configs"),
        )
    root = Path(os.environ.get(ROOT_ENV, DEFAULT_ROOT))

    deployment = build_deployment(config, root=root, metrics=metrics, access=_access(metrics))

    tokens_path = os.environ.get(TOKENS_PATH_ENV)
    if tokens_path and deployment.access is None:
        _publish_tokens(deployment.tokens, Path(tokens_path))

    port = int(os.environ.get(PORT_ENV, DEFAULT_PORT))
    log.info("memory service starting", port=port)
    _serve(deployment, port, interval_s=config.memory.index_lag_target_s)


def _serve(deployment: MemoryDeployment, port: int, *, interval_s: float) -> None:
    """Serve the app while an indexer drains the write queue.

    앱을 서빙하면서 **색인 큐를 비우는 루프**를 함께 돌립니다.

    09 Write Path 는 저장과 색인의 분리를 규정합니다 — append 는 embedding 을
    기다리지 않습니다. 그 대신 **누군가 큐를 비워야** 하고, 그 주체가 없으면
    저장한 기억이 영원히 검색되지 않습니다 (#207).
    """
    asyncio.run(_run(deployment, port, interval_s=interval_s))


async def _run(deployment: MemoryDeployment, port: int, *, interval_s: float) -> None:
    """서버와 인덱서를 함께 돌린다 — 서버가 끝나면 인덱서도 정리한다."""
    server = uvicorn.Server(
        uvicorn.Config(deployment.app, host="0.0.0.0", port=port, log_config=None)  # noqa: S104
    )
    indexing = asyncio.create_task(_index_loop(deployment.indexer, interval_s))
    # 변경 알림을 따라가지 않으면 회수가 캐시 뒤에 묻힌다
    access = deployment.access
    watching = asyncio.create_task(access.watch()) if access is not None else None
    try:
        await server.serve()
    finally:
        indexing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await indexing
        if access is not None and watching is not None:
            watching.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watching
            await access.aclose()
        # 종료 직전 한 번 더 비운다 — 버리면 그 항목은 영원히 검색되지 않는다
        _drain_once(deployment.indexer)


async def _index_loop(indexer: Any, interval_s: float) -> None:
    """주기적으로 큐를 비운다 — 목표 지연은 설정이 정한다."""
    if indexer is None:
        return
    while True:
        await asyncio.sleep(interval_s)
        _drain_once(indexer)


def _drain_once(indexer: Any) -> None:
    """한 번 비운다 — 실패가 서비스를 죽이지 않는다 (09 Write Path 3).

    누적 실패는 `IndexQueue` 가 `MEM_003` 으로 드러내고 재시도를 위해 항목을
    큐에 남긴다 — 여기서 또 재시도하면 이중이다.
    """
    if indexer is None:
        return
    try:
        indexed = indexer.drain()
    except Exception as err:  # noqa: BLE001 — 어떤 실패도 서비스를 죽이면 안 된다
        log.warning("memory indexing pass failed", exc_info=err)
        return
    if indexed:
        log.debug("memory indexing pass", **{"entries": indexed})


if __name__ == "__main__":
    main()
