"""Runs the Control Plane.

Control Plane 을 프로세스로 실행한다 — ``python -m aaestro.orchestrator``.

01 은 Control Plane 의 책임으로 "run submission and result retrieval" 을 규정하고
`create_app` 이 그 표면을 갖고 있었는데, **그 앱을 만드는 곳이 테스트뿐이었다** —
`aaestro run-list` / `run-drain` / `run-resume` 세 명령이 붙을 서버가 없었다 (#221).

**이 프로세스가 하지 않는 일**: run 을 구동하지 않는다. 조회와 drain 은 저장소만
있으면 되지만 resume 은 다르다 — 이어갈 state 가 구동 프로세스의 핸들에 있다.
그래서 여기서 서빙하는 앱은 `resume` 없이 만들어지고, resume 요청은 501 로
거절한다. 조용히 성공해 운영자가 재개됐다고 믿는 편이 훨씬 나쁘다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.config import (
    CONFIG_DIR_ENV,
    DEFAULT_CONFIG_DIR,
    REPO_ROOT_ENV,
    load_config,
    resolve_environment,
)
from aaestro.core.startup import startup_guard
from aaestro.http_auth import is_loopback
from aaestro.memory.client import MEMORY_TOKENS_PATH_ENV, MEMORY_URL_ENV
from aaestro.observability.logging import LOG_FORMAT_ENV, LOG_LEVEL_ENV
from aaestro.observability.metrics import (
    METRICS_PORT_ENV,
    Metrics,
    metrics_port_from_env,
    start_metrics_server,
)
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.inuse import any_of, run_backed
from aaestro.orchestrator.runstore import SqliteRunStore

if TYPE_CHECKING:
    from aaestro.authoring import InUse
    from aaestro.catalog import Catalog
    from aaestro.materials import MaterialStore
    from aaestro.orchestrator.runs import RunService
    from aaestro.runtime.deployments import DeploymentManager
    from aaestro.runtime.images import ImageBuilder

log = structlog.get_logger(__name__)


def _setup_observability() -> Metrics:
    """Configure logging and expose metrics for this process.

    계측 코드가 있어도 registry 를 만들어 주입하지 않으면 아무 것도 흐르지 않는다 —
    agentd / memory 와 같은 이유다.
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


def main() -> None:
    """Serve the Control Plane over the configured run store."""
    # 계측기는 **노출되는 것 하나**를 모두가 쓴다 — 저마다 새로 만들면 기록은 되는데 아무도 못 본다
    # 설정 로드 뒤의 기동 실패 (빠진 저장소 · 토큰, 깨진 선언) 도 같은 한 줄로 — 가드 밖이면
    # traceback 만 남고 details 가 사라진다
    with startup_guard(log, "control plane startup failed"):
        # 포트 설정 읽기 (CFG_001) 도 가드 안 — 로깅 설정이 그보다 먼저라 한 줄은 JSON 이다
        metrics = _setup_observability()
        _start(metrics)


