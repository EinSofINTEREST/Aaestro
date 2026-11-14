"""Agent startup sequence.

agentd 기동 시퀀스 (03 MCP Startup Sequence). 순서를 지키는 것 자체가 계약이다 —
필수 자원 하나라도 실패하면 Ready 로 전환하지 않는다 (부분 기동 금지).
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Final, Protocol, runtime_checkable

from aaestro.core.agent import ComponentHealth, HealthState, HealthStatus
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.markers import defang
from aaestro.core.skill import SkillSpec
from aaestro.core.tools import namespaced
from aaestro.memory.tool import MEMORY_SEARCH_SPEC, MEMORY_SEARCH_TOOL
from aaestro.protocols.a2a.tool import ASK_PEER_SPEC, ASK_PEER_TOOL

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from aaestro.agentd.executor import Model
    from aaestro.core.manifest import AgentManifest, McpServerSpec, ModelConfig
    from aaestro.modules.promptset import LoadedPromptset
    from aaestro.modules.skillset import LoadedSkillset

MCP_STARTUP_TIMEOUT_S = 15.0


@runtime_checkable
class McpLauncher(Protocol):
    """Starts one MCP server and reports its tools.

    MCP 서버 기동 계약. stdio/sidecar/external 3 패턴의 실제 구현은 #12 가 채운다.
    """

    async def start(self, spec: McpServerSpec, *, timeout_s: float) -> Sequence[str]:
        """서버를 기동하고 노출 tool 이름 목록을 반환한다."""
        ...

    def schemas(self) -> dict[str, dict[str, Any]]:
        """네임스페이스가 붙은 tool 이름 → input schema.

        이름만으로는 모델이 인자를 채울 수 없다 — 기동이 이것을 tool registry 로
        흘려보낸다.
        """
        ...

    def descriptions(self) -> dict[str, str]:
        """네임스페이스가 붙은 tool 이름 → 서버가 준 설명 (신뢰하지 않는 입력)."""
        ...


@dataclass
class BootstrapResult:
    """What a successful startup produced.

    기동 결과. Ready 로 전환할 수 있는 상태의 스냅샷이다.
    """

    promptset: LoadedPromptset | None = None
    skillsets: tuple[LoadedSkillset, ...] = ()
    tools: dict[str, SkillSpec] = field(default_factory=dict)
    """tool 이름 → 스키마. MCP tool 도 spec 이다 — 문자열을 담으면 provider 가
    to_tool_schema() 를 부르다 터진다."""
    mcp_servers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    degraded: tuple[str, ...] = ()
    """기동은 됐지만 optional 자원이 빠진 목록 — health 가 degraded 로 보고한다."""

    def health(self) -> HealthStatus:
        """Report component health after startup.

        기동 후 컴포넌트 상태를 종합합니다. optional 자원이 빠졌으면 degraded.
        """
        components: dict[str, ComponentHealth] = {}
        for name in self.mcp_servers:
            components[f"mcp:{name}"] = ComponentHealth(state=HealthState.HEALTHY)
        for name in self.degraded:
            components[f"mcp:{name}"] = ComponentHealth(
                state=HealthState.DEGRADED, detail="optional server unavailable"
            )
        if self.promptset is not None:
            components["modules"] = ComponentHealth(state=HealthState.HEALTHY)
        return HealthStatus.aggregate(components)


def _module_error(code: ErrorCode, message: str, agent: str, **details: Any) -> AaestroError:
    """모듈/기동 실패를 구조화 에러로 만든다."""
    return AaestroError(
        category=ErrorCategory.MCP if code.startswith("MCP") else ErrorCategory.MODULE,
        code=code,
        message=message,
        agent=agent,
        details=details,
    )


MCP_DESCRIPTION_MAX_CHARS: Final = 1024
"""서버 설명의 상한 — 서버가 프롬프트를 마음대로 부풀리지 못하게 한다."""

_KEEP_KEYS: Final = frozenset(
    {
        # 인자 값 계약
        "enum",
        "const",
        "default",
        "pattern",
        "format",
        # 이름 · 참조
        "required",
        "dependentRequired",
        "$ref",
        "$id",
        "$schema",
        "$anchor",
        "$dynamicRef",
        "$dynamicAnchor",
    }
)
"""스키마 위치에서 값을 통째로 보존하는 키 — 정리도 재귀도 하지 않는다."""
_SCHEMA_MAP_KEYS: Final = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)
"""이름 → 하위 스키마 맵 — 키 이름은 그대로, 값은 스키마로 재귀한다."""
_SUBSCHEMA_KEYS: Final = frozenset(
    {
        "items",
        "prefixItems",
        "additionalProperties",
        "unevaluatedProperties",
        "unevaluatedItems",
        "propertyNames",
        "contains",
        "not",
        "if",
        "then",
        "else",
        "allOf",
        "anyOf",
        "oneOf",
    }
)
"""하위 스키마 또는 그 목록 — 스키마로 재귀한다. 그 밖의 키는 데이터 위치다."""


def _clean_text(raw: str) -> str:
    """서버 문구를 모델에 싣기 전 정리한다 — 서버 문구는 신뢰하지 않는 입력이다.

    순서: 유니코드 범주 C* (폭 없는 문자 · bidi 제어 포함) 와 줄 · 문단 구분자를 공백으로
    (탭 · 개행은 둔다) → strip → 상한에서 자르기 → 표기 무력화. 무력화가 마지막이라 잘린 꼬리가
    표기가 되지 못한다. 상한은 무력화 전 길이 기준이라 결과는 무력화 접미사만큼 길 수 있다.
    """
    spaced = "".join(
        " "
        if (unicodedata.category(c).startswith("C") or unicodedata.category(c) in {"Zl", "Zp"})
        and c not in "\t\n"
        else c
        for c in raw
    )
    return defang(spaced.strip()[:MCP_DESCRIPTION_MAX_CHARS].strip())


def _clean_data(node: Any) -> Any:
    """데이터 위치 — 스키마로 해석하지 않고 안의 모든 문자열을 정리한다 (키 이름은 그대로)."""
    if isinstance(node, str):
        return _clean_text(node)
    if isinstance(node, dict):
        return {key: _clean_data(value) for key, value in node.items()}
    if isinstance(node, list):
        return [_clean_data(item) for item in node]
    return node


def clean_schema(node: Any) -> Any:
    """Copy a server input schema with every server-written string cleaned.

    스키마 위치와 데이터 위치를 구분해 정리한 사본을 돌려줍니다. 스키마 위치에서는 인자
    계약 · 이름 · 참조 키 (``_KEEP_KEYS``) 의 값을 통째로 보존하고, 하위 스키마를 담는 키
    (``properties`` 의 값들, ``items``, ``allOf`` …) 는 스키마로 재귀합니다. 그 밖의 모든 키
    (``description`` · ``title`` · ``examples`` · ``$comment`` · ``x-*`` · 모르는 키) 는 데이터
    위치라 안의 문자열을 키 이름과 무관하게 정리합니다 — 새 키의 기본값은 "정리" 입니다.
    딕셔너리 키 이름 (속성 이름) 은 인자 계약이라 그대로입니다.
    """
    if isinstance(node, list):
        return [clean_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key in _KEEP_KEYS:
            out[key] = value
        elif key in _SCHEMA_MAP_KEYS and isinstance(value, dict):
            out[key] = {name: clean_schema(sub) for name, sub in value.items()}
        elif key in _SUBSCHEMA_KEYS:
            out[key] = clean_schema(value)
        else:
            out[key] = _clean_data(value)
    return out


def mcp_tool_description(server: str, raw: str) -> str:
    """Turn a server-provided description into what the model sees.

    서버가 준 설명은 신뢰하지 않는 입력입니다 (03 MCP Rules 5): 제어문자를 공백으로 바꾸고,
    프레임워크 표기를 무력화하고, 상한에서 자르고, 출처를 붙입니다. 설명이 없으면
    기존 문구입니다.
    """
    text = _clean_text(raw)
    if not text:
        return f"MCP tool from {server}"
    return f"{text} (from MCP server {server})"


def build_tool_registry(
    skillsets: Sequence[LoadedSkillset],
    mcp_servers: dict[str, tuple[str, ...]],
    *,
    agent: str,
    with_memory: bool = False,
    with_peers: bool = False,
    mcp_schemas: Mapping[str, Mapping[str, Any]] | None = None,
    mcp_descriptions: Mapping[str, str] | None = None,
) -> dict[str, SkillSpec]:
    """Merge skillset and MCP tools into one registry.

    skillset tool 과 MCP tool 을 하나의 registry 로 합칩니다.

    Args:
        skillsets: Loaded skillsets contributing tools.
        mcp_servers: Server name to its exposed tool names.
        agent: Agent name for error context.
        with_peers: Register the framework ``ask_peer`` tool — A2A 가 켜진
            에이전트만 봅니다. 부를 수 없는 tool 을 광고하면 모델이 고를 때마다
            거부되어 루프에 빠집니다.
        with_memory: Register the framework ``memory_search`` tool — memory 가
            붙지 않았는데 노출하면 모델이 부를 수 없는 tool 을 본다.
        mcp_schemas: Namespaced tool name to its input schema — 이름만 담으면
            모델이 인자를 채울 수 없다.
        mcp_descriptions: Namespaced tool name to the server's own description —
            신뢰하지 않는 입력이라 ``mcp_tool_description`` 을 거쳐 실린다.

    Returns:
        Tool name to its spec — MCP tool 도 스키마를 담은 spec 이다.

    Raises:
        AaestroError: MODULE/``MOD_002`` if two sources claim the same tool name.
    """
    registry: dict[str, SkillSpec] = {}
    schemas = mcp_schemas or {}
    descriptions = mcp_descriptions or {}

    for skillset in skillsets:
        for spec in skillset.tools():
            if spec.name in registry:
                raise _module_error(
                    ErrorCode.MOD_002,
                    f"duplicate tool name across skillsets: {spec.name}",
                    agent,
                    tool=spec.name,
                    skillset=skillset.ref,
                )
            registry[spec.name] = spec

    for server, tools in mcp_servers.items():
        for tool in tools:
            name = namespaced(server, tool)
            if name in registry:
                raise _module_error(
                    ErrorCode.MOD_002,
                    f"duplicate tool name: {name}",
                    agent,
                    tool=name,
                    mcp_server=server,
                )
            registry[name] = SkillSpec(
                name=name,
                description=mcp_tool_description(server, descriptions.get(name, "")),
                parameters=clean_schema(dict(schemas.get(name, {}))),
            )

    if with_memory:
        if MEMORY_SEARCH_TOOL in registry:
            # 프레임워크 tool 이름을 스킬셋이 가져가면 둘 중 하나가 조용히 가려진다
            raise _module_error(
                ErrorCode.MOD_002,
                f"tool name is reserved by the framework: {MEMORY_SEARCH_TOOL}",
                agent,
                tool=MEMORY_SEARCH_TOOL,
            )
        registry[MEMORY_SEARCH_TOOL] = MEMORY_SEARCH_SPEC

    if with_peers:
        if ASK_PEER_TOOL in registry:
            raise _module_error(
                ErrorCode.MOD_002,
                f"tool name is reserved by the framework: {ASK_PEER_TOOL}",
                agent,
                tool=ASK_PEER_TOOL,
            )
        registry[ASK_PEER_TOOL] = ASK_PEER_SPEC

    return registry


def _anthropic_adapter(config: ModelConfig, agent: str, environ: Mapping[str, str]) -> Model:
    """Anthropic 어댑터 — base URL 과 키는 SDK 가 env 에서 직접 읽는다."""
    # 쓰는 provider 의 것만 올린다 — SDK import 가 기동 경로 전체에 얹히지 않게
    from aaestro.agentd.providers.anthropic import AnthropicModel

    return AnthropicModel(config=config, agent=agent)


def _openai_compat_adapter(config: ModelConfig, agent: str, environ: Mapping[str, str]) -> Model:
    """OpenAI 호환 어댑터 — base URL 과 Bearer 값은 설정(env)에서 온다."""
    from aaestro.agentd.providers.openai_compat import (
        API_KEY_ENV,
        BASE_URL_ENV,
        OpenAICompatModel,
    )

    base_url = environ.get(BASE_URL_ENV, "").strip()
    credential = environ.get(API_KEY_ENV, "").strip()
    missing = [
        name for name, value in ((BASE_URL_ENV, base_url), (API_KEY_ENV, credential)) if not value
    ]
    if missing:
        # 기본 주소로 떨어지면 선언하지 않은 provider 로 프롬프트가 나간다 — 값 없이 뜨지 않는다
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="openai-compat provider requires its base url and credential settings",
            agent=agent,
            details={"provider": config.provider, "settings": missing},
        )
    return OpenAICompatModel(config=config, agent=agent, base_url=base_url, credential=credential)


MODEL_ADAPTERS: Final[Mapping[str, Callable[[ModelConfig, str, Mapping[str, str]], Model]]] = (
    MappingProxyType(
        {
            "anthropic": _anthropic_adapter,
            "openai-compat": _openai_compat_adapter,
        }
    )
)
"""provider 이름 → 어댑터. 새 provider 는 여기 한 줄이다 — 분기문을 늘리지 않는다."""


def model_factory(agent: str, environ: Mapping[str, str]) -> Callable[[ModelConfig], Model]:
    """Build the factory that binds a model declaration to its provider adapter.

    모델 선언을 provider 어댑터에 묶는 팩토리를 만듭니다. 체인을 만드는 시점에 검사합니다:
    기동이면 기동 실패, 태스크가 실어 온 선언이면 그 태스크 실패.

    Args:
        agent: Agent name for error context.
        environ: Process settings the adapters read their endpoint from.

    Returns:
        A factory from a resolved model declaration to its adapter.

    Raises:
        AaestroError: (from the factory) CONFIG/``CFG_001`` if no adapter is bound for the
            provider, or the adapter's settings are missing — 모르는 provider 가 조용히 다른
            provider 로 떨어지지 않습니다.
    """

    def build(config: ModelConfig) -> Model:
        adapter = MODEL_ADAPTERS.get(config.provider)
        if adapter is None:
            from aaestro.agentd.models import unsupported_provider

            raise unsupported_provider(agent, config.provider)
        return adapter(config, agent, environ)

    return build


class Bootstrap:
    """Runs the startup sequence for one agent.

    에이전트 하나의 기동 시퀀스를 수행한다.
    """

    def __init__(
        self,
        manifest: AgentManifest,
        *,
        promptset_loader: Any,
        skillset_loader: Any,
        mcp_launcher: McpLauncher | None = None,
        mcp_timeout_s: float = MCP_STARTUP_TIMEOUT_S,
    ) -> None:
        self._manifest = manifest
        self._promptsets = promptset_loader
        self._skillsets = skillset_loader
        self._mcp = mcp_launcher
        self._mcp_timeout_s = mcp_timeout_s

    async def run(self) -> BootstrapResult:
        """Execute the startup sequence in order.

        기동 시퀀스를 순서대로 수행합니다 (03 MCP Startup Sequence):
        manifest 검증 → 모듈 로드 → MCP 기동 → tool registry → Ready.

        **부분 기동 금지**: 필수 MCP 서버가 하나라도 실패하면 예외를 던져
        Ready 로 전환하지 않습니다. ``optional: true`` 서버만 실패해도 계속합니다.

        Returns:
            The startup result, ready to serve.

        Raises:
            AaestroError: MCP/``MCP_001`` if a required server fails to start,
                MODULE/``MOD_002`` on a tool namespace collision.
        """
        agent = self._manifest.name

        # 2. promptset / skillset 로드 — 실패는 로더가 MOD_* 로 보고한다
        promptset = self._promptsets.load(self._manifest.spec.promptset.ref)
        skillsets = tuple(self._skillsets.load(ref.ref) for ref in self._manifest.spec.skillsets)

        # 3. MCP 서버 기동
        mcp_servers, degraded = await self._start_mcp_servers(agent)

        # 4. tool registry 구성 — 네임스페이스 충돌 검사
        tools = build_tool_registry(
            skillsets,
            mcp_servers,
            agent=agent,
            # memory space 를 선언한 에이전트만 memory_search 를 본다
            with_memory=bool(self._manifest.spec.memory.spaces),
            # A2A 를 켠 에이전트만 위임 창구를 본다 (03 Rule 1)
            with_peers=self._manifest.spec.a2a.enabled,
            # 스키마를 흘리지 않으면 모델이 MCP tool 의 인자를 채울 수 없다
            mcp_schemas=self._mcp.schemas() if self._mcp is not None else None,
            mcp_descriptions=self._mcp.descriptions() if self._mcp is not None else None,
        )

        return BootstrapResult(
            promptset=promptset,
            skillsets=skillsets,
            tools=tools,
            mcp_servers=mcp_servers,
            degraded=degraded,
        )

    async def _start_mcp_servers(
        self, agent: str
    ) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
        """선언된 MCP 서버를 기동한다 — 필수 실패는 기동 자체를 중단시킨다."""
        started: dict[str, tuple[str, ...]] = {}
        degraded: list[str] = []

        if self._mcp is None:
            return started, tuple(degraded)

        for spec in self._manifest.spec.mcp.servers:
            try:
                tools = await self._mcp.start(spec, timeout_s=self._mcp_timeout_s)
            except Exception as err:
                if not spec.optional:
                    # 부분 기동 금지 — 필수 서버가 빠진 채 Ready 가 되면
                    # 모델이 없는 tool 을 부르다 런타임에 실패한다
                    raise _module_error(
                        ErrorCode.MCP_001,
                        f"required mcp server failed to start: {spec.name}",
                        agent,
                        mcp_server=spec.name,
                    ) from err
                degraded.append(spec.name)
                continue
            started[spec.name] = tuple(self._allowed(spec, tools))

        return started, tuple(degraded)

    @staticmethod
    def _allowed(spec: McpServerSpec, tools: Sequence[str]) -> list[str]:
        """``allowed_tools`` 가 선언되면 그 목록만 바인딩한다."""
        if not spec.allowed_tools:
            return list(tools)
        allowed = set(spec.allowed_tools)
        return [tool for tool in tools if tool in allowed]
