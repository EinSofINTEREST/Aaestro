"""Agent manifest and group schemas.

에이전트 계약 선언(manifest)과 그룹(리소스 스코프 경계) 스키마.
Manifest 는 에이전트의 유일한 계약 소스이며, 코드에 하드코딩된
모델명/프롬프트/tool 목록은 금지된다.
"""

from __future__ import annotations

import posixpath
import re
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Any, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from aaestro.core.errors import repeated_positions

RESERVED_GLOBAL_GROUP = "global"
MANIFEST_ENV = "AAESTRO_MANIFEST"
"""agentd 가 매니페스트를 찾는 env — 마운트 자리를 가리키게 주입한다."""
DEFAULT_SIDECAR_PORT = 8000
DEFAULT_SIDECAR_PATH = "/mcp"
INVALID_PORT = -1
"""URL 의 포트를 읽을 수 없다는 표시 — 유효한 포트는 음수가 아니다."""


def split_url(value: str) -> SplitResult | None:
    """``urlsplit`` — 풀 수 없는 값은 ``None``.

    ``urlsplit`` 이 던지는 ``ValueError`` 의 메시지는 값의 조각 (netloc, 대괄호 안) 을 싣는다.
    validator 밖으로 그대로 올리면 선언 · 설정의 값이 에러로 나간다. ``None`` 을 돌려주어 호출부가
    ``except`` 블록 **밖에서** 고정 문구로 거절하게 한다 — 블록 안에서 던지면 원인이
    ``__context__`` 에 남고, pydantic 이 그 예외를 에러 ``ctx`` 에 담는다.
    """
    try:
        return urlsplit(value)
    except ValueError:
        return None


def _url_port(parts: SplitResult) -> int | None:
    """포트 — 없으면 ``None``, 숫자가 아니거나 범위 밖이면 :data:`INVALID_PORT` (원인은 버린다)."""
    try:
        return parts.port
    except ValueError:
        return INVALID_PORT


def _require_http_url(value: str, what: str) -> None:
    """스킴·호스트·유효한 포트가 있는 http(s) URL 인가 — 판정이 이 값을 풀다 터지지 않게."""
    parts = split_url(value)
    if parts is None:
        raise ValueError(f"{what} must be a valid http(s) URL")
    # 자격증명 검사가 맨 앞이다 — 스킴·포트가 틀린 URL 에서도 "자격증명을 싣지 말라" 가 먼저 나온다.
    # 메시지에는 값을 싣지 않는다 (field_problems 가 가리지만 원인 쪽에서도 막는다)
    if parts.username is not None or parts.password is not None:
        # 자격증명은 auth.token_env 로만 — URL 에 실리면 로그·카드·에러 곳곳에 샌다
        raise ValueError(f"{what} must not carry credentials; use auth.token_env")
    port = _url_port(parts)
    if port == INVALID_PORT:
        raise ValueError(f"{what} has an invalid port")
    if parts.scheme not in ("http", "https") or not parts.hostname or port == 0:
        raise ValueError(f"{what} must be an http(s) URL with a host")


_HOST_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
EGRESS_TARGET = re.compile(rf"^{_HOST_LABEL}(\.{_HOST_LABEL})*(:[0-9]{{1,5}})?$")
"""이그레스 목적지 — 소문자 호스트 이름과 선택 포트. 레지스트리 대상과 같은 모양이다."""

_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
NAME_PATTERN = _NAME_PATTERN
"""선언 이름 규칙 — 소문자·숫자·하이픈. 이름이 곧 파일 경로가 되므로 경로 구분자와 점 구간이
들어갈 수 없다는 것이 이 규칙의 보안상 의미다 (#273)."""
_SEMVER_PATTERN = re.compile(r"^\d+\.\d+\.\d+$")
_MODULE_REF_PATTERN = re.compile(
    r"^(?P<type>skillsets|promptsets|memorysets|skillpacks|agents|graphs)/"
    r"(?P<name>[a-z0-9]+(?:-[a-z0-9]+)*)@(?P<version>\d+\.\d+\.\d+)$"
)
_RESOURCE_CPU_PATTERN = re.compile(r"^\d+(?:\.\d+)?$")
_SHELL_BASENAMES = frozenset(
    {"sh", "bash", "zsh", "dash", "ash", "ksh", "fish", "csh", "tcsh", "env"}
)
_RESOURCE_MEMORY_PATTERN = re.compile(r"^\d+(?:Ki|Mi|Gi|Ti)$")

_BYTE_UNITS = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def parse_bytes(value: str) -> int:
    """``"50Gi"`` 같은 표기를 바이트로 — 검증을 통과한 값만 들어온다."""
    return int(value[:-2]) * _BYTE_UNITS[value[-2:]]


MEMORYSET_REF_PREFIX = "memorysets/"

ModuleRefStr = Annotated[str, Field(pattern=_MODULE_REF_PATTERN.pattern)]
"""모듈 참조 문자열 — ``{type}/{name}@{version}``. ``latest`` 등 비고정 참조 금지."""

AgentName = Annotated[str, Field(pattern=_NAME_PATTERN.pattern)]
SemVer = Annotated[str, Field(pattern=_SEMVER_PATTERN.pattern)]


