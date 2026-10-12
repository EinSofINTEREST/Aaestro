"""Framework configuration.

환경별 설정 로딩과 스키마 검증. 잘못된 설정으로는 기동하지 않는다 —
런타임 중에 발견되는 설정 오류는 이미 늦다 (기동 시 FATAL).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

ENV_PREFIX = "AAESTRO_"
"""환경변수 오버라이드 접두사 — 중첩 키는 ``__`` 로 구분한다."""

NESTED_SEPARATOR = "__"
DEFAULT_CONFIG_DIR = "configs"
DEFAULT_ENVIRONMENT = "dev"
MEMORY_DB = ":memory:"
"""sqlite 의 in-memory 지시자 — 연결마다 별개라 프로세스 간에 공유되지 않는다."""
ENVIRONMENT_ENV = "AAESTRO_ENV"


def resolve_environment(
    explicit: str | None = None, *, environ: Mapping[str, str] | None = None
) -> str:
    """Pick the configuration environment: explicit, then env var, then default.

    설정 환경 이름을 정합니다 — 명시값 > ``AAESTRO_ENV`` > 기본값.
    이름을 한 곳에서 정해야 CLI 와 상주 프로세스가 같은 설정을 본다.
    """
    source = os.environ if environ is None else environ
    return explicit or source.get(ENVIRONMENT_ENV) or DEFAULT_ENVIRONMENT


def config_error(message: str, **details: Any) -> AaestroError:
    """설정 실패를 구조화 에러로 만든다 — 기동을 막는 FATAL 경로다."""
    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message=message,
        details=details,
    )


class ResourceDefaults(BaseModel):
    """Default container resources."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu: str = "1.0"
    memory: str = "1Gi"


