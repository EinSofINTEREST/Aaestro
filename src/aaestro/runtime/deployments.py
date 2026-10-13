"""Deploying a graph — validated, launched as real containers, torn down cleanly.

`AgentLauncher` 는 기동·감시·재시작을 다 갖고 있었지만 **소유자가 없었다** (#243).
컨테이너를 띄우는 것은 compose 뿐이었고, "그래프를 배포한다" 는 동작이 시스템에
존재하지 않았다. 여기가 그 소유자다 — control plane 프로세스가 이것을 들고 있다.

01 Deploy 단계를 그대로 따른다: 검증 → 기동 → health OK → 기록. 하나라도 실패하면
**띄운 것을 되감는다** — 부분 기동은 없다 (03 MCP Startup 과 같은 원칙).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import sqlite3
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from urllib.parse import quote, urlsplit

import structlog

from aaestro.access.client import ACCESS_URL_ENV
from aaestro.access.model import ACCESS_CREDENTIAL_ENV
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import agent_name
from aaestro.protocols.mcp.transport import MCP_PROXY_URL_ENV
from aaestro.runtime.images import image_tag
from aaestro.runtime.launcher import LaunchedAgent, MemoryEndpoint
from aaestro.runtime.scope import ScopedSecrets
from aaestro.runtime.sidecars import McpSidecars, sidecars_of
from aaestro.runtime.spec import (
    A2A_EDGES_ENV,
    A2A_PEERS_ENV,
    A2A_SECRET_ENV,
    DEFAULT_CONTROL_PORT,
    container_name,
)

if TYPE_CHECKING:
    from aaestro.access.registry import AccessRegistry
    from aaestro.authoring import Author
    from aaestro.catalog import Catalog
    from aaestro.core.manifest import AgentManifest, ModelOverride
    from aaestro.orchestrator.topology import GraphTopology
    from aaestro.runtime.launcher import AgentLauncher
    from aaestro.runtime.sidecars import ProxyAttachment

log = structlog.get_logger(__name__)

DEFAULT_READY_TIMEOUT_S = 60.0
DEFAULT_READY_POLL_S = 0.5


class DeploymentStatus(StrEnum):
    """05 status 어휘를 따른다 — 실패는 한 이름으로."""

    STARTING = "starting"
    READY = "ready"
    FAILED = "failed"
    STOPPED = "stopped"
    LOST = "lost"
    """재시작 후 일부/전부를 다시 붙이지 못했다 — 운영자가 알아야 하므로 지우지 않는다.
    붙은 컨테이너는 그대로 감시하며, 선언도 계속 보호한다. 정리는 teardown 이 한다."""


@dataclass(frozen=True)
class DeployedAgent:
    """배포 안의 에이전트 하나 — 재시작 뒤 다시 붙을 수 있을 만큼만 기록한다.

    `token` 을 저장하는 이유: agentd 는 기동 시 받은 토큰을 바꿀 수 없다 (env).
    control plane 이 재시작한 뒤 그 컨테이너에 다시 말을 걸려면 같은 토큰이
    있어야 한다. 이 토큰은 사설 네트워크 안 Control API 의 per-agent 자격이고,
    저장소 파일은 이미 run 기록과 같은 신뢰 경계 안에 있다.
    """

    name: str
    replica: int
    container_id: str
    image: str
    control_port: int
    token: str
    a2a_port: int | None = None
    access_credential: str = ""
    """레지스트리가 발급한 에이전트 신원 (#277). 해체 후 재부착이 컨테이너를 다시 세울 때 같은
    값을 다시 주입해야 해서 `token` 과 같은 이유로 기록에 둔다. 레지스트리는 해시만 갖는다."""


@dataclass(frozen=True)
class DeploymentRecord:
    deployment_id: str
    graph: str
    version: str
    status: str
    agents: tuple[DeployedAgent, ...] = ()
    error: str | None = None
    updated_at: str = ""
    a2a_secret: str = ""
    """이 배포의 per-edge 토큰 서명 키 — 컨테이너들이 기동 시 받은 값이라
    재시작 뒤 같은 배포에 새 컨테이너를 세우려면 같은 키여야 한다."""
    declared: tuple[str, ...] = ()
    """이 배포가 쓰는 에이전트 이름 — 컨테이너가 서기 **전에** 적는다. `agents` 는
    Ready 가 되어야 채워지므로 그것만 보면 기동 중인 선언을 덮어쓸 수 있다."""
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    """배포 요청의 노드별 모델 override — ``exclude_unset`` 으로 덤프한 JSON 형태.
    run 이 토폴로지에 다시 입힐 때 "명시한 키만 덮는다" 가 유지되어야 한다."""


@runtime_checkable
class DeploymentStore(Protocol):
    """Where deployments are recorded so a restarted control plane can find them."""

    def upsert(self, record: DeploymentRecord) -> None: ...
    def get(self, deployment_id: str) -> DeploymentRecord | None: ...
    def list(self) -> Sequence[DeploymentRecord]: ...


def _storage_error(message: str, **details: str) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message=message, details=details
    )


def _now() -> str:
    return datetime.now(UTC).isoformat()


class InMemoryDeploymentStore:
    """테스트/단일 프로세스용."""

    def __init__(self) -> None:
        self._records: dict[str, DeploymentRecord] = {}

    def upsert(self, record: DeploymentRecord) -> None:
        self._records[record.deployment_id] = record

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        return self._records.get(deployment_id)

    def list(self) -> Sequence[DeploymentRecord]:
        return sorted(self._records.values(), key=lambda r: r.updated_at)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id TEXT PRIMARY KEY,
    graph         TEXT NOT NULL,
    version       TEXT NOT NULL,
    status        TEXT NOT NULL,
    agents        TEXT NOT NULL,
    error         TEXT,
    updated_at    TEXT NOT NULL,
    a2a_secret    TEXT NOT NULL DEFAULT '',
    declared      TEXT NOT NULL DEFAULT '[]',
    model_overrides TEXT NOT NULL DEFAULT '{}'
);
"""

_ADDED_COLUMNS = {
    "a2a_secret": "TEXT NOT NULL DEFAULT ''",
    "declared": "TEXT NOT NULL DEFAULT '[]'",
    "model_overrides": "TEXT NOT NULL DEFAULT '{}'",
}
"""첫 스키마 이후 더해진 컬럼 — `_migrate` 가 옛 DB 에 채운다. 새로 더할 때 여기에도 적는다."""


@dataclass
class SqliteDeploymentStore:
    """runstore 와 같은 배치 — 파일 하나, 프로세스 재시작을 넘긴다.

    Control plane 은 별도 스레드에서 서빙된다 (`SqliteRunStore` 와 같은 사정) —
    연결 생성/마이그레이션과 모든 쿼리를 `_lock` 으로 직렬화해 경쟁 상태를 막는다.
    """

    path: str | Path
    _conn: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            try:
                self._conn = sqlite3.connect(
                    str(self.path), isolation_level=None, check_same_thread=False
                )
                self._conn.row_factory = sqlite3.Row
                self._conn.execute(_SCHEMA)
                _migrate(self._conn)
            except sqlite3.Error as err:
                raise _storage_error(
                    "deployment store could not be opened", path=str(self.path)
                ) from err
        return self._conn

    def upsert(self, record: DeploymentRecord) -> None:
        try:
            with self._lock:
                self._connect().execute(
                    "INSERT INTO deployments VALUES (?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(deployment_id) DO UPDATE SET "
                    "graph=excluded.graph, version=excluded.version, status=excluded.status, "
                    "agents=excluded.agents, error=excluded.error, updated_at=excluded.updated_at, "
                    "a2a_secret=excluded.a2a_secret, declared=excluded.declared, "
                    "model_overrides=excluded.model_overrides",
                    (
                        record.deployment_id,
                        record.graph,
                        record.version,
                        record.status,
                        json.dumps([a.__dict__ for a in record.agents]),
                        record.error,
                        record.updated_at,
                        record.a2a_secret,
                        json.dumps(list(record.declared)),
                        json.dumps(record.model_overrides),
                    ),
                )
        except sqlite3.Error as err:
            raise _storage_error(
                "deployment could not be stored", deployment_id=record.deployment_id
            ) from err

    def get(self, deployment_id: str) -> DeploymentRecord | None:
        with self._lock:
            row = (
                self._connect()
                .execute("SELECT * FROM deployments WHERE deployment_id = ?", (deployment_id,))
                .fetchone()
            )
        return _row(row) if row else None

    def list(self) -> Sequence[DeploymentRecord]:
        with self._lock:
            rows = (
                self._connect().execute("SELECT * FROM deployments ORDER BY updated_at").fetchall()
            )
        return [_row(r) for r in rows]


def _migrate(conn: sqlite3.Connection) -> None:
    """있는 DB 에 빠진 컬럼을 더한다 — `CREATE TABLE IF NOT EXISTS` 는 그러지 않는다.

    #254: 컬럼이 늘어난 코드로 옛 DB 를 열면 기동이 죽었다. 기본값이 있는 컬럼만 더하므로
    기존 행은 그대로 읽힌다.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(deployments)")}
    for name, declaration in _ADDED_COLUMNS.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE deployments ADD COLUMN {name} {declaration}")  # noqa: S608 — 상수


def _row(row: sqlite3.Row) -> DeploymentRecord:
    """컬럼 **이름**으로 읽는다 — 순서에 기대면 컬럼이 늘 때마다 깨진다 (#254)."""
    return DeploymentRecord(
        deployment_id=row["deployment_id"],
        graph=row["graph"],
        version=row["version"],
        status=row["status"],
        agents=tuple(DeployedAgent(**a) for a in json.loads(row["agents"])),
        error=row["error"],
        updated_at=row["updated_at"],
        a2a_secret=row["a2a_secret"],
        declared=tuple(json.loads(row["declared"])),
        model_overrides=json.loads(row["model_overrides"]),
    )


DECLARATION_MOUNT_PATH = "/app/declaration"
"""에이전트 선언 **디렉토리**가 걸리는 자리 (#275).

매니페스트 파일 하나를 바인드하면, 저작 경로의 원자적 교체(임시 파일 + rename)가 떠 있는
컨테이너에 반영되지 않는다 — 바인드는 inode 에 묶여 옛 파일을 계속 보인다. 디렉토리를 걸면
경로 조회가 새 파일을 찾는다 (02 Volumes)."""
MANIFEST_MOUNT_PATH = f"{DECLARATION_MOUNT_PATH}/manifest.yaml"
MANIFEST_ENV = "AAESTRO_MANIFEST"
"""agentd 가 매니페스트를 찾는 env — 마운트 자리를 가리키게 주입한다."""
MODULES_MOUNT_PATH = "/app/modules"
"""agentd 의 기본 `AAESTRO_ROOT` 아래 모듈 자리 — base 이미지 계약."""

MODULE_TYPES = ("skillsets", "promptsets", "memorysets")


@dataclass(frozen=True)
class Provision:
    """runtime 이 한 에이전트 컨테이너에 실어 보내는 것 — 선언(mounts)과 배선(env).

    03 Discovery: 에이전트는 peer 주소를 스스로 알아내지 않는다. 그래프의
    `connections` 를 edge/peer env 로 번역해 주는 곳이 여기다 — compose 가
    손으로 적던 값이다.
    """

    env: Mapping[str, str]
    mounts: tuple[Mapping[str, Any], ...]
    a2a_port: int | None
    image: str | None = None
    """구운 태그 — 커스텀 에이전트만. None 이면 매니페스트 선언이나 base 이미지 (#266)."""


@runtime_checkable
class BuiltImages(Protocol):
    """배포가 빌드 단계에 묻는 두 가지 — `ImageBuilder` 가 만족한다 (#266).

    배포는 굽지 않는다 (02 Lifecycle 1). 굽혔는지 **확인만** 한다.
    """

    def needs_build(self, agent: str, version: str) -> bool: ...
    def record_of(self, agent: str, version: str) -> Any: ...


_HOLDS_DECLARATIONS = (DeploymentStatus.STARTING, DeploymentStatus.READY, DeploymentStatus.LOST)
"""컨테이너가 (아직/여전히) 선언을 쓰고 있을 수 있는 상태 — authoring 이 손대면 안 된다."""


def _agents_declared(record: DeploymentRecord) -> frozenset[str]:
    return frozenset(record.declared) | frozenset(a.name for a in record.agents)


def not_deployed(deployment_id: str) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=f"unknown deployment: {deployment_id}",
        details={"deployment_id": deployment_id},
    )


@dataclass
class DeploymentManager:
    """Owns the launcher and turns graphs into running agent containers.

    Attributes:
        catalog: 그래프와 매니페스트를 읽는 곳.
        author: 배포 전 검증 (`Author.validate`) — 실패하면 아무것도 기동하지 않는다.
        launcher: 컨테이너를 실제로 띄우는 runtime.
        store: 배포 기록 — 프로세스 재시작을 넘겨야 한다.
        secrets_env: local/group/global 세 스코프의 값 원천. v0.1 은 control plane
            프로세스의 환경변수 하나다 — 스코프별 **선언**(allowlist / group.secrets /
            global.secrets)이 무엇을 통과시킬지 정하고, 값은 여기서 온다.
        agent_env: 모든 에이전트에 주입하는 **비밀이 아닌** 인프라 env (provider base
            URL 등). secrets 와 달리 allowlist 를 거치지 않는다 — 어디에 provider 가
            있는지는 자격증명이 아니라 배치다. 여기 secret 을 넣으면 allowlist 를 우회하는
            것이므로, 이름이 secret 패턴이면 거부한다.
        memory_url / memory_tokens: Memory Service 주소와 에이전트별 토큰 파일 내용.
        ready_timeout_s / poll: health 대기. 06 — sleep 은 주입한다.
        images: 빌드 단계 (#266). 재료가 있는 에이전트는 그 버전이 `built` 여야 기동한다.
            None 이면 빌드 표면이 꺼진 것이고 매니페스트의 이미지를 그대로 쓴다.
    """

    catalog: Catalog
    author: Author
    launcher: AgentLauncher
    store: DeploymentStore
    secrets_env: Mapping[str, str] = field(default_factory=dict)
    agent_env: Mapping[str, str] = field(default_factory=dict)
    memory_url: str | None = None
    memory_tokens: Mapping[str, str] = field(default_factory=dict)
    images: BuiltImages | None = None
    access: AccessRegistry | None = None
    """권한 레지스트리 (#277) — 있으면 에이전트마다 신원을 발급해 주입하고,
    해체·되감기 때 폐기한다."""
    access_url: str | None = None
    """에이전트 컨테이너에서 닿는 레지스트리 주소 (#281) — A2A 표를 받고 확인하는 곳."""
    egress: EgressEndpoints | None = None
    """이그레스 프록시 (#293) — 있으면 외부 HTTPS·모델 API 를 프록시로 보내고 모델 키를 넣지
    않는다."""
    sidecar_proxy: ProxyAttachment | None = None
    """사이드카 네트워크에 붙일 프록시 컨테이너 (#304) — 프록시를 켠 배포에서 사이드카를 쓰려면
    필요하다. 에이전트가 사이드카에 직접 닿지 않고 프록시의 도구 판정을 거치게 한다."""
    ready_timeout_s: float = DEFAULT_READY_TIMEOUT_S
    ready_poll_s: float = DEFAULT_READY_POLL_S
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self) -> None:
        secretish = [k for k in self.agent_env if _looks_secret(k)]
        if secretish:
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_002,
                message="agent_env must not carry secrets — those go through env_allowlist",
                details={"keys": secretish},
            )

    # --- 조회 --------------------------------------------------------------

    def get(self, deployment_id: str) -> DeploymentRecord:
        record = self.store.get(deployment_id)
        if record is None:
            raise not_deployed(deployment_id)
        return self._refresh(record)

    def deployments(self) -> Sequence[DeploymentRecord]:
        return [self._refresh(r) for r in self.store.list()]

    def in_use(self, kind: str, name: str) -> bool:
        """authoring 이 묻는다 — 배포 중인 선언은 지우거나 덮어쓰지 못한다 (#242)."""
        # LOST 도 포함한다 — 일부만 다시 붙은 배포의 컨테이너가 아직 그 선언으로 돈다
        live = [r for r in self.store.list() if r.status in _HOLDS_DECLARATIONS]
        if kind == "graph":
            return any(r.graph == name for r in live)
        return any(name in _agents_declared(r) for r in live)

    # --- 배포 --------------------------------------------------------------

    async def deploy(
        self, graph_name: str, *, model_overrides: Mapping[str, ModelOverride] | None = None
    ) -> DeploymentRecord:
        """Validate, launch every agent, wait for health, record.

        01 Deploy: 검증 실패 시 아무것도 기동하지 않는다. 기동 중 하나라도 실패하면
        띄운 것을 전부 되감는다 — 유령 컨테이너는 05 Consistency 3 위반이다.

        ``model_overrides`` (노드 id → 부분 모델 선언) 는 **검증 전에** 토폴로지에 입힌다 —
        override 가 manifest 밖 provider 로 바꾸는 것을 검증이 보고 거절해야 한다.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` if an override names an unknown node,
                ``VAL_001`` if the graph fails deployment validation.
        """
        overrides = dict(model_overrides or {})
        topology = self.catalog.graph(graph_name).with_model_overrides(overrides)
        report = self.author.validate(graphs=[topology], with_saved_graphs=False)
        if not report.ok:
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_001,
                message="graph failed deployment validation",
                details={"graph": graph_name, "findings": [f.message for f in report.findings]},
            )

        manifests = self._agents_of(topology)
        # 기록을 남기기 전에 — 거절된 배포는 실패한 배포가 아니라 시작되지 않은 배포다
        self._check_images(manifests)
        self._check_sidecars(manifests)
        deployment_id = f"dep-{uuid.uuid4().hex[:12]}"
        record = DeploymentRecord(
            deployment_id=deployment_id,
            graph=graph_name,
            version=topology.metadata.version,
            status=DeploymentStatus.STARTING,
            updated_at=_now(),
            a2a_secret=secrets.token_urlsafe(32),
            # 첫 await 전에 적는다 — 기동 중에도 authoring 이 이 선언을 못 건드리게
            declared=tuple(m.name for m in manifests),
            model_overrides={
                node_id: o.model_dump(mode="json", exclude_unset=True)
                for node_id, o in overrides.items()
            },
        )
        self.store.upsert(record)
        bound = self._bind_log(record)

        provisions: dict[str, Provision] = {}
        launched: list[LaunchedAgent] = []
        attempted: list[str] = []
        try:
            credentials = self._issue_identities(manifests, deployment_id, graph_name)
            provisions = self._provision(
                topology, manifests, a2a_secret=record.a2a_secret, credentials=credentials
            )
            for manifest in manifests:
                # 사이드카부터 선다 — 반쯤 선 사이드카도 되감기가 치우도록 먼저 적는다
                attempted.append(manifest.name)
                launched.append(await self._launch(manifest, provisions[manifest.name]))
            await self._wait_ready(launched)
        except BaseException as err:
            await self._rollback(launched)
            for name in attempted:
                await self.sidecars.stop(name)
            self._release_ports(provisions)
            # 되감긴 배포의 신원이 살아 있으면 존재하지 않는 컨테이너 이름으로 강제 지점을 통과한다
            self._revoke_identities(deployment_id)
            failed = DeploymentRecord(
                **{
                    **record.__dict__,
                    "status": DeploymentStatus.FAILED,
                    "error": str(err),
                    "updated_at": _now(),
                }
            )
            self.store.upsert(failed)
            bound.error(
                "deployment failed and was rolled back", agents=len(launched), error_code=_code(err)
            )
            raise

        ready = DeploymentRecord(
            **{
                **record.__dict__,
                "status": DeploymentStatus.READY,
                "agents": tuple(
                    _deployed(
                        a,
                        self.launcher.issuer.known(a.agent) or "",
                        provisions[a.agent].env.get(ACCESS_CREDENTIAL_ENV, ""),
                    )
                    for a in launched
                ),
                "updated_at": _now(),
            }
        )
        self.store.upsert(ready)
        bound.info("deployment ready", agents=len(launched))
        return ready

    async def teardown(self, deployment_id: str) -> DeploymentRecord:
        """Drain then stop every agent (02 Lifecycle 4·5). 기록은 남긴다."""
        record = self.get(deployment_id)
        if record.status in (DeploymentStatus.STOPPED, DeploymentStatus.FAILED):
            return record
        names = list(dict.fromkeys(agent.name for agent in record.agents))
        try:
            for name in names:
                await self.launcher.stop(name)
        finally:
            # 에이전트 정지가 실패해도 사이드카는 치운다 — 03 Placement 1, lifecycle 을 함께한다
            for name in names:
                await self.sidecars.stop(name)
            # 해체를 요청한 이상 신원은 죽인다 — 정지 하나가 실패했다고 모든 신원이 살아 남으면
            # 운영자가 끝냈다고 믿는 배포가 강제 지점을 계속 통과한다
            self._revoke_identities(deployment_id)
        stopped = DeploymentRecord(
            **{**record.__dict__, "status": DeploymentStatus.STOPPED, "updated_at": _now()}
        )
        self.store.upsert(stopped)
        self._bind_log(stopped).info("deployment stopped", agents=len(record.agents))
        return stopped

    async def reattach(self) -> Sequence[DeploymentRecord]:
        """After a restart, find the containers we left running and pick them back up.

        Docker 가 컨테이너를 들고 있으므로 기록과 대조한다 (05 Consistency 3).
        사라진 컨테이너는 `lost` 로 표시한다 — 조용히 지우면 운영자가 모른다.
        """
        touched: list[DeploymentRecord] = []
        for record in self.store.list():
            if record.status == DeploymentStatus.STARTING:
                # 기동 도중 죽은 배포다 — 붙일 기록(신원 포함)이 없으니 발급된 신원만 남는다
                self._revoke_orphaned_identities(record)
            if record.status != DeploymentStatus.READY:
                continue
            try:
                restart_args = self._restart_args_of(record)
            except AaestroError as err:
                # 선언이나 secrets 가 사라졌으면 다시 세울 수 없다 — 붙지 않고
                # 운영자에게 보인다. 조용히 붙이면 첫 재시작에서 넘어진다
                self._mark_lost(record, f"cannot rebuild from declarations: {err.message}", touched)
                continue
            if not await self._isolated(record, touched):
                continue
            unready = await self._resume_sidecars(restart_args)
            if unready:
                self._mark_lost(record, f"mcp sidecars not restored: {', '.join(unready)}", touched)
                continue
            missing = []
            for agent in record.agents:
                # 이름으로 찾는다 — launcher 의 재시작이 컨테이너를 갈아 끼우면
                # id 와 control 포트가 바뀌지만 이름은 같은 자리를 가리킨다
                live = await self._live(agent)
                if live is None or not await self.launcher.adopt(
                    agent.name,
                    replica=agent.replica,
                    container_id=live[0],
                    image=agent.image,
                    control_host=live[1],
                    control_port=live[2],
                    token=agent.token,
                    a2a_port=agent.a2a_port,
                    restart_args=restart_args[agent.name],
                ):
                    missing.append(agent.name)
            if missing:
                self._mark_lost(record, f"containers missing: {', '.join(missing)}", touched)
            else:
                touched.append(self._refresh(record))
        return touched

    async def _isolated(self, record: DeploymentRecord, touched: list[DeploymentRecord]) -> bool:
        """재부착 전에 배포의 모든 컨테이너가 runtime 의 격리대로 서 있는지 본다 (#280).

        하나라도 아니면 아무것도 붙이지 않고 lost 로 두며 그 배포의 신원을 회수한다 — 외부 경로가
        남은 컨테이너가 신원으로 메모리·peer·모델에 계속 닿지 못하게. 컨테이너는 지우지 않는다:
        운영자가 원인을 보고 해체한다.
        """
        engine = self.launcher.engine
        for agent in record.agents:
            try:
                container_id = await asyncio.to_thread(
                    engine.client.find, container_name(agent.name, agent.replica)
                )
                if container_id is None:
                    continue  # 없는 컨테이너는 아래 대조가 missing 으로 보고한다
                await engine.verify_isolation(agent.name, agent.image, container_id)
            except AaestroError as err:
                reason, code = "network isolation mismatch", err.code
            except Exception as err:  # noqa: BLE001 — 격리를 확인하지 못했다: 붙이지 않는 쪽으로 닫는다
                # 조회 사이에 컨테이너가 사라지거나 daemon 이 답하지 않으면 SDK 예외가 온다. 여기서
                # 새면 이 배포는 lost 도 신원 회수도 없이 남고, 뒤의 배포는 재부착조차 되지 않는다
                reason, code = "network isolation unverifiable", type(err).__name__
            else:
                continue
            self._bind_log(record).error(
                "deployment not reattached — agent network isolation not confirmed",
                agent=agent.name,
                reason=reason,
                error_code=code,
            )
            self._mark_lost(record, f"{reason}: {agent.name}", touched)
            self._revoke_orphaned_identities(record)
            return False
        return True

    async def _live(self, agent: DeployedAgent) -> tuple[str, str, int] | None:
        """이 자리에 지금 서 있는 컨테이너의 (id, control 주소, control 포트) — 없으면 None."""
        engine = self.launcher.engine
        container_id = await asyncio.to_thread(
            engine.client.find, container_name(agent.name, agent.replica)
        )
        if container_id is None:
            return None
        try:
            host, port = await engine.control_address(container_id, DEFAULT_CONTROL_PORT)
        except Exception:  # noqa: BLE001 — 주소가 없으면 붙을 수 없는 컨테이너다
            return None
        return container_id, host, port

    async def _resume_sidecars(self, restart_args: Mapping[str, Mapping[str, Any]]) -> list[str]:
        """재부착 전에 사이드카를 되살린다 — 떠 있으면 그대로, 사라졌으면 같은 선언으로 다시.

        Docker 의 재시작 상한을 넘겨 멈춘 사이드카도 여기서 다시 선다. 세울 수 없으면 그 에이전트
        이름을 돌려준다 — 배포는 lost 로 드러난다.
        """
        unready = []
        for name, args in restart_args.items():
            try:
                await self.sidecars.start(args["manifest"], args["secrets"], reuse=True)
            except AaestroError as err:
                log.error("mcp sidecars could not be restored", agent=name, error_code=err.code)
                unready.append(name)
        return unready

    def _refresh(self, record: DeploymentRecord) -> DeploymentRecord:
        """launcher 가 아는 현재 컨테이너로 기록을 맞춘다 — 재시작이 id/포트를 바꾼다."""
        if record.status != DeploymentStatus.READY:
            return record
        agents = []
        for agent in record.agents:
            launched = self.launcher.launched.get((agent.name, agent.replica))
            agents.append(_relaunched(agent, launched) if launched is not None else agent)
        if tuple(agents) == record.agents:
            return record
        refreshed = DeploymentRecord(
            **{**record.__dict__, "agents": tuple(agents), "updated_at": _now()}
        )
        self.store.upsert(refreshed)
        return refreshed

    def _mark_lost(
        self, record: DeploymentRecord, reason: str, touched: list[DeploymentRecord]
    ) -> None:
        lost = DeploymentRecord(
            **{
                **record.__dict__,
                "status": DeploymentStatus.LOST,
                "error": reason,
                "updated_at": _now(),
            }
        )
        self.store.upsert(lost)
        touched.append(lost)
        self._bind_log(lost).warning("deployment lost", reason=reason)

    # --- 내부 --------------------------------------------------------------

    def _agents_of(self, topology: GraphTopology) -> list[AgentManifest]:
        """노드별 에이전트 — 여러 노드가 같은 에이전트를 쓰면 **한 번만**."""
        seen: dict[str, AgentManifest] = {}
        for node in topology.spec.nodes:
            if node.agent is None:
                continue
            name = agent_name(node.agent)
            if name not in seen:
                seen[name] = self.catalog.agent(name)
        return list(seen.values())

    def _provision(
        self,
        topology: GraphTopology,
        manifests: Sequence[AgentManifest],
        *,
        a2a_secret: str,
        ports: Mapping[str, int] | None = None,
        credentials: Mapping[str, str] | None = None,
    ) -> dict[str, Provision]:
        """그래프 하나의 에이전트 전부에 대한 선언 마운트와 A2A 배선.

        peer 주소에 상대의 포트가 들어가므로 포트는 **기동 전에 전부** 정한다.
        `ports` 를 주면(재부착) 할당하지 않고 그 값을 쓴다 — 컨테이너 안의 env 는
        이미 그 포트로 굳어 있다.
        """
        agent_of_node = {
            node.id: agent_name(node.agent) for node in topology.spec.nodes if node.agent
        }
        edges = [
            (agent_of_node[c.caller], agent_of_node[c.callee])
            for c in topology.spec.connections
            if c.caller in agent_of_node and c.callee in agent_of_node
        ]
        assigned: dict[str, int] = dict(ports or {})
        if ports is None and self.launcher.ports is not None:
            try:
                for manifest in manifests:
                    if manifest.spec.a2a.enabled:
                        assigned[manifest.name] = self.launcher.ports.allocate(manifest.name)
            except AaestroError:
                # 범위가 말랐다 — 여기서 잡은 것은 여기서 돌려준다
                for name in assigned:
                    self.launcher.ports.release(name)
                raise

        stewards = self._running_stewards()
        provisions: dict[str, Provision] = {}
        for manifest in manifests:
            # base 이미지 기본값(/app/manifest.yaml)이 아니라 디렉토리 마운트 안을 읽게 한다
            env: dict[str, str] = {MANIFEST_ENV: MANIFEST_MOUNT_PATH}
            credential = (credentials or {}).get(manifest.name)
            if credential:
                # 강제 지점에 내미는 에이전트 신원 (01 Access Control 6) — 배선이라 provision 에
                env[ACCESS_CREDENTIAL_ENV] = credential
                if self.access_url:
                    env[ACCESS_URL_ENV] = self.access_url
                if self.egress is not None:
                    env.update(self.egress.env_for(manifest.name, credential))
            # 권한 에이전트는 그래프 배선이 아니라 설정이 정한다 — 떠 있으면 모두가 요청할 수 있다.
            # 호출은 표로만 증명되므로 레지스트리 모드에서만 잇는다
            reachable = (
                {s: a for s, a in stewards.items() if s != manifest.name}
                if credential and self.access_url
                else {}
            )
            own_edges = [*edges, *((manifest.name, s) for s in reachable)]
            if own_edges:
                env[A2A_EDGES_ENV] = ",".join(f"{caller}>{callee}" for caller, callee in own_edges)
                if not (credential and self.access_url):
                    # 레지스트리 모드에서는 공유 서명 키를 넣지 않는다 — 그래프의 모든 에이전트가
                    # 쥐는 키로는 누구든 다른 에이전트 행세의 토큰을 만든다 (#281)
                    env[A2A_SECRET_ENV] = a2a_secret
            peers = [
                f"{callee}={container_name(callee, 0)}:{assigned[callee]}"
                for caller, callee in edges
                if caller == manifest.name and callee in assigned
            ]
            peers += [f"{s}={address}" for s, address in reachable.items() if s not in assigned]
            if peers:
                env[A2A_PEERS_ENV] = ",".join(peers)
            provisions[manifest.name] = Provision(
                env=env,
                mounts=self._mounts(manifest.name),
                a2a_port=assigned.get(manifest.name),
                image=self._baked_image(manifest),
            )
        return provisions

    @property
    def sidecars(self) -> McpSidecars:
        """이 배포들의 MCP 사이드카 — 상태가 없으므로 부를 때마다 만든다."""
        return McpSidecars(client=self.launcher.engine.client, proxy=self.sidecar_proxy)

    def _check_sidecars(self, manifests: Sequence[AgentManifest]) -> None:
        """프록시를 켠 배포는 프록시를 사이드카 네트워크에 붙일 수 있어야 사이드카를 띄운다 (#304).

        붙일 수 없으면 에이전트를 사이드카에 직접 잇는 수밖에 없는데, 그러면 도구 단위 판정이
        에이전트 컨테이너 안의 검사로 떨어진다 — 강제가 아니다. 띄우지 않는다.

        Raises:
            AaestroError: CONFIG/``CFG_001`` — ``runtime.egress_proxy.container`` 가 없다.
        """
        if self.egress is None or self.sidecar_proxy is not None:
            return
        declaring = [m.name for m in manifests if sidecars_of(m)]
        if declaring:
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_001,
                message="mcp sidecars behind the egress proxy need runtime.egress_proxy.container",
                details={"agents": declaring, "setting": "runtime.egress_proxy.container"},
            )

    def _running_stewards(self) -> dict[str, str]:
        """지금 떠 있는 권한 에이전트의 A2A 주소 — 이름 → ``container:port``.

        다른 배포에 속한 컨테이너다. 권한 에이전트를 나중에 세우면 이미 떠 있던 에이전트의 env 에는
        없다 — env 는 기동 시 고정되므로 그 에이전트를 다시 배포해야 한다 (운영 문서에 적는다).
        """
        if self.access is None:
            return {}
        found: dict[str, str] = {}
        for steward in sorted(self.access.stewards):
            launched = self.launcher.launched.get((steward, 0))
            if launched is not None and launched.a2a_port is not None:
                found[steward] = f"{container_name(steward, 0)}:{launched.a2a_port}"
        return found

    def _issue_identities(
        self, manifests: Sequence[AgentManifest], deployment_id: str, graph: str
    ) -> dict[str, str]:
        """배포할 에이전트마다 신원 하나 — 레지스트리가 없으면 발급하지 않는다."""
        if self.access is None:
            return {}
        return {
            m.name: self.access.issue_identity(m.name, deployment_id, graph=graph)
            for m in manifests
        }

    def _revoke_identities(self, deployment_id: str) -> None:
        if self.access is not None:
            self.access.revoke_deployment(deployment_id)

    def _revoke_orphaned_identities(self, record: DeploymentRecord) -> None:
        if self.access is not None and self.access.revoke_deployment(record.deployment_id):
            self._bind_log(record).warning("identities of an interrupted deployment revoked")

    def _baked_image(self, manifest: AgentManifest) -> str | None:
        """재료가 있으면 프레임워크가 소유한 태그, 없으면 None (declarative agent)."""
        version = manifest.metadata.version
        if self.images is None or not self.images.needs_build(manifest.name, version):
            return None
        return image_tag(manifest.name, version)

    def _check_images(self, manifests: Sequence[AgentManifest]) -> None:
        """굽지 않은 커스텀 에이전트는 기동하지 않는다 (#266).

        배포가 대신 굽지 않는다 — 02 Lifecycle 1 은 런타임 중 빌드를 금지한다.
        재배포(reattach/restart)는 이 검사를 거치지 않는다: 이미 떠 있던 것을 다시
        세우는 일이고, 그 사이 누가 다시 굽다 실패했다고 살아 있는 배포를 잃으면 안 된다.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` 매니페스트가 다른 이미지를 선언,
                RUNTIME/``RT_012`` 그 버전이 아직 `built` 가 아니거나, 기록의 태그가
                띄울 태그와 다름 (HTTP 409).
        """
        for manifest in manifests:
            tag = self._baked_image(manifest)
            if tag is None or self.images is None:
                continue
            declared = manifest.spec.runtime.image
            if declared and declared != tag:
                # 두 곳이 다른 이미지를 가리키면 무엇이 도는지 알 수 없다
                raise AaestroError(
                    category=ErrorCategory.VALIDATION,
                    code=ErrorCode.VAL_002,
                    message="manifest declares an image other than its built one",
                    agent=manifest.name,
                    details={"declared": declared, "built": tag},
                )
            record = self.images.record_of(manifest.name, manifest.metadata.version)
            if record is None or not record.ok:
                raise AaestroError(
                    category=ErrorCategory.RUNTIME,
                    code=ErrorCode.RT_012,
                    message="agent image is not built — build it before deploying",
                    agent=manifest.name,
                    details={
                        "image": tag,
                        "version": manifest.metadata.version,
                        "build_status": None if record is None else record.status,
                        "build_error": None if record is None else record.error,
                    },
                )
            if record.image != tag:
                # 태그 규칙이 코드에서 바뀌면 (예: 이미지 접두사 변경) 이전에 구운 기록은
                # 옛 태그를 가리킨 채 `built` 로 남는다 — 상태만 믿으면 런타임이 새 태그를
                # 띄우다 기동 단계에서 터진다
                raise AaestroError(
                    category=ErrorCategory.RUNTIME,
                    code=ErrorCode.RT_012,
                    message="agent image was built under another tag — rebuild it before deploying",
                    agent=manifest.name,
                    details={
                        "image": tag,
                        "recorded_image": record.image,
                        "version": manifest.metadata.version,
                        "build_status": record.status,
                        "build_error": record.error,
                    },
                )

    def _restart_args_of(self, record: DeploymentRecord) -> dict[str, dict[str, Any]]:
        """기록된 배포의 컨테이너를 같은 선언으로 다시 세우는 데 필요한 것 전부."""
        topology = self.catalog.graph(record.graph)
        manifests = self._agents_of(topology)
        ports = {a.name: a.a2a_port for a in record.agents if a.a2a_port is not None}
        credentials = {a.name: a.access_credential for a in record.agents if a.access_credential}
        provisions = self._provision(
            topology, manifests, a2a_secret=record.a2a_secret, ports=ports, credentials=credentials
        )
        # 이미지는 **기록에서** 가져온다 — 지금 스토어로 다시 유도하면, 재료·빌드 스토어 없이
        # 재시작한 control plane 에서는 None 이 되어 다음 재시작이 base 이미지로 떨어진다
        deployed = {a.name: a.image for a in record.agents}
        return {
            m.name: {
                "manifest": m,
                "secrets": self._env_for(m, provisions[m.name]),
                "memory": self._memory_for(m, provisions[m.name]),
                "mounts": provisions[m.name].mounts,
                "image": deployed.get(m.name) or provisions[m.name].image,
                "networks": self.sidecars.agent_networks(m),
            }
            for m in manifests
        }

    def _mounts(self, agent: str) -> tuple[Mapping[str, Any], ...]:
        """base 이미지에 선언을 들여보낸다 — 선언 디렉토리와 모듈 루트들, 전부 읽기 전용.

        전부 **디렉토리** 마운트다 (#275). 없는 모듈 루트는 걸지 않는다: Docker 는 없는
        호스트 경로를 root 소유 디렉토리로 만들어 버린다.
        """
        roots = self.catalog.roots
        mounts: list[Mapping[str, Any]] = [
            {
                # 경로 검증을 거친 매니페스트의 디렉토리 — 루트 밖을 걸지 않는다 (#273)
                "name": str(self.catalog.agent_path(agent).parent),
                "mount_path": DECLARATION_MOUNT_PATH,
                "read_only": True,
            }
        ]
        for module_type in MODULE_TYPES:
            root = roots.for_type(module_type).resolve()
            if root.is_dir():
                mounts.append(
                    {
                        "name": str(root),
                        "mount_path": f"{MODULES_MOUNT_PATH}/{module_type}",
                        "read_only": True,
                    }
                )
        return tuple(mounts)

    def _env_for(self, manifest: AgentManifest, provision: Provision) -> dict[str, str]:
        """컨테이너 env — 인프라(agent_env) < 스코프 secrets < 배선(provision) 순으로 겹친다."""
        scoped = ScopedSecrets.for_agent(
            manifest,
            groups=self.catalog.groups().items,
            local=self.secrets_env,
            group_values=self.secrets_env,
            global_values=self.secrets_env,
        ).env_for(tuple(manifest.spec.runtime.env_allowlist))
        if self.egress is not None:
            # 프록시가 종단하는 서비스의 키는 에이전트에게 주지 않는다 — 프록시가 주입한다 (02)
            terminated = PROXY_TERMINATED_SECRETS | remote_mcp_credentials(manifest)
            scoped = {k: v for k, v in scoped.items() if k not in terminated}
        return {**self.agent_env, **scoped, **provision.env}

    def _memory_for(self, manifest: AgentManifest, provision: Provision) -> MemoryEndpoint | None:
        """Memory Service 주소와 내밀 자격.

        레지스트리가 있으면 자격은 **에이전트 신원**이다 — Memory Service 가 요청마다 레지스트리에
        묻는다. 그때 정적 토큰으로 되돌아가면 회수가 닿지 않는 뒷문이 되므로 쓰지 않는다.
        """
        if self.access is not None:
            credential = provision.env.get(ACCESS_CREDENTIAL_ENV)
            if self.memory_url and credential:
                return MemoryEndpoint(url=self.memory_url, token=credential)
            return None
        token = self.memory_tokens.get(manifest.name)
        if self.memory_url and token:
            return MemoryEndpoint(url=self.memory_url, token=token)
        return None

    async def _launch(self, manifest: AgentManifest, provision: Provision) -> LaunchedAgent:
        env = self._env_for(manifest, provision)
        sidecars = self.sidecars
        # 에이전트보다 먼저 — agentd 가 기동하며 사이드카 세션을 연다 (03 Startup Sequence 3)
        await sidecars.start(manifest, env)
        return await self.launcher.start(
            manifest,
            secrets=env,
            memory=self._memory_for(manifest, provision),
            mounts=provision.mounts,
            a2a_port=provision.a2a_port,
            image=provision.image,
            networks=sidecars.agent_networks(manifest),
        )

    def _release_ports(self, provisions: Mapping[str, Provision]) -> None:
        """되감기 뒤 미리 잡아 둔 포트를 돌려준다 — 기동 전에 실패하면 stop 이 안 돌려준다."""
        if self.launcher.ports is None:
            return
        for agent, provision in provisions.items():
            if provision.a2a_port is not None:
                self.launcher.ports.release(agent)

    async def _wait_ready(self, launched: Sequence[LaunchedAgent]) -> None:
        deadline = self.clock() + self.ready_timeout_s
        while True:
            pending = [a.agent for a in launched if not a.lifecycle.accepts_tasks]
            if not pending:
                return
            if self.clock() >= deadline:
                raise AaestroError(
                    category=ErrorCategory.RUNTIME,
                    code=ErrorCode.RT_002,
                    message="agents did not become healthy before the deploy deadline",
                    details={"pending": pending, "timeout_s": self.ready_timeout_s},
                    retryable=True,
                )
            await self.sleep(self.ready_poll_s)

    async def _rollback(self, launched: Sequence[LaunchedAgent]) -> None:
        for agent in launched:
            try:
                await self.launcher.stop(agent.agent, replica=agent.replica)
            except Exception as err:  # noqa: BLE001 — 되감기 중 하나가 실패해도 나머지를 계속 정리한다
                log.error(
                    "rollback could not stop an agent",
                    agent=agent.agent,
                    container_id=agent.handle.short_id,
                    image=agent.handle.image,
                    error_code=_code(err),
                )

    def _bind_log(self, record: DeploymentRecord) -> Any:
        return log.bind(deployment_id=record.deployment_id, graph=record.graph)