def _start(metrics: Metrics) -> None:
    """설정을 읽고 조립해 서빙한다 — 여기서 올라온 ``AaestroError`` 는 ``main`` 의 가드가 받는다."""
    with startup_guard(log, "configuration rejected"):
        config = load_config(
            resolve_environment(),
            config_dir=os.environ.get(CONFIG_DIR_ENV, DEFAULT_CONFIG_DIR),
        )
    orchestrator = config.orchestrator

    if orchestrator.run_store is None:
        # 빈 목록을 돌려주면 운영자는 "run 이 없다" 고 읽는다 — 설정이 빠진 것과
        # 구분되지 않으므로 기동을 거부한다
        raise config_missing_store()

    if orchestrator.control_token is None:
        if not is_loopback(orchestrator.control_host):
            # 파일을 쓰고 컨테이너를 띄우는 표면을 무인증으로 밖에 열지 않는다
            raise config_missing_token(orchestrator.control_host)
        log.warning(
            "control plane has no token — allowed only because it binds to loopback",
            host=orchestrator.control_host,
        )

    if (
        orchestrator.access_store is not None
        and orchestrator.access_enforcer_token is None
        and not is_loopback(orchestrator.control_host)
    ):
        # 판정 라우트를 무인증으로 밖에 열면 누구나 자격의 유효성을 떠볼 수 있다
        raise config_missing_enforcer_token(orchestrator.control_host)
    if config.runtime.egress_proxy is not None and orchestrator.access_store is None:
        # 프록시는 에이전트 신원으로 판정한다 — 레지스트리 없이 켜면 외부 호출이 전부 막힌다
        raise config_egress_without_registry()
    if orchestrator.access_store is not None and orchestrator.access_agent_url is None:
        raise config_missing_agent_url()
    warn_if_agents_reach_the_host(config.runtime, orchestrator.control_host)

    store = SqliteRunStore(path=orchestrator.run_store)
    # 설정의 registry.roots 는 상대 경로다 — 작업 디렉토리가 아니라 레포 루트 기준
    root = Path(os.environ.get(REPO_ROOT_ENV, ".")).resolve()
    catalog = Catalog.from_config(config.registry.roots, base=root)
    deployments = _deployment_manager(config, catalog, store_root=root, orchestrator=orchestrator)
    access = _access_registry(orchestrator, catalog, metrics=metrics)
    if deployments is not None:
        # 배포가 에이전트마다 신원을 발급해 주입한다 (#277)
        deployments.access = access
        deployments.access_url = orchestrator.access_agent_url
        if config.runtime.egress_proxy is not None:
            from aaestro.runtime.deployments import EgressEndpoints

            proxy = config.runtime.egress_proxy
            deployments.egress = EgressEndpoints(
                connect_url=proxy.connect_url, providers_url=proxy.providers_url
            )
            deployments.sidecar_proxy = sidecar_proxy(proxy)
    # 실행 중 run 과 배포가 참조하는 선언은 지우거나 덮어쓰지 못한다 (#242 리뷰 / #243)
    pins: list[InUse] = [run_backed(store, catalog)]
    if deployments is not None:
        pins.append(deployments.in_use)
    author = Author(
        catalog=catalog,
        a2a_port_range=config.protocols.a2a.port_range,
        in_use=any_of(*pins),
        materials=_material_store(orchestrator),
        providers=_proxy_providers(config),
        state_artifacts=orchestrator.state_artifact_root is not None,
    )
    if deployments is not None:
        # manager 는 검증에 author 를 쓴다 — 서로를 가리키므로 여기서 잇는다
        deployments.author = author
    runs = (
        None
        if deployments is None
        else _run_service(config, catalog, deployments, store=store, metrics=metrics)
    )
    builder = _image_builder(orchestrator, catalog, author)
    if deployments is not None:
        # 배포는 굽지 않는다 — 굽혔는지 **묻기만** 한다 (#266). 빌더가 없으면 빌드 표면이
        # 꺼진 것이고, 매니페스트의 이미지를 그대로 쓴다
        deployments.images = builder
    log.info(
        "control plane starting",
        port=orchestrator.control_port,
        run_store=orchestrator.run_store,
        repo_root=str(root),
    )
    uvicorn.run(
        create_app(
            store,
            catalog=catalog,
            token=orchestrator.control_token,
            author=author,
            builder=builder,
            deployments=deployments,
            runs=runs,
            access=access,
            enforcer_token=orchestrator.access_enforcer_token,
        ),
        **server_options(orchestrator.control_host, orchestrator.control_port),
    )


def server_options(host: str, port: int) -> dict[str, Any]:
    """The uvicorn settings the entrypoint serves with — shared with the shutdown test (#485)."""
    # 진행 중인 해체는 uvicorn 의 graceful shutdown (요청 대기, 상한 없음) 이 끝까지 보낸다 —
    # timeout_graceful_shutdown 을 주면 해체가 끊긴다 (#485)
    return {"host": host, "port": port, "log_config": None}


def _deployment_manager(
    config: Any, catalog: Catalog, *, store_root: Path, orchestrator: Any
) -> DeploymentManager | None:
    """배포 lifecycle 을 조립한다 — `deployment_store` 가 없으면 배포 표면을 열지 않는다.

    Docker 데몬은 SDK 가 환경(DOCKER_HOST)에서 찾는다. secrets 의 값 원천은 이 프로세스의
    환경변수다 — 무엇이 통과할지는 스코프 선언(allowlist / group / global)이 정한다.
    """
    if orchestrator.deployment_store is None:
        log.warning("deployments disabled — orchestrator.deployment_store is not set")
        return None
    from aaestro.runtime.deployments import DeploymentManager, SqliteDeploymentStore
    from aaestro.runtime.docker.client import SdkDockerClient
    from aaestro.runtime.launcher import AgentLauncher
    from aaestro.runtime.ports import A2APortAllocator

    launcher = AgentLauncher(
        engine=agent_engine(config.runtime, SdkDockerClient()),
        ports=A2APortAllocator(port_range=config.protocols.a2a.port_range),
        health_interval_s=config.runtime.health_check.interval_s,
        startup_grace_s=config.runtime.health_check.startup_grace_s,
    )
    tokens_path = os.environ.get(MEMORY_TOKENS_PATH_ENV)
    memory_tokens: dict[str, str] = {}
    if tokens_path and Path(tokens_path).is_file():
        memory_tokens = json.loads(Path(tokens_path).read_text(encoding="utf-8"))
    return DeploymentManager(
        catalog=catalog,
        author=Author(
            catalog=catalog,
            a2a_port_range=config.protocols.a2a.port_range,
            providers=_proxy_providers(config),
            state_artifacts=orchestrator.state_artifact_root is not None,
        ),
        launcher=launcher,
        store=SqliteDeploymentStore(path=orchestrator.deployment_store),
        secrets_env=dict(os.environ),
        agent_env=dict(config.runtime.agent_env),
        memory_url=os.environ.get(MEMORY_URL_ENV),
        memory_tokens=memory_tokens,
        ready_timeout_s=orchestrator.deployment_ready_timeout_s,
    )