class HealthCheckConfig(BaseModel):
    """Agent health polling."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    interval_s: float = Field(default=10.0, gt=0)
    timeout_s: float = Field(default=3.0, gt=0)
    unhealthy_threshold: int = Field(default=3, gt=0)
    startup_grace_s: float = Field(default=45.0, ge=0)
    """기동 유예 — 처음 Ready 가 되기 전까지 이 시간 안의 실패는 재시작을 일으키지 않는다 (#297).

    기동이 `interval_s × unhealthy_threshold` 보다 긴 에이전트(MCP 서버 여럿, 원격 initialize)는
    유예가 없으면 뜨는 중에 재시작돼 배포가 끝내 실패한다. `0` 은 유예 없음(이전 동작)이다.
    `orchestrator.deployment_ready_timeout_s` 보다 길게 두면 배포가 먼저 끝나므로 의미가 없다."""

    @model_validator(mode="after")
    def _timeout_within_interval(self) -> HealthCheckConfig:
        """타임아웃이 주기보다 길면 확인이 밀려 상태 판정이 늦어진다."""
        if self.timeout_s > self.interval_s:
            raise ValueError("health check timeout_s must not exceed interval_s")
        return self


class EgressProxyConfig(BaseModel):
    """Where agents reach the egress proxy (#293) — both listeners of one proxy.

    에이전트 컨테이너에서 닿는 주소다. 설정하면 배포가 에이전트의 외부 HTTPS 를 이 프록시로 보내고
    (``HTTPS_PROXY``), 모델 API 를 프록시로 부르게 하며(``ANTHROPIC_BASE_URL``), 모델 API 키를
    에이전트 env 에 넣지 않는다. 판정에 에이전트 신원이 필요하므로 권한 레지스트리를 함께 켠다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_url: str = Field(min_length=1)
    """CONNECT 창구 — ``http://egress:8080``."""
    providers_url: str = Field(min_length=1)
    """provider 종단 창구 — ``http://egress:8081``."""
    container: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    """프록시 컨테이너 이름 — MCP 사이드카를 쓰려면 필요하다 (#304). runtime 이 에이전트마다의
    사이드카 네트워크에 이 컨테이너를 붙여, 사이드카에 닿는 것이 프록시뿐이게 한다."""
    providers: tuple[str, ...] = ("anthropic",)
    """프록시가 종단하는 provider 이름. 배포 검증이 fallback provider 를 이 목록과 대조한다 —
    프록시 실제 upstream 과 맞춰 선언한다. 어긋나면 fallback 이 런타임에 프록시에서 거절된다."""

    @field_validator("providers")
    @classmethod
    def _non_empty_unique_providers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        # 빈 목록이면 어떤 모델도 부를 수 없다 — 프록시를 켠 의미가 없는 오설정이다
        if not value:
            raise ValueError("runtime.egress_proxy.providers must not be empty")
        if len(set(value)) != len(value):
            raise ValueError("runtime.egress_proxy.providers has duplicates")
        return value

    @model_validator(mode="after")
    def _plain_urls(self) -> EgressProxyConfig:
        for name, value in (
            ("connect_url", self.connect_url),
            ("providers_url", self.providers_url),
        ):
            parts = urlsplit(value)
            try:
                port = parts.port
            except ValueError:
                port = None
            # 포트가 없으면 80 으로 가서 8080·8081 창구에 닿지 않는다
            if (
                parts.scheme != "http"
                or not parts.hostname
                or not port
                or parts.username
                or parts.password
                or parts.path not in ("", "/")
                or parts.query
                or parts.fragment
            ):
                # 자격은 배포가 에이전트마다 붙인다 — 설정에 박힌 자격은 모든 에이전트가 나눠 갖는다
                raise ValueError(f"runtime.egress_proxy.{name} must be http://host:port")
        return self


class RuntimeConfig(BaseModel):
    """Agent runtime settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["docker"] = "docker"
    network: str = "aaestro-net"
    agent_base_image: str = "aaestro/agent-base:0.1.0"
    default_resources: ResourceDefaults = Field(default_factory=ResourceDefaults)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    egress_proxy: EgressProxyConfig | None = None
    """이그레스 프록시 (#293). 없으면 에이전트는 기존대로 직접 나가고 모델 키를 env 로 받는다."""
    agent_env: dict[str, str] = Field(default_factory=dict)
    """모든 에이전트 컨테이너에 주입하는 **비밀이 아닌** 인프라 env — provider base URL
    처럼 "어디에 있는가" 를 알리는 값. secrets 는 여기가 아니라 env_allowlist 로 간다
    (02 Secrets Injection); 이름이 secret 패턴이면 기동을 거부한다 (#243)."""

    @model_validator(mode="after")
    def _reject_host_network(self) -> RuntimeConfig:
        """호스트 네트워크 모드는 격리를 무력화한다 (02 Network)."""
        if self.network == "host":
            raise ValueError("agent containers must not use the 'host' network")
        return self


class ServiceDefaults(BaseModel):
    """Service run idle policy."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    idle_min_delay_s: float = Field(default=30.0, gt=0)
    idle_max_delay_s: float = Field(default=600.0, gt=0)
    max_failure_streak: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def _min_below_max(self) -> ServiceDefaults:
        """min 이 max 보다 크면 backoff 가 진행되지 않는다."""
        if self.idle_min_delay_s > self.idle_max_delay_s:
            raise ValueError("idle_min_delay_s must not exceed idle_max_delay_s")
        return self


class OrchestratorConfig(BaseModel):
    """Graph orchestration settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpointer: Literal["memory", "redis", "postgres"] = "memory"
    checkpointer_url: str | None = None
    """외부 checkpointer 의 접속 URL. 자격증명을 파일에 굽지 않도록
    ``AAESTRO_ORCHESTRATOR__CHECKPOINTER_URL`` 로 덮어쓰는 것이 기본 경로다 —
    이 필드가 없어 `checkpointer: postgres` 선언이 도달 불가능했다 (#220)."""
    run_store: str | None = Field(default=None, min_length=1)
    """Run 기록 저장소 **파일 경로** (sqlite). **프로세스 밖에서 run 을 보고 조작하는
    경로**가 여기에 달려 있다 — 미설정이면 run 이 기록되지 않아 control plane 이
    빈 목록을 돌려주고, drain 요청도 전달되지 않는다 (#221).

    파일이 아닌 값을 거절한다: sqlite 의 ``""`` 와 ``":memory:"`` 는 오류가 아니라
    **연결마다 다른 private DB** 를 연다. 그러면 CLI 와 control plane 이 서로 다른
    저장소를 보게 되어, 이 설정이 있는데도 run 이 보이지 않는 상태가 조용히 재현된다 —
    미설정보다 나쁘다. 설정했다고 믿기 때문이다."""
    deployment_ready_timeout_s: float = Field(default=60.0, gt=0)
    """배포가 에이전트의 Ready 를 기다리는 상한 — 넘기면 `RT_002` 로 롤백한다.

    기동이 느린 에이전트를 받으려면 이 값과 `runtime.health_check.startup_grace_s` 를 함께 올린다 —
    유예만 올리면 배포가 먼저 끝나고, 대기만 올리면 뜨는 중에 재시작된다 (#297)."""
    control_host: str = "127.0.0.1"
    """Control Plane bind 주소. 기본은 loopback — 이 표면은 인증이 없으므로
    외부에 열려면 그 앞을 막는 것이 배포하는 쪽의 책임이다."""
    control_port: int = Field(default=8700, gt=0, le=65535)
    build_store: str | None = Field(default=None, min_length=1)
    """빌드 결과 저장소 경로 (sqlite). 배포가 "구워졌는가" 를 보는 근거다 (#265/#266) —
    프로세스를 넘겨 읽혀야 하므로 `run_store` 와 같은 파일 경로 규칙."""
    material_store: str | None = Field(default=None, min_length=1)
    """빌드 재료 저장소 경로 (sqlite). 커스텀 에이전트의 `src/`·`Dockerfile` 이 여기 산다 —
    없으면 재료 표면을 열지 않는다 (#264). `run_store` 와 같은 파일 경로 규칙."""
    deployment_store: str | None = Field(default=None, min_length=1)
    """배포 기록 저장소 경로 (sqlite). control plane 이 재시작한 뒤 살아 있는
    컨테이너를 다시 찾으려면 프로세스 밖에 기록이 있어야 한다 (#243).
    `run_store` 와 같은 규칙 — 파일 경로여야 한다."""
    control_token: str | None = Field(default=None, min_length=1)
    """Control Plane 의 Bearer 토큰. 이 표면은 파일을 쓰고 컨테이너를 띄우므로
    (#242~) 무인증으로 두지 않는다. loopback 밖으로 bind 하면서 토큰이 없으면
    진입점이 기동을 거부한다 — `AAESTRO_ORCHESTRATOR__CONTROL_TOKEN` 으로 준다."""
    access_store: str | None = Field(default=None, min_length=1)
    """권한 레지스트리 저장소 경로 (sqlite) — 에이전트 신원과 부여·회수 기록 (#277). 없으면 권한
    표면을 열지 않는다. 신원이 재시작을 넘어야 하므로 `run_store` 와 같은 파일 경로 규칙."""
    access_enforcer_token: str | None = Field(default=None, min_length=1)
    """강제 지점(Memory Service, 이그레스 프록시, A2A 서버)이 판정 라우트에 내미는 토큰. control
    plane 토큰과 **달라야** 한다 — 같으면 강제 지점이 운영자 권한을 덤으로 갖는다."""
    access_agent_url: str | None = Field(default=None, min_length=1)
    """에이전트 컨테이너에서 닿는 control plane 주소 — A2A 호출자가 표를 받고 피호출자가 확인하는 곳
    (#281). 레지스트리를 켜면 필수다: 없으면 에이전트가 공유 서명 키로 되돌아가야 하는데, 그 키는
    그래프의 모든 에이전트가 쥐어 경계가 되지 못한다."""
    access_stewards: tuple[str, ...] = ()
    """권한 에이전트로 지정된 에이전트 이름 — 운영자 설정이다. 에이전트가 스스로 지정할 수 없다."""
    state_artifact_root: str | None = Field(default=None, min_length=1)
    """그래프 state 압축 (`summarize`) 이 접은 원문을 두는 디렉토리 (#371). 오케스트레이터
    전용 저장소다 — 에이전트의 artifact 스코프 (local/group/global) 와 별개이고 에이전트는
    여기에 닿지 못한다. 없으면 `summarize` 를 선언한 그래프는 배포 검증과 run 시작에서
    거절된다. 자동 정리는 없다 — run 보존 기간에 맞춰 운영자가 비운다."""
    max_concurrent_runs: int = Field(default=10, gt=0)
    max_service_runs: int = Field(default=5, gt=0)
    node_timeout_s: float = Field(default=300.0, gt=0)
    service_defaults: ServiceDefaults = Field(default_factory=ServiceDefaults)

    @model_validator(mode="after")
    def _build_store_is_a_file(self) -> OrchestratorConfig:
        """run_store 와 같은 이유 — in-memory 는 연결마다 별개라 재시작을 넘기지 못한다."""
        if self.build_store is None:
            return self
        target = self.build_store.strip()
        if not target or target == MEMORY_DB or target.startswith(f"file:{MEMORY_DB}"):
            raise ValueError(
                "orchestrator.build_store must be a file path — "
                "an in-memory database is private to each connection"
            )
        return self

    @model_validator(mode="after")
    def _material_store_is_a_file(self) -> OrchestratorConfig:
        """run_store 와 같은 이유 — in-memory 는 연결마다 별개라 재시작을 넘기지 못한다."""
        if self.material_store is None:
            return self
        target = self.material_store.strip()
        if not target or target == MEMORY_DB or target.startswith(f"file:{MEMORY_DB}"):
            raise ValueError(
                "orchestrator.material_store must be a file path — "
                "an in-memory database is private to each connection"
            )
        return self

    @model_validator(mode="after")
    def _access_agent_url_is_a_plain_http_url(self) -> OrchestratorConfig:
        """에이전트가 신원을 싣고 부르는 주소 — 스킴·호스트가 있는 http(s) URL 만.

        자격을 URL 에 넣거나(userinfo) 경로·쿼리를 붙이면 로그와 프록시에 새거나 요청이 엉뚱한
        곳으로 간다. 평문 ``http`` 는 에이전트 전용 사설 네트워크 안에서만 쓴다 — 문서에 명시한다.
        """
        if self.access_agent_url is None:
            return self
        parts = urlsplit(self.access_agent_url)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.path not in ("", "/")
        ):
            raise ValueError(
                "orchestrator.access_agent_url must be an http(s) URL with a host and nothing else"
            )
        return self

    @model_validator(mode="after")
    def _access_store_is_a_file(self) -> OrchestratorConfig:
        """신원이 재시작을 넘어야 한다 — in-memory 는 연결마다 별개다."""
        if self.access_store is None:
            return self
        target = self.access_store.strip()
        if not target or target == MEMORY_DB or target.startswith(f"file:{MEMORY_DB}"):
            raise ValueError(
                "orchestrator.access_store must be a file path — "
                "an in-memory database is private to each connection"
            )
        return self

    @model_validator(mode="after")
    def _enforcer_token_is_not_the_control_token(self) -> OrchestratorConfig:
        """강제 지점의 토큰으로 운영자 라우트를 부를 수 있으면 두 토큰을 나눈 의미가 없다."""
        if self.access_enforcer_token is not None and (
            self.access_enforcer_token == self.control_token
        ):
            raise ValueError(
                "orchestrator.access_enforcer_token must differ from orchestrator.control_token"
            )
        return self

    @model_validator(mode="after")
    def _deployment_store_is_a_file(self) -> OrchestratorConfig:
        """run_store 와 같은 이유 — in-memory 는 연결마다 별개라 재시작을 넘기지 못한다."""
        if self.deployment_store is None:
            return self
        target = self.deployment_store.strip()
        if not target or target == MEMORY_DB or target.startswith(f"file:{MEMORY_DB}"):
            raise ValueError(
                "orchestrator.deployment_store must be a file path — "
                "an in-memory database is private to each connection"
            )
        return self

    @model_validator(mode="after")
    def _run_store_is_a_file(self) -> OrchestratorConfig:
        """프로세스 간에 공유되지 않는 저장소를 거절한다.

        값을 하나씩 막는 대신 **공유되는가**를 본다: sqlite 에서 파일 경로가
        아닌 것(``:memory:`` 및 ``file::memory:`` 계열)은 연결마다 별개의 DB 다.

        공백을 벗겨 비교하는 것은 별개 이유다 — `` :memory: `` 는 sqlite 에겐
        그 이름의 **파일**이라 공유는 되지만, 저장소 경로로 그런 이름을 의도했을
        가능성은 없다. 오타를 그대로 받아 이상한 파일을 만드는 대신 거절한다.
        """
        if self.run_store is None:
            return self
        target = self.run_store.strip()
        shared = target and target != MEMORY_DB and not target.startswith(f"file:{MEMORY_DB}")
        if not shared:
            raise ValueError(
                "orchestrator.run_store must be a file path — "
                "an in-memory database is private to each connection"
            )
        return self


class A2AConfig(BaseModel):
    """A2A port allocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port_range: tuple[int, int] = (9100, 9199)

    @model_validator(mode="after")
    def _valid_range(self) -> A2AConfig:
        """범위가 뒤집히면 포트를 하나도 할당할 수 없다."""
        low, high = self.port_range
        if low >= high:
            raise ValueError("a2a port_range must be ascending")
        if low < 1024:
            raise ValueError("a2a port_range must not use privileged ports")
        return self


class McpConfig(BaseModel):
    """MCP startup budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    startup_timeout_s: float = Field(default=15.0, gt=0)


class ProtocolsConfig(BaseModel):
    """Protocol layer settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    a2a: A2AConfig = Field(default_factory=A2AConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)


class RegistryRoots(BaseModel):
    """Module resolution roots — every ref type must be covered."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skillsets: str = "./modules/skillsets"
    promptsets: str = "./modules/promptsets"
    memorysets: str = "./modules/memorysets"
    agents: str = "./agents"
    graphs: str = "./graphs"
    groups: str = "./groups"


class RegistryConfig(BaseModel):
    """Module registry settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["filesystem"] = "filesystem"
    roots: RegistryRoots = Field(default_factory=RegistryRoots)


class MemoryConfig(BaseModel):
    """Memory service settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    backend: Literal["sqlite", "postgres"] = "sqlite"
    index_lag_target_s: float = Field(default=5.0, gt=0)
    run_scope_retention_days: int = Field(default=30, gt=0)
    # sqlite 는 파일 경로, postgres 는 DSN. DSN 은 자격증명을 담으므로 설정
    # 파일이 아니라 AAESTRO_MEMORY__DSN 으로 주입한다 (01 Configuration Strategy)
    path: str = ":memory:"
    dsn: str = ""


class ObservabilityConfig(BaseModel):
    """Logging and metrics settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["pretty", "json"] = "json"
    metrics_port: int = Field(default=9090, gt=1023, lt=65536)


class AaestroConfig(BaseModel):
    """The complete framework configuration.

    프레임워크 설정 전체. 모든 섹션이 기본값을 가지므로 빈 파일도 유효하다 —
    운영자가 바꾸고 싶은 것만 적는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    orchestrator: OrchestratorConfig = Field(default_factory=OrchestratorConfig)
    protocols: ProtocolsConfig = Field(default_factory=ProtocolsConfig)
    registry: RegistryConfig = Field(default_factory=RegistryConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)


def _coerce(raw: str) -> Any:
    """환경변수 문자열을 YAML 스칼라로 해석한다 — 숫자/불리언이 문자열로 남지 않게."""
    try:
        return yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Collect ``AAESTRO_`` overrides into a nested mapping.

    ``AAESTRO_`` 환경변수를 중첩 매핑으로 모읍니다.
    ``AAESTRO_RUNTIME__NETWORK=x`` → ``{"runtime": {"network": "x"}}``.

    **중첩 구분자가 있는 것만** 설정 override 로 봅니다. 컨테이너에 주입되는
    런타임 env (``AAESTRO_AGENT_TOKEN``, ``AAESTRO_A2A_PORT`` 등) 가 같은
    접두사를 쓰기 때문입니다 — 구분 없이 받으면 그 환경에서 설정 검증이
    통째로 실패합니다 (#182). 최상위 설정 키는 전부 섹션이므로 스칼라
    override 를 잃지 않습니다.

    Args:
        environ: Source environment (defaults to the process environment).

    Returns:
        The nested override mapping.
    """
    source = os.environ if environ is None else environ
    overrides: dict[str, Any] = {}

    # 깊은 키를 나중에 적용해 얕은 키가 자식을 덮어쓰지 못하게 한다.
    # 정렬하지 않으면 결과가 dict 순회 순서에 좌우돼 비결정적이 된다
    paths: list[tuple[list[str], str]] = []
    for key, value in source.items():
        if not key.startswith(ENV_PREFIX):
            continue
        suffix = key[len(ENV_PREFIX) :]
        if NESTED_SEPARATOR not in suffix:
            # 런타임 env — 설정 키가 아니다
            continue
        path = suffix.lower().split(NESTED_SEPARATOR)
        if not all(path):
            continue
        paths.append((path, value))

    for path, value in sorted(paths, key=lambda item: len(item[0])):
        cursor = overrides
        for part in path[:-1]:
            existing = cursor.get(part)
            if not isinstance(existing, dict):
                existing = {}
                cursor[part] = existing
            cursor = existing
        leaf = path[-1]
        # 이미 하위 키가 채워졌으면 스칼라로 덮어쓰지 않는다
        if isinstance(cursor.get(leaf), dict):
            continue
        cursor[leaf] = _coerce(value)

    return overrides


def merge(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge an overlay into a base mapping.

    오버레이를 기반 매핑에 깊게 병합합니다 — 섹션 하나를 덮어써도 나머지
    키가 사라지지 않습니다.
    """
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = merge(current, value)
        else:
            merged[key] = value
    return merged


def load_config(
    environment: str = DEFAULT_ENVIRONMENT,
    *,
    config_dir: str | Path = DEFAULT_CONFIG_DIR,
    environ: Mapping[str, str] | None = None,
) -> AaestroConfig:
    """Load and validate configuration for one environment.

    한 환경의 설정을 읽어 검증합니다. 파일 값 위에 ``AAESTRO_`` 환경변수가
    덮입니다 — 컨테이너 배포에서 파일을 다시 굽지 않고 조정할 수 있어야 합니다.

    Args:
        environment: Environment name (``dev`` / ``staging`` / ``prod``).
        config_dir: Directory holding ``{environment}.yaml``.
        environ: Source environment for overrides.

    Returns:
        The validated configuration.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the file is missing, unparseable,
            or fails schema validation — 잘못된 설정으로 기동하지 않습니다.
    """
    path = Path(config_dir) / f"{environment}.yaml"

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise config_error(
            "configuration file not found", path=str(path), environment=environment
        ) from err
    except (OSError, yaml.YAMLError) as err:
        raise config_error("configuration file could not be parsed", path=str(path)) from err

    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise config_error("configuration root must be a mapping", path=str(path))

    merged = merge(raw, env_overrides(environ))

    try:
        return AaestroConfig.model_validate(merged)
    except ValidationError as err:
        raise config_error(
            "configuration failed validation",
            path=str(path),
            errors=[
                {"field": ".".join(str(p) for p in e["loc"]), "problem": e["msg"]}
                for e in err.errors()
            ],
        ) from err


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "DEFAULT_ENVIRONMENT",
    "ENVIRONMENT_ENV",
    "ENV_PREFIX",
    "NESTED_SEPARATOR",
    "A2AConfig",
    "HealthCheckConfig",
    "AaestroConfig",
    "McpConfig",
    "MemoryConfig",
    "ObservabilityConfig",
    "OrchestratorConfig",
    "ProtocolsConfig",
    "RegistryConfig",
    "RegistryRoots",
    "ResourceDefaults",
    "RuntimeConfig",
    "ServiceDefaults",
    "config_error",
    "env_overrides",
    "load_config",
    "resolve_environment",
    "merge",
]