def _deployed(launched: LaunchedAgent, token: str, access_credential: str = "") -> DeployedAgent:
    return DeployedAgent(
        name=launched.agent,
        replica=launched.replica,
        container_id=launched.handle.container_id,
        image=launched.handle.image,
        control_port=launched.handle.control_port,
        token=token,
        a2a_port=launched.a2a_port,
        access_credential=access_credential,
    )


def _relaunched(agent: DeployedAgent, launched: LaunchedAgent) -> DeployedAgent:
    """컨테이너가 바뀐 자리 — 바뀌는 것은 컨테이너뿐이다.

    나머지는 **기록에서** 잇는다: 새로 만들면 기록에만 있는 값(토큰, 신원)이 조용히 빈 값이 되고,
    다음 재시작이 신원 없는 컨테이너를 세운다.
    """
    return replace(
        agent,
        container_id=launched.handle.container_id,
        image=launched.handle.image,
        control_port=launched.handle.control_port,
        a2a_port=launched.a2a_port,
    )


PROXY_TERMINATED_SECRETS = frozenset({"ANTHROPIC_API_KEY", "OPENAI_COMPAT_API_KEY"})
"""이그레스 프록시가 종단해 스스로 주입하는 자격 — 에이전트 env 로 나가지 않는다."""


def remote_mcp_credentials(manifest: AgentManifest) -> frozenset[str]:
    """원격 MCP 서버의 자격 이름 — 프록시가 붙이므로 에이전트 env 로 나가지 않는다 (02, #282)."""
    return frozenset(
        server.auth.token_env
        for server in manifest.spec.mcp.servers
        if server.url is not None and server.auth is not None
    )