def _require_pinned_image(value: str, subject: str) -> str:
    """Reject images that are not pinned to an explicit tag or digest.

    이미지가 태그 또는 digest 로 고정되었는지 검사합니다.

    태그 판정은 **마지막 경로 세그먼트**에서만 수행합니다 — ``registry.local:5000/x``
    처럼 레지스트리 포트가 있는 참조를 태그로 오인하면, 태그 없는 이미지가
    검증을 통과해 pull 시점에 ``latest`` 로 해석됩니다.

    Args:
        value: The image reference.
        subject: Subject name used in the error message (e.g. ``"agent"``).

    Returns:
        The validated image reference.

    Raises:
        ValueError: If the reference is not pinned.
    """
    if "@" in value:  # digest 고정 (예: image@sha256:...)
        return value

    last_segment = value.rsplit("/", 1)[-1]
    tag = last_segment.partition(":")[2]
    if not tag or tag == "latest":
        raise ValueError(
            f"{subject} image must be pinned to an explicit tag or digest (no 'latest')"
        )
    return value


class McpTransport(StrEnum):
    """MCP transport 종류.

    ``sse`` 는 두지 않는다 — MCP 명세가 legacy 로 내린 전송이고 연결 경로가 없다. 선언을
    받아 두면 매니페스트는 통과하고 initialize 에서야 실패한다 (#318).
    """

    STDIO = "stdio"
    STREAMABLE_HTTP = "streamable-http"


LEGACY_SSE_TRANSPORT = "sse"


class MemoryMode(StrEnum):
    """Memory space 접근 권한."""

    RW = "rw"
    RO = "ro"


class Metadata(BaseModel):
    """Agent manifest metadata.

    에이전트 식별 정보. ``group`` 은 최대 하나이며 예약 그룹 ``global`` 을
    직접 선언하는 것은 금지된다 (모든 에이전트는 암묵적 global 멤버).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: AgentName
    version: SemVer
    group: str | None = None
    description: str | None = None

    @field_validator("group")
    @classmethod
    def _reject_reserved_group(cls, value: str | None) -> str | None:
        """예약 그룹 직접 선언 차단 — 배포 검증에서 ``CFG_002`` 로 이어진다."""
        if value is None:
            return None
        if value == RESERVED_GLOBAL_GROUP:
            raise ValueError(
                "group 'global' is reserved — omit metadata.group instead of declaring it"
            )
        if not _NAME_PATTERN.match(value):
            raise ValueError("group must be lowercase alphanumeric with hyphens")
        return value


Effort = Literal["low", "medium", "high", "xhigh", "max"]
"""출력 품질/비용 조절 단계 — ``output_config.effort`` 로 실린다."""

FallbackTrigger = Literal["rate_limit", "server_error", "context_overflow"]
"""fallback 으로 넘어가는 실패 부류 — 05 의 에러 카테고리(LLM_001/LLM_003/LLM_002)에 대응한다."""


class FallbackModel(BaseModel):
    """A backup model tried when the primary fails in a declared way.

    주 모델이 ``on`` 에 적힌 부류로 실패할 때 시도하는 백업 모델.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str = Field(min_length=1)
    name: str = Field(min_length=1)
    on: tuple[FallbackTrigger, ...]

    @field_validator("on")
    @classmethod
    def _non_empty_unique_triggers(
        cls, value: tuple[FallbackTrigger, ...]
    ) -> tuple[FallbackTrigger, ...]:
        """빈 ``on`` 은 영원히 시도되지 않는 fallback 이다 — 선언 실수를 드러낸다."""
        if not value:
            raise ValueError("fallback 'on' must name at least one trigger")
        if len(set(value)) != len(value):
            raise ValueError("fallback 'on' has duplicate triggers")
        return value


class RetryDeclaration(BaseModel):
    """Backoff for one retryable failure class.

    재시도 가능한 실패 부류 하나의 backoff 선언.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_s: float = Field(gt=0)
    attempts: int = Field(ge=1)
    max_s: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _max_not_below_start(self) -> RetryDeclaration:
        """상한이 시작값보다 작으면 첫 대기부터 상한에 잘려 선언 의도와 어긋난다."""
        if self.max_s is not None and self.max_s < self.start_s:
            raise ValueError("retry max_s must be >= start_s")
        return self


class ModelRetry(BaseModel):
    """Retry declarations per failure class.

    실패 부류별 재시도 선언. 생략한 부류는 프레임워크 기본 정책을 쓴다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rate_limit: RetryDeclaration | None = None
    network: RetryDeclaration | None = None