def _proxy_providers(config: Any) -> frozenset[str] | None:
    """배포 검증이 대조할 프록시 upstream — 프록시가 없으면 None (논리 호스트만 본다)."""
    proxy = config.runtime.egress_proxy
    return None if proxy is None else frozenset(proxy.providers)


def sidecar_proxy(proxy: Any) -> Any:
    """사이드카 네트워크에 붙일 프록시 — 컨테이너 이름을 모르면 None (사이드카 배포가 거절된다)."""
    from urllib.parse import urlsplit

    from aaestro.runtime.sidecars import ProxyAttachment

    if proxy.container is None:
        return None
    # 사이드카가 물려받는 HTTPS_PROXY 가 이 이름을 가리킨다 — 같은 별칭으로 붙인다
    return ProxyAttachment(container=proxy.container, alias=urlsplit(proxy.connect_url).hostname)


def agent_engine(runtime: Any, client: Any) -> Any:
    """The Docker engine agents run on — isolated whenever an egress proxy is configured.

    프록시를 켜면 그것이 유일한 출구여야 한다 — 에이전트 네트워크에 외부 경로가 남으면 컨테이너 안
    코드가 프록시를 건너뛰고 판정을 우회한다 (#280, 02 Network).
    """
    from aaestro.runtime.docker.engine import DockerEngine

    return DockerEngine(
        client=client, network=runtime.network, internal=runtime.egress_proxy is not None
    )


def _run_service(
    config: Any,
    catalog: Catalog,
    deployments: DeploymentManager,
    *,
    store: SqliteRunStore,
    metrics: Any = None,
) -> RunService:
    """이 프로세스가 run 을 구동한다 — 노드 호출은 배포된 컨테이너로 라우팅된다 (#244)."""
    from aaestro.orchestrator.checkpoint import build_checkpointer
    from aaestro.orchestrator.models import CatalogModels
    from aaestro.orchestrator.run import RunManager
    from aaestro.orchestrator.runs import RoutedClients, RunService
    from aaestro.orchestrator.state_artifacts import state_artifact_store
    from aaestro.orchestrator.submit import RunSubmitter
    from aaestro.runtime.nodes import ControlNodeRuntime

    orchestrator = config.orchestrator
    submitter = RunSubmitter(
        runtime=ControlNodeRuntime(clients=RoutedClients(deployments.launcher)),
        manager=RunManager(
            max_concurrent_runs=orchestrator.max_concurrent_runs,
            max_service_runs=orchestrator.max_service_runs,
            store=store,
        ),
        checkpointer=build_checkpointer(
            orchestrator.checkpointer, url=orchestrator.checkpointer_url
        ),
        node_timeout_s=orchestrator.node_timeout_s,
        metrics=metrics,
        models=CatalogModels(catalog),
        # state 압축(summarize)의 원문 저장소 — 없으면 그런 그래프는 검증과 시작에서 거절된다
        artifacts=state_artifact_store(orchestrator.state_artifact_root),
    )
    return RunService(catalog=catalog, deployments=deployments, submitter=submitter, store=store)


def _image_builder(orchestrator: Any, catalog: Catalog, author: Author) -> ImageBuilder | None:
    """`Declared → Built` 를 당기는 단계 (#265) — 재료와 결과 저장소가 모두 있어야 연다.

    Docker 는 배포와 같은 데몬을 쓴다. 빌드는 컨테이너를 띄우지 않으므로 배포 표면과는
    독립이다 — 배포를 열지 않아도 구울 수 있다.
    """
    if author.materials is None or orchestrator.build_store is None:
        log.warning(
            "image builds disabled — material_store and build_store must both be set",
        )
        return None
    from aaestro.runtime.docker.client import SdkDockerClient
    from aaestro.runtime.images import ImageBuilder, SqliteBuildStore

    return ImageBuilder(
        catalog=catalog,
        materials=author.materials,
        builds=SqliteBuildStore(path=orchestrator.build_store),
        client=SdkDockerClient(),
    )