@dataclass(frozen=True)
class EgressEndpoints:
    """The egress proxy as agent containers reach it."""

    connect_url: str
    providers_url: str

    def env_for(self, agent: str, credential: str) -> dict[str, str]:
        """이 에이전트의 외부 호출 배선 — 프록시 자격은 **그 에이전트의 신원**이다."""
        parts = urlsplit(self.connect_url)
        user = f"{quote(agent, safe='')}:{quote(credential, safe='')}"
        # netloc 을 그대로 쓴다 — hostname 은 IPv6 괄호를 벗겨 `@::1:8080` 같은 깨진 주소가 된다.
        # 설정 검증이 userinfo 를 막으므로 netloc 은 host[:port] 뿐이다
        proxy = f"{parts.scheme}://{user}@{parts.netloc}"
        return {
            # 대소문자 둘 다 — 도구마다 읽는 쪽이 다르다. http 는 프록시로 보내지 않는다: 프레임워크
            # 서비스(메모리·레지스트리·peer)는 사설 네트워크의 http 다
            "HTTPS_PROXY": proxy,
            "https_proxy": proxy,
            "ANTHROPIC_BASE_URL": f"{self.providers_url.rstrip('/')}/anthropic",
            # 원격 MCP 서버도 프록시가 종단한다 — 도구 단위 판정과 자격 주입 (#282)
            MCP_PROXY_URL_ENV: f"{self.providers_url.rstrip('/')}/mcp",
            # 키 자리에 신원을 싣는다 — 프록시가 떼고 진짜 키를 붙인다
            "ANTHROPIC_API_KEY": credential,
            # OpenAI 호환 provider 도 같은 방식이다 — 주소는 프록시 종단 창구, 키 자리는 신원
            "OPENAI_COMPAT_BASE_URL": f"{self.providers_url.rstrip('/')}/openai-compat",
            "OPENAI_COMPAT_API_KEY": credential,
        }


def _code(err: BaseException) -> str:
    return str(getattr(err, "code", type(err).__name__))


def _looks_secret(key: str) -> bool:
    upper = key.upper()
    return any(
        marker in upper
        for marker in (
            "SECRET",
            "TOKEN",
            "PASSWORD",
            "PASSWD",
            "API_KEY",
            "APIKEY",
            "CREDENTIAL",
            "PRIVATE_KEY",
        )
    )


__all__ = [
    "DeployedAgent",
    "DeploymentManager",
    "DeploymentRecord",
    "DeploymentStatus",
    "DeploymentStore",
    "InMemoryDeploymentStore",
    "SqliteDeploymentStore",
    "not_deployed",
]