class CircuitDeclaration(BaseModel):
    """Circuit breaker thresholds for the model provider.

    모델 provider 에 대한 circuit breaker 임계값 (05 Circuit Breaker).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_failures: int = Field(ge=1)
    reset_s: float = Field(gt=0)


class ModelConfig(BaseModel):
    """Model provider configuration.

    모델 설정 — 코드가 아니라 manifest 가 모델을 결정한다. ``fallbacks`` /
    ``retry`` / ``circuit`` 는 선택이며, 생략하면 단일 모델 선언과 같다.
    """

    # 모르는 키는 거부한다 — `fallback:` 같은 오타가 조용히 무시되면 백업이 없는 채로 돈다
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    name: str
    max_tokens: int | None = Field(default=None, gt=0)
    effort: Effort | None = None
    """출력 품질/비용 조절.

    최신 모델은 sampling 파라미터(``temperature``/``top_p``/``top_k``)를
    받지 않는다 — 보내면 호출이 실패한다. 의도(품질과 비용의 균형)는
    ``output_config.effort`` 가 이어받는다.
    """
    fallbacks: tuple[FallbackModel, ...] = ()
    retry: ModelRetry | None = None
    circuit: CircuitDeclaration | None = None

    @model_validator(mode="after")
    def _distinct_models(self) -> ModelConfig:
        """주 모델과 같은 fallback, 같은 fallback 두 번은 같은 실패를 되풀이할 뿐이다."""
        # 어느 모델인지는 값이 아니라 **위치** 로 말한다 — 이름은 선언의 값이다 (#386)
        seen: dict[tuple[str, str], str] = {(self.provider, self.name): "the primary model"}
        for index, fallback in enumerate(self.fallbacks):
            key = (fallback.provider, fallback.name)
            if key in seen:
                raise ValueError(f"fallbacks[{index}] repeats {seen[key]}")
            seen[key] = f"fallbacks[{index}]"
        return self

    def providers(self) -> frozenset[str]:
        """Every provider this declaration may call — primary and fallbacks.

        이 선언이 부를 수 있는 provider 전부 (주 모델 + fallback).
        """
        return frozenset({self.provider, *(f.provider for f in self.fallbacks)})

    def overridden(self, *layers: ModelOverride | None) -> ModelConfig:
        """Apply override layers in order, nearest last.

        override 계층을 순서대로 적용합니다 (manifest 기본값 → 노드 → 배포).
        각 계층은 **명시한 키만** 덮으며, 명시한 null/빈 값은 아래 계층을 지웁니다.
        결과는 다시 검증되므로 fallback 중복 규칙이 override 뒤에도 지켜집니다.

        Raises:
            pydantic.ValidationError: If the resolved declaration is invalid.
        """
        data: dict[str, Any] = {name: getattr(self, name) for name in type(self).model_fields}
        for layer in layers:
            if layer is None:
                continue
            for key in layer.model_fields_set:
                data[key] = getattr(layer, key)
        if data["fallbacks"] is None:
            data["fallbacks"] = ()
        return type(self).model_validate(data)


class ModelOverride(BaseModel):
    """A partial model declaration from a node or a deployment.

    노드 · 배포 계층의 부분 선언. 키가 **있으면** 아래 계층을 덮는다 (null 포함).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str | None = None
    name: str | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    effort: Effort | None = None
    fallbacks: tuple[FallbackModel, ...] | None = None
    retry: ModelRetry | None = None
    circuit: CircuitDeclaration | None = None

    @model_validator(mode="after")
    def _provider_carries_name(self) -> ModelOverride:
        """모델 이름은 provider 에 속한다 — provider 만 바꾸면 남의 이름이 딸려 온다."""
        for key in ("provider", "name"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"model override {key} must not be null")
        if "provider" in self.model_fields_set and "name" not in self.model_fields_set:
            raise ValueError("model override that sets provider must also set name")
        return self

    @model_serializer(mode="wrap")
    def _dump_set_keys(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        """설정한 키만, null 이어도 내보낸다.

        키의 유무가 곧 의미다 — 미설정 키를 null 로 내보내면 "아래 계층 지우기" 가 되고,
        설정한 null 을 ``exclude_none`` 이 지우면 지우기 선언이 사라진다.
        """
        dumped = handler(self)
        return {
            key: dumped.get(key)
            for key in type(self).model_fields
            if key in self.model_fields_set and (key in dumped or getattr(self, key) is None)
        }

    def merged(self, other: ModelOverride) -> ModelOverride:
        """Layer ``other`` on top of this override.

        ``other`` 가 명시한 키가 이 override 를 덮습니다.
        """
        data = {key: getattr(self, key) for key in self.model_fields_set}
        data.update({key: getattr(other, key) for key in other.model_fields_set})
        return type(self).model_validate(data)


class ModuleRef(BaseModel):
    """A versioned module reference.

    버전 고정된 모듈 참조.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ref: ModuleRefStr


class MemorySpaceRef(BaseModel):
    """A memory space attachment.

    Memory space 부착 선언. ``as`` 별칭은 에이전트 관점의 논리 이름이며,
    충돌 시 해석 순서는 local > group > global 이다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    ref: ModuleRefStr
    alias: str = Field(alias="as")
    mode: MemoryMode = MemoryMode.RW
    writers: tuple[AgentName, ...] = ()

    @field_validator("ref")
    @classmethod
    def _memoryset_ref(cls, value: str) -> str:
        """space 는 memoryset 만 가리킨다 — 다른 타입은 부착 검사가 읽을 수 없는 ref 가 된다 (#467).

        받은 값은 메시지에 싣지 않는다 (필드와 사유만).
        """
        if not value.startswith(MEMORYSET_REF_PREFIX):
            raise ValueError(f"memory space ref must be a {MEMORYSET_REF_PREFIX}* reference")
        return value


class MemorySpec(BaseModel):
    """Declared memory spaces.

    선언된 memory space 목록 — 미선언 space 접근은 ``MEM_001`` 로 거부된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    spaces: tuple[MemorySpaceRef, ...] = ()

    @field_validator("spaces")
    @classmethod
    def _unique_aliases(cls, value: tuple[MemorySpaceRef, ...]) -> tuple[MemorySpaceRef, ...]:
        """같은 선언 위치 안에서 별칭 중복 금지."""
        repeated = repeated_positions([s.alias for s in value])
        if repeated:
            raise ValueError(f"duplicate memory space alias at index {repeated}")
        return value


class McpAuth(BaseModel):
    """External MCP server authentication.

    외부 MCP 서버 인증 — 토큰 값이 아니라 env 키만 선언한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    type: Literal["bearer"]
    token_env: str


class McpSidecar(BaseModel):
    """Sidecar container spec for an agent-owned MCP server.

    에이전트 전용 MCP 사이드카. 소유 에이전트와 1:1 이며 lifecycle 을 함께한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str
    resources: ResourceSpec | None = None
    port: int = Field(default=DEFAULT_SIDECAR_PORT, gt=0, lt=65536)
    """사이드카가 streamable HTTP 로 듣는 컨테이너 포트 — 게시하지 않는다."""
    path: str = DEFAULT_SIDECAR_PATH
    """MCP 엔드포인트 경로."""

    @field_validator("image")
    @classmethod
    def _reject_latest_tag(cls, value: str) -> str:
        """사이드카 이미지는 semver 태그 고정 — ``latest`` 금지."""
        return _require_pinned_image(value, "sidecar")

    @field_validator("path")
    @classmethod
    def _plain_path(cls, value: str) -> str:
        """경로만 — 주소의 다른 부분을 이 값으로 바꾸지 못한다."""
        if not value.startswith("/") or any(c in value for c in "?#@\\ ") or "//" in value:
            raise ValueError("sidecar path must be an absolute path without query or fragment")
        return value


def sidecar_host(agent: str, server: str) -> str:
    """The sidecar container's name — its address on the agent's sidecar network.

    runtime 이 이 이름으로 띄우고, agentd 와 이그레스 프록시가 이 이름으로 부른다 — 주소를 선언에
    적지 않는다 (03 MCP 패턴 2). 이름 규칙이 하이픈 연속을 막으므로 ``--`` 는 에이전트 이름과
    서버 이름을 헷갈리지 않게 가르고, 레플리카 컨테이너(``aaestro-{agent}-{n}``)와도 겹치지 않는다.
    """
    return f"aaestro-{agent}--mcp-{server}"


def sidecar_url(agent: str, server: McpServerSpec) -> str:
    """Where this agent's sidecar server answers — derived, never declared.

    Raises:
        ValueError: If the server is not a sidecar.
    """
    if server.sidecar is None:
        raise ValueError(f"mcp server is not a sidecar: {server.name}")
    host = sidecar_host(agent, server.name)
    return f"http://{host}:{server.sidecar.port}{server.sidecar.path}"


class McpServerSpec(BaseModel):
    """An MCP server owned by exactly one agent.

    MCP 서버 선언 — stdio / sidecar / external 3 패턴.
    프로토콜 자원은 정확히 하나의 에이전트에 속한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    transport: McpTransport
    command: tuple[str, ...] = ()
    sidecar: McpSidecar | None = None
    url: str | None = None
    auth: McpAuth | None = None
    allowed_tools: tuple[str, ...] = ()
    env_allowlist: tuple[str, ...] = ()
    optional: bool = False

    @field_validator("transport", mode="before")
    @classmethod
    def _no_legacy_sse(cls, value: Any) -> Any:
        """enum 오류("stdio 또는 streamable-http") 대신 무엇으로 바꿀지 알려 준다."""
        if value == LEGACY_SSE_TRANSPORT:
            raise ValueError(
                "mcp transport 'sse' is not supported — it is legacy in the MCP spec; "
                "use 'streamable-http'"
            )
        return value

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        """서버 이름은 tool 네임스페이스(``mcp__{server}__{tool}``)에 쓰인다."""
        if not _NAME_PATTERN.match(value):
            raise ValueError("mcp server name must be lowercase alphanumeric with hyphens")
        return value

    @model_validator(mode="after")
    def _check_transport_shape(self) -> McpServerSpec:
        """전송 방식별 필수 필드와 보안 규칙을 검증한다."""
        if self.transport is McpTransport.STDIO:
            if not self.command:
                raise ValueError("stdio transport requires 'command'")
            if self.sidecar is not None or self.url is not None:
                raise ValueError("stdio transport must not declare 'sidecar' or 'url'")
            # 셸 문자열 실행 금지 — 이미지에 설치된 실행 파일만 허용
            if PurePosixPath(self.command[0]).name in _SHELL_BASENAMES:
                raise ValueError("stdio command must be an installed executable, not a shell")
            return self

        # HTTP 계열 — sidecar 와 external 중 정확히 하나
        if self.command:
            raise ValueError("'command' is only valid for stdio transport")
        if (self.sidecar is None) == (self.url is None):
            raise ValueError("http transport requires exactly one of 'sidecar' or 'url'")
        if self.sidecar is not None and self.auth is not None:
            raise ValueError("sidecar servers must not declare 'auth' — url is runtime-injected")
        if self.url is not None:
            _require_http_url(self.url, "mcp server url")
        return self


class McpSpec(BaseModel):
    """Per-agent MCP server declarations.

    이 에이전트 전용 MCP 서버 목록 — 서버 이름 중복은 금지된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    servers: tuple[McpServerSpec, ...] = ()

    @field_validator("servers")
    @classmethod
    def _unique_names(cls, value: tuple[McpServerSpec, ...]) -> tuple[McpServerSpec, ...]:
        """동일 서버 이름 중복 선언 차단 — tool 네임스페이스 충돌 방지."""
        repeated = repeated_positions([s.name for s in value])
        if repeated:
            raise ValueError(f"duplicate mcp server name at index {repeated}")
        return value

    @field_validator("servers")
    @classmethod
    def _remote_credentials_stay_remote(
        cls, value: tuple[McpServerSpec, ...]
    ) -> tuple[McpServerSpec, ...]:
        """원격 서버 자격 이름을 stdio 서버에 넘기지 않는다 (#282).

        이그레스 프록시가 켜지면 원격 서버 자격은 프록시가 붙이고 에이전트 env 에서 빠진다 — 같은
        이름을 stdio 서버가 받으면 그 서버는 기동에 실패하고, 받는다 해도 프록시가 쥔 자격을
        컨테이너 안 프로세스에 다시 주는 셈이다.
        """
        remote = {s.auth.token_env for s in value if s.url is not None and s.auth is not None}
        for index, server in enumerate(value):
            if server.command and remote.intersection(server.env_allowlist):
                raise ValueError(
                    f"servers[{index}].env_allowlist names a remote server's credential — "
                    "remote mcp credentials must not reach stdio servers"
                )
        return value


class A2ACapabilities(BaseModel):
    """A2A capability flags advertised on the AgentCard."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    streaming: bool = False
    push_notifications: bool = False


class A2ASpec(BaseModel):
    """A2A exposure settings.

    A2A 노출 설정 — peer 호출을 받으려면 ``enabled: true`` 가 필요하다.
    포트는 runtime 이 할당하므로 선언하지 않는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    capabilities: A2ACapabilities = Field(default_factory=A2ACapabilities)


class ResourceSpec(BaseModel):
    """CPU/memory limits.

    리소스 상한 — 그룹 quota 합계 검증의 단위가 된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu: str = "1.0"
    memory: str = "1Gi"

    @field_validator("cpu")
    @classmethod
    def _valid_cpu(cls, value: str) -> str:
        """CPU 는 코어 수 문자열 (예: ``"1.0"``)."""
        if not _RESOURCE_CPU_PATTERN.match(value):
            raise ValueError("cpu must be a decimal core count string, e.g. '1.0'")
        return value

    @field_validator("memory")
    @classmethod
    def _valid_memory(cls, value: str) -> str:
        """Memory 는 Ki/Mi/Gi/Ti 접미사 (예: ``"1Gi"``)."""
        if not _RESOURCE_MEMORY_PATTERN.match(value):
            raise ValueError("memory must be an integer with Ki/Mi/Gi/Ti suffix, e.g. '1Gi'")
        return value

    @property
    def cpu_cores(self) -> float:
        """코어 수 — quota 합산에 사용."""
        return float(self.cpu)

    @property
    def memory_bytes(self) -> int:
        """바이트 단위 메모리 — quota 합산에 사용."""
        return parse_bytes(self.memory)


class ArtifactSpec(BaseModel):
    """Artifact storage declared for a scope.

    스코프에 선언된 artifact 저장 정책. ``dict[str, Any]`` 로 두면 오타가
    조용히 통과하고 quota 가 강제되지 않는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quota: str | None = None
    """저장 상한 (예: ``"50Gi"``). 미선언 시 상한 없음."""

    @field_validator("quota")
    @classmethod
    def _valid_quota(cls, value: str | None) -> str | None:
        """quota 는 Ki/Mi/Gi/Ti 접미사 — resources.memory 와 같은 표기."""
        if value is not None and not _RESOURCE_MEMORY_PATTERN.match(value):
            raise ValueError("quota must be an integer with Ki/Mi/Gi/Ti suffix, e.g. '50Gi'")
        return value

    @property
    def quota_bytes(self) -> int | None:
        """바이트 단위 상한 — 미선언 시 None."""
        return None if self.quota is None else parse_bytes(self.quota)


# Docker 이름 있는 볼륨 규칙. 실 daemon 에서 확인했다: 한 글자 이름은 "at least two alphanumeric
# characters" 로 거절되고 `ab`, `a.b_c` 는 만들어진다. 첫 글자가 영숫자라 "/" 로 시작하는 호스트
# 경로, ".", ".." 도 빠진다.
_VOLUME_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]+$")
# 실 daemon 에서 255자 이름은 만들어지고 256자는 "file name too long" 으로 실패한다
# (local 드라이버가 이름을 디렉토리 이름으로 쓴다).
MAX_VOLUME_NAME_LENGTH = 255


def agent_volume_name(agent: str, volume: str) -> str:
    """Docker volume name for a volume an agent declares.

    매니페스트 볼륨의 실제 Docker 이름 — 에이전트마다 갈라 같은 선언 이름이 볼륨을 공유하지 않게
    한다 (02 Volumes). 레플리카는 같은 볼륨을 쓴다. 생성 인자와 재부착 대조가 이 함수 하나를 거친다.
    """
    # 구분자 `.` 은 에이전트 이름에 올 수 없다 — `-` 로 이으면 a + b-work 와 a-b + work 가 겹친다
    return f"aaestro-{agent}.{volume}"


class VolumeSpec(BaseModel):
    """An explicitly declared volume mount.

    명시 선언된 볼륨 — 에이전트 간 공유는 금지된다 (사이드채널 차단).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    mount_path: str
    read_only: bool = False

    @field_validator("name")
    @classmethod
    def _require_named_volume(cls, value: str) -> str:
        """볼륨 출처는 Docker 이름 있는 볼륨만 — 호스트 경로 bind 는 부류째 금지."""
        if len(value) > MAX_VOLUME_NAME_LENGTH or not _VOLUME_NAME_PATTERN.fullmatch(value):
            raise ValueError("volume name must be a docker named volume, not a host path")
        return value

    @field_validator("mount_path")
    @classmethod
    def _reject_sensitive_paths(cls, value: str) -> str:
        """호스트 민감 경로 마운트 절대 금지 — 정규화한 경로로 판정한다.

        Docker 가 목적지를 정리(`//etc`, `/a/../etc` → `/etc`)하므로 문자열 비교만으로는 우회된다.
        정규화한 값을 저장해 런타임 마운트·재부착 대조(양쪽 정규화)와 같은 경로를 보게 한다.
        """
        # NUL·개행 같은 제어 문자는 Docker 가 그 앞에서 자르는 등 목적지가 문자열과 달라진다
        if not value.isprintable():
            raise ValueError("mount path must contain only printable characters")
        if not value.startswith("/"):
            raise ValueError("mount path must be absolute")
        depth = 0
        for part in value.split("/"):
            if part == "..":
                depth -= 1
                # 루트 위로 올라가는 경로는 normpath 가 조용히 `/` 로 깎으므로 먼저 거절
                if depth < 0:
                    raise ValueError("mount path must not climb above the root")
            elif part not in ("", "."):
                depth += 1
        clean = "/" + posixpath.normpath(value).lstrip("/")
        forbidden = ("/var/run/docker.sock", "/etc", "/root", "/proc", "/sys")
        if any(clean == p or clean.startswith(f"{p}/") for p in forbidden):
            raise ValueError("mounting sensitive host path is forbidden")
        return clean


class RuntimeSpec(BaseModel):
    """Container runtime settings.

    컨테이너 실행 설정. ``env_allowlist`` 의 각 키는 배포 검증에서
    local > group > global 스코프 체인으로 해석 가능해야 한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image: str | None = None
    resources: ResourceSpec = Field(default_factory=ResourceSpec)
    env_allowlist: tuple[str, ...] = ()
    volumes: tuple[VolumeSpec, ...] = ()
    replicas: int = 1
    max_concurrent_tasks: int = 4
    egress: tuple[str, ...] = ()
    """이 에이전트가 이그레스 프록시를 거쳐 닿는 외부 목적지 — ``host`` 또는 ``host:port`` (#293).

    선언이 기본 권한이다. 모델 provider 와 external MCP 서버의 호스트는 따로 적지 않아도 선언에서
    나온다. 와일드카드·스킴·경로는 받지 않는다 — 받으면 선언이 "어디든" 이 된다."""

    @field_validator("egress")
    @classmethod
    def _explicit_destinations(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for value in values:
            matched = EGRESS_TARGET.fullmatch(value)
            port = value.rpartition(":")[2] if ":" in value else ""
            # 범위 밖 포트는 어떤 CONNECT 와도 맞지 않는다 — 선언이 조용히 쓸모없어진다
            if matched is None or (port and not 0 < int(port) < 65536):
                raise ValueError("egress destination must be host or host:port without wildcards")
        return values

    @field_validator("image")
    @classmethod
    def _reject_latest_tag(cls, value: str | None) -> str | None:
        """이미지 태그는 semver — ``latest`` 배포 금지."""
        if value is None:
            return None
        return _require_pinned_image(value, "agent")


class AgentSpec(BaseModel):
    """The agent contract body.

    에이전트 계약 본문 — 선언되지 않은 모듈/서버/자원의 사용은 금지된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: ModelConfig
    promptset: ModuleRef
    skillsets: tuple[ModuleRef, ...] = ()
    skillpacks: tuple[ModuleRef, ...] = ()
    memory: MemorySpec = Field(default_factory=MemorySpec)
    mcp: McpSpec = Field(default_factory=McpSpec)
    a2a: A2ASpec = Field(default_factory=A2ASpec)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    entrypoint: str | None = None

    @field_validator("promptset")
    @classmethod
    def _promptset_type(cls, value: ModuleRef) -> ModuleRef:
        """promptset 필드는 promptsets 타입 ref 만 받는다."""
        if not value.ref.startswith("promptsets/"):
            raise ValueError("promptset ref must be of type 'promptsets'")
        return value

    @field_validator("skillsets")
    @classmethod
    def _skillset_types(cls, value: tuple[ModuleRef, ...]) -> tuple[ModuleRef, ...]:
        """skillsets 필드는 skillsets 타입 ref 만 받는다."""
        for item in value:
            if not item.ref.startswith("skillsets/"):
                raise ValueError("skillset ref must be of type 'skillsets'")
        return value

    @field_validator("skillpacks")
    @classmethod
    def _skillpack_types(cls, value: tuple[ModuleRef, ...]) -> tuple[ModuleRef, ...]:
        """skillpacks 필드는 skillpacks 타입 ref 만 받는다."""
        for item in value:
            if not item.ref.startswith("skillpacks/"):
                raise ValueError("skillpack ref must be of type 'skillpacks'")
        # 같은 팩을 두 번 (같은 버전이든 다른 버전이든) 싣지 않는다 — 같은 이름의 skill 이
        # 두 벌 생기고, 어느 쪽이 쓰이는지 선언에서 읽히지 않는다
        names = [item.ref.partition("/")[2].partition("@")[0] for item in value]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(f"duplicate skillpack: {sorted(duplicates)}")
        return value


class AgentManifest(BaseModel):
    """The single source of truth for an agent's contract.

    에이전트의 유일한 계약 소스. 배포 시 이 스키마로 검증되며,
    미검증 manifest 로는 컨테이너를 기동하지 않는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Agent"]
    metadata: Metadata
    spec: AgentSpec

    @model_validator(mode="after")
    def _check_mcp_env_declared(self) -> AgentManifest:
        """MCP 서버가 요구하는 env 키가 에이전트 allowlist 에 있는지 확인한다."""
        # 메시지는 서버를 **위치** 로 가리키고 키 이름은 싣지 않는다 (#386). env 키 자리에 비밀 값
        # 자체를 적는 실수 (``token_env: sk-…``) 가 드물지 않고, 그 값이 에러 · 로그로 나간다
        allowed = set(self.spec.runtime.env_allowlist)
        for index, server in enumerate(self.spec.mcp.servers):
            absent = [at for at, key in enumerate(server.env_allowlist) if key not in allowed]
            if absent:
                raise ValueError(
                    f"spec.mcp.servers[{index}].env_allowlist names keys absent from "
                    f"runtime.env_allowlist (at index {', '.join(map(str, absent))})"
                )
            if server.auth is not None and server.auth.token_env not in allowed:
                raise ValueError(
                    f"spec.mcp.servers[{index}].auth.token_env must be in runtime.env_allowlist"
                )
        return self

    @model_validator(mode="after")
    def _check_volume_names_fit(self) -> AgentManifest:
        """에이전트 이름을 붙인 볼륨 이름이 Docker 상한 안인지 — 기동 전 배포 검증에서 거절."""
        for index, volume in enumerate(self.spec.runtime.volumes):
            if len(agent_volume_name(self.name, volume.name)) > MAX_VOLUME_NAME_LENGTH:
                # 위치만 싣는다 — 이름 자체는 에러 · 로그로 내보내지 않는다
                raise ValueError(
                    f"spec.runtime.volumes[{index}].name exceeds {MAX_VOLUME_NAME_LENGTH} "
                    "characters once prefixed with the agent name"
                )
        return self

    @property
    def name(self) -> str:
        """에이전트 이름 — 그래프가 참조하는 id."""
        return self.metadata.name

    @property
    def group(self) -> str:
        """소속 그룹 — 미선언 시 예약 그룹 ``global``."""
        return self.metadata.group or RESERVED_GLOBAL_GROUP


class GroupQuotas(BaseModel):
    """Aggregate resource ceiling for group members.

    소속 에이전트 리소스 합계 상한 — 초과 시 기동 거부(``RT_006``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cpu: str | None = None
    memory: str | None = None
    max_agents: int | None = None

    @field_validator("cpu")
    @classmethod
    def _valid_cpu(cls, value: str | None) -> str | None:
        """quota 오타를 집계 시점이 아니라 배포 검증에서 잡는다."""
        if value is not None and not _RESOURCE_CPU_PATTERN.match(value):
            raise ValueError("cpu must be a decimal core count string, e.g. '8.0'")
        return value

    @field_validator("memory")
    @classmethod
    def _valid_memory(cls, value: str | None) -> str | None:
        """quota 오타를 집계 시점이 아니라 배포 검증에서 잡는다."""
        if value is not None and not _RESOURCE_MEMORY_PATTERN.match(value):
            raise ValueError("memory must be an integer with Ki/Mi/Gi/Ti suffix, e.g. '16Gi'")
        return value

    @property
    def cpu_cores(self) -> float | None:
        """상한 코어 수."""
        return float(self.cpu) if self.cpu is not None else None

    @property
    def memory_bytes(self) -> int | None:
        """상한 메모리 바이트."""
        if self.memory is None:
            return None
        return ResourceSpec(memory=self.memory).memory_bytes


class CeilingMemory(BaseModel):
    """One memory space the permission agent may grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    space: str = Field(pattern=r"^(local|group|global):[^:\s*]+:[^:\s*]+$")
    """레지스트리 대상 이름 — Memory Service 의 space id (``scope:owner:alias``).

    형식을 강제한다: 와일드카드나 빈 조각을 받으면 상한이 "어느 space 든" 이 된다. run scope 는
    run 이 끝나면 사라지므로 상한에 두지 않는다."""
    mode: MemoryMode = MemoryMode.RO


class AccessCeiling(BaseModel):
    """The most a permission agent may grant a group's members at run time.

    실행 중 권한 확장의 상한 (01 Access Control). 상한은 **레지스트리가 결정적으로** 강제한다 —
    확장 요청은 신뢰할 수 없는 입력이므로 한계를 권한 에이전트의 판단에 맡기지 않는다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_ttl_s: int = Field(gt=0, le=7 * 24 * 3600)
    """부여 만료의 상한 — 만료 없는 부여는 없다."""
    memory: tuple[CeilingMemory, ...] = ()
    egress: tuple[str, ...] = ()
    """목적지 ``host`` 또는 ``host:port``."""
    mcp_tool: tuple[str, ...] = ()
    """``server/tool`` — 원격 MCP 만."""
    a2a: tuple[str, ...] = ()
    """피호출자 에이전트 이름."""

    @field_validator("egress", "mcp_tool", "a2a")
    @classmethod
    def _no_blank_targets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        # 빈 문자열이나 와일드카드를 허용하면 상한이 "무엇이든" 이 된다
        for value in values:
            if not value.strip() or "*" in value:
                raise ValueError("ceiling targets must be explicit — no blanks or wildcards")
        return values


class GroupAccess(BaseModel):
    """Access-control declarations scoped to a group."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ceiling: AccessCeiling | None = None
    """없으면 이 그룹의 멤버에게 실행 중 확장은 없다."""


class GroupSpec(BaseModel):
    """Group-scoped resources.

    그룹 스코프 리소스 — 그룹은 리소스 경계일 뿐 우열이나 연결 권한이 아니다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    quotas: GroupQuotas = Field(default_factory=GroupQuotas)
    secrets: tuple[str, ...] = ()
    memory: MemorySpec = Field(default_factory=MemorySpec)
    artifacts: ArtifactSpec = Field(default_factory=ArtifactSpec)
    access: GroupAccess = Field(default_factory=GroupAccess)


class GroupManifest(BaseModel):
    """A group definition (``groups/<name>.yaml``).

    그룹 정의. 예약 그룹 ``global`` 은 전역 스코프 리소스 선언 전용이다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Group"]
    metadata: Metadata
    spec: GroupSpec

    @model_validator(mode="after")
    def _check_group_metadata(self) -> GroupManifest:
        """그룹 정의의 metadata 는 자신의 이름만 갖는다 (소속 선언 금지)."""
        if self.metadata.group is not None:
            raise ValueError("group definitions must not declare metadata.group")
        return self

    @property
    def name(self) -> str:
        """그룹 이름."""
        return self.metadata.name

    @property
    def is_global(self) -> bool:
        """예약 전역 그룹인지."""
        return self.metadata.name == RESERVED_GLOBAL_GROUP


def agent_ref_parts(ref: str) -> tuple[str, str]:
    """Split ``agents/{name}@{version}`` into name and version.

    에이전트 참조를 이름과 버전으로 나눕니다 — 그래프 노드가 가리키는 에이전트를 찾는 **유일한**
    규칙입니다. 같은 파싱이 일곱 곳에 복제돼 있었다 (#319). 형식을 강제하지 않습니다: 틀린 참조는
    배포 검증이 "없는 에이전트" 로 보고하도록 이름을 그대로 돌려줍니다.
    """
    _, _, remainder = ref.partition("/")
    name, _, version = remainder.partition("@")
    return name, version


def agent_name(ref: str) -> str:
    """``agents/{name}@{version}`` → ``name``."""
    return agent_ref_parts(ref)[0]


class ParsedModuleRef(BaseModel):
    """A parsed module reference.

    파싱된 모듈 참조 — registry 가 경로로 해석하는 입력.
    """

    model_config = ConfigDict(frozen=True)

    type: str
    name: str
    version: str

    @classmethod
    def parse(cls, ref: str) -> ParsedModuleRef:
        """Parse a ``{type}/{name}@{version}`` reference.

        모듈 참조 문자열을 파싱합니다. 형식 위반 시 ``ValueError`` —
        registry boundary 에서 ``MOD_001`` 로 변환됩니다.

        Args:
            ref: Module reference in ``{type}/{name}@{version}`` format.

        Returns:
            The parsed reference.

        Raises:
            ValueError: If the reference is malformed or not version-pinned.
        """
        match = _MODULE_REF_PATTERN.match(ref)
        if match is None:
            raise ValueError("invalid module ref")
        return cls(
            type=match.group("type"),
            name=match.group("name"),
            version=match.group("version"),
        )

    def __str__(self) -> str:
        """정규 표현 문자열로 되돌린다."""
        return f"{self.type}/{self.name}@{self.version}"