def _material_store(orchestrator: Any) -> MaterialStore | None:
    """빌드 재료 저장소 — 설정이 없으면 재료 표면을 열지 않는다 (#264)."""
    if orchestrator.material_store is None:
        log.warning("build materials disabled — orchestrator.material_store is not set")
        return None
    from aaestro.materials import SqliteMaterialStore

    return SqliteMaterialStore(path=orchestrator.material_store)


def _access_registry(orchestrator: Any, catalog: Catalog, *, metrics: Any = None) -> Any:
    """권한 레지스트리 (#277) — `access_store` 가 없으면 권한 표면을 열지 않는다."""
    if orchestrator.access_store is None:
        log.warning("access control disabled — orchestrator.access_store is not set")
        return None
    from aaestro.access.baselines import (
        A2ABaseline,
        EgressBaseline,
        McpToolBaseline,
        MemoryBaseline,
    )
    from aaestro.access.model import ResourceKind
    from aaestro.access.registry import AccessRegistry
    from aaestro.access.store import SqliteAccessStore

    store = SqliteAccessStore(path=orchestrator.access_store)
    return AccessRegistry(
        store=store,
        catalog=catalog,
        stewards=frozenset(orchestrator.access_stewards),
        # 선언 판정은 그것을 쓰는 강제 지점과 함께 연결한다 — memory 는 Memory Service (#278),
        # a2a 는 피호출자의 A2A 서버 (#281), egress 와 원격 MCP 도구는 이그레스 프록시 (#293, #282)
        baselines={
            ResourceKind.MEMORY: MemoryBaseline(catalog),
            ResourceKind.A2A: A2ABaseline(
                catalog, store, stewards=frozenset(orchestrator.access_stewards)
            ),
            ResourceKind.EGRESS: EgressBaseline(catalog),
            ResourceKind.MCP_TOOL: McpToolBaseline(catalog),
        },
        metrics=metrics,
    )


def config_egress_without_registry() -> Exception:
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="runtime.egress_proxy requires the access registry (orchestrator.access_store)",
        details={"setting": "runtime.egress_proxy"},
    )


def config_missing_agent_url() -> Exception:
    """레지스트리를 켜면 에이전트가 닿을 주소가 있어야 한다 — 없으면 A2A 가 공유 키로 되돌아간다."""
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="access registry requires orchestrator.access_agent_url for agents to reach it",
        details={"setting": "orchestrator.access_agent_url"},
    )


def config_missing_enforcer_token(host: str) -> Exception:
    """판정 라우트를 무인증으로 loopback 밖에 열지 않는다."""
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message=(
            "access registry bound outside loopback requires orchestrator.access_enforcer_token"
        ),
        details={"setting": "orchestrator.access_enforcer_token", "host": host},
    )


def warn_if_agents_reach_the_host(runtime: Any, control_host: str) -> bool:
    """Warn when isolated agents can still reach this process through the network gateway (#303).

    프록시를 켜면 에이전트는 외부 경로 없는 내부 네트워크에 있다 (#280). 그래도 Docker 브리지의
    게이트웨이는 호스트 자신이라, 모든 인터페이스에 바인드한 호스트 서비스는 에이전트가 부를 수
    있다 — 판정을 거치지 않는 출구다. 운영자 API 는 토큰이 막지만, 표면이 열려 있다는 사실을 알린다.
    막는 절차는 ``deployments/docker/isolate-agent-network.sh`` 와 access-control runbook 에 있다.
    """
    if runtime.egress_proxy is None or is_loopback(control_host):
        return False
    log.warning(
        "control plane is reachable from isolated agents through the network gateway",
        host=control_host,
        network=runtime.network,
        remedy="bind to loopback, or block the agent network's gateway with "
        "deployments/docker/isolate-agent-network.sh",
    )
    return True


def config_missing_token(host: str) -> Exception:
    """loopback 밖에 무인증으로 열면 누구나 컨테이너를 띄울 수 있다 — CFG_001 로 막는다."""
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="control plane bound outside loopback requires orchestrator.control_token",
        details={"setting": "orchestrator.control_token", "host": host},
    )


def config_missing_store() -> Exception:
    """저장소 없이 뜨면 조회가 조용히 비어 보인다 — CFG_001 로 막는다."""
    from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="control plane requires orchestrator.run_store",
        details={"setting": "orchestrator.run_store"},
    )


if __name__ == "__main__":  # pragma: no cover - 프로세스 진입점
    main()
