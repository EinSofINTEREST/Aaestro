"""agentd entrypoint — the container's main process.

컨테이너의 메인 프로세스. manifest 를 읽어 기동 시퀀스를 돌리고 Control API 를
서빙한다. 이미지가 이 모듈을 실행하므로, 에이전트별 이미지는 manifest 만 바꿔
끼우면 된다.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import itertools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
import uvicorn
import yaml

from aaestro.agentd.a2a_server import build_peer_client
from aaestro.agentd.health import agent_health
from aaestro.agentd.mcp import build_mcp_client
from aaestro.agentd.server import AgentRuntime, create_app
from aaestro.agentd.telemetry import ExecutorTelemetry
from aaestro.agentd.tools import AgentToolRegistry
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.core.skill import SkillSpec
from aaestro.core.tools import is_mcp_tool
from aaestro.http_auth import resolve_required_token
from aaestro.memory.http import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
from aaestro.memory.tool import MEMORY_SEARCH_TOOL
from aaestro.observability.metrics import DEFAULT_METRICS_PORT, Metrics
from aaestro.protocols.a2a.card import build_card
from aaestro.protocols.a2a.tool import ASK_PEER_TOOL

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from fastapi import FastAPI

    from aaestro.core.agent import TaskRequest
    from aaestro.core.manifest import ModelConfig

CONTROL_PORT = 8080
"""Control port — 컨테이너 내부 고정 (02 Network). A2A 포트는 runtime 이 할당한다."""

DEFAULT_MANIFEST_PATH = "/app/manifest.yaml"
MANIFEST_ENV = "AAESTRO_MANIFEST"
TOKEN_ENV = "AAESTRO_AGENT_TOKEN"  # noqa: S105 — 값이 아니라 키 이름이다
EXECUTOR_ENV = "AAESTRO_EXECUTOR"
BIND_HOST_ENV = "AAESTRO_AGENT_BIND_HOST"
DEFAULT_BIND_HOST = "0.0.0.0"  # noqa: S104 — 컨테이너 안에서는 runtime 이 닿아야 한다

ECHO_EXECUTOR = "echo"

ARTIFACT_ROOT_ENV = "AAESTRO_ARTIFACT_ROOT"
"""Artifact 저장 루트 — runtime 이 주입한다. 미주입 시 저장소 없음."""

ARTIFACT_QUOTA_ENV = "AAESTRO_ARTIFACT_QUOTAS"
"""스코프별 바이트 상한 — ``local=1024,group=2048`` 형식. runtime 이 주입한다."""

GLOBAL_SCOPE = "global"
"""전역 artifact 스코프 이름 — 예약 그룹과 같은 이름을 쓴다."""

ROOT_ENV = "AAESTRO_ROOT"
DEFAULT_ROOT = "/app"
"""모듈 registry 루트 — 이미지가 modules/ 를 어디에 두는지."""

ANTHROPIC_PROVIDER = "anthropic"
"""이 이미지가 바인딩한 모델 provider — 다른 provider 선언은 CFG_001 로 거부한다."""

LOG_LEVEL_ENV = "AAESTRO_LOG_LEVEL"
LOG_FORMAT_ENV = "AAESTRO_LOG_FORMAT"
METRICS_PORT_ENV = "AAESTRO_METRICS_PORT"
"""관측 설정 — 컨테이너 안이라 env 로 받는다 (01 Multi-Environment Support)."""
"""테스트 이미지가 선택하는 대역 — base 이미지의 기본값이 되어서는 안 된다."""

log = structlog.get_logger(__name__)


def load_manifest(path: Path) -> AgentManifest:
    """Load and validate the agent manifest.

    manifest 를 읽어 검증합니다. 미검증 manifest 로는 기동하지 않습니다
    (02 Manifest Rules 1).

    Args:
        path: Path to ``manifest.yaml``.

    Returns:
        The validated manifest.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the file is missing or invalid.
    """
    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="cannot read agent manifest",
            details={"path": str(path)},
        ) from err

    try:
        return AgentManifest.model_validate(raw)
    except ValueError as err:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="agent manifest failed validation",
            details={"path": str(path)},
        ) from err


def build_app(
    manifest: AgentManifest,
    executor: Any,
    *,
    token: str | None = None,
    tools: Sequence[Any] = (),
    reload: Callable[[], Awaitable[dict[str, Any] | None]] | None = None,
) -> FastAPI:
    """Build the Control API app for a manifest.

    manifest 에 대한 Control API 앱을 만듭니다.

    Args:
        manifest: The validated agent manifest.
        executor: The task executor serving invoke/stream.
        token: Per-agent token required on authenticated endpoints.

    Returns:
        The FastAPI application.
    """
    runtime = AgentRuntime(
        agent=manifest.name,
        executor=executor,
        # card 는 manifest 와 **실제 로드된 tool** 로부터 생성한다 — 손으로 쓰면
        # skill 목록이 빠지고 (03 AgentCard 1: 수동 작성 금지), peer 는 이
        # 에이전트가 뭘 할 수 있는지 알 수 없다
        card=build_card(manifest, tools).model_dump(mode="json"),
        # 매 요청 실행기에서 읽는다 — 리로드가 바꾼 세션·optional 목록이 그대로 반영된다
        health=lambda: agent_health(executor),
        max_concurrent_tasks=manifest.spec.runtime.max_concurrent_tasks,
        reload=reload,
    )
    return create_app(runtime, token=token)


def load_entrypoint(manifest: AgentManifest) -> Any:
    """Load the custom executor a manifest declares.

    manifest 가 선언한 커스텀 실행기를 로드합니다 (02 Registering Agents 2).

    해석에 실패하면 **기동을 거부합니다** — 조용히 기본 실행기로 떨어지면
    운영자는 자기 코드가 도는 줄 알지만 실제로는 표준 루프가 돕니다.

    Args:
        manifest: The validated agent manifest.

    Returns:
        The custom executor instance.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the reference cannot be resolved or
            the loaded object does not satisfy the executor contract.
    """
    reference = manifest.spec.entrypoint or ""
    module_name, _, class_name = reference.partition(":")
    if not module_name or not class_name:
        raise _entrypoint_error(
            manifest, reference, reason="entrypoint must be in 'module:Class' format"
        )

    try:
        module = importlib.import_module(module_name)
        loaded = getattr(module, class_name)
    except (ImportError, AttributeError) as err:
        raise _entrypoint_error(manifest, reference, reason=type(err).__name__) from err

    try:
        instance = loaded(manifest) if _wants_manifest(loaded) else loaded()
    except Exception as err:
        # 생성자가 터지면 데몬이 구조화되지 않은 예외로 죽는다 — 운영자는
        # 설정 문제인지 코드 버그인지 구분할 단서를 잃는다
        raise _entrypoint_error(manifest, reference, reason=type(err).__name__) from err

    # 계약을 확인하지 않으면 첫 태스크에서야 AttributeError 로 터진다
    for required in ("execute", "stream"):
        if not callable(getattr(instance, required, None)):
            raise _entrypoint_error(manifest, reference, reason=f"executor is missing {required}()")

    log.info("custom executor loaded", agent=manifest.name, entrypoint=reference)
    return instance


def _wants_manifest(loaded: Any) -> bool:
    """생성자가 manifest 를 받는지 — 받지 않는 실행기도 허용한다."""
    try:
        signature = inspect.signature(loaded)
    except (TypeError, ValueError):  # pragma: no cover - 내장 타입 방어
        return False
    return len(signature.parameters) > 0


def _entrypoint_error(manifest: AgentManifest, reference: str, *, reason: str) -> AaestroError:
    """entrypoint 해석 실패 — 기동을 막는다."""
    return AaestroError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="agent entrypoint could not be loaded",
        agent=manifest.name,
        details={"entrypoint": reference, "reason": reason},
    )


async def build_executor(manifest: AgentManifest, *, metrics: Metrics | None = None) -> Any:
    """Select the executor this image should serve.

    이 이미지가 서빙할 실행기를 고릅니다. 기본은 manifest 기반 표준 실행기이고,
    ``AAESTRO_EXECUTOR=echo`` 를 선언한 **테스트 이미지만** echo 대역을 씁니다 —
    base 이미지가 echo 로 동작하면 모든 에이전트가 대역이 되어버립니다.

    Args:
        manifest: The validated agent manifest.

    Returns:
        The executor serving invoke/stream.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the declared executor is unknown.
    """
    # manifest 가 커스텀 실행기를 선언했으면 그것이 이 이미지의 실행기다.
    # truthiness 로 보면 `entrypoint: ""` 가 **미선언과 같아져** 조용히 표준
    # 실행기로 떨어진다 — 선언한 이상 해석 실패로 거부해야 한다
    if manifest.spec.entrypoint is not None:
        return load_entrypoint(manifest)

    declared = os.environ.get(EXECUTOR_ENV, "").strip().lower()

    if declared == ECHO_EXECUTOR:
        from aaestro.agentd.echo import EchoExecutor

        return EchoExecutor()

    if declared:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="unknown executor selection",
            agent=manifest.name,
            details={"executor": declared},
        )

    # 표준 경로: manifest 의 모듈/모델 선언을 따르는 실행기
    if manifest.spec.model.provider != ANTHROPIC_PROVIDER:
        # 조용히 대역으로 떨어지면 운영에서 가짜 응답이 나간다
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message="no model provider bound for this provider",
            agent=manifest.name,
            details={"provider": manifest.spec.model.provider},
        )

    from aaestro.modules.registry import ModuleRegistry

    registry = ModuleRegistry.under(Path(os.environ.get(ROOT_ENV, DEFAULT_ROOT)))
    # 선언한 MCP 서버의 세션 — 없으면 선언된 MCP 도구가 광고도 실행도 되지 않는다 (#282)
    mcp = build_mcp_client(manifest, metrics=metrics)
    try:
        return await _standard_executor(manifest, registry, mcp, metrics=metrics)
    except BaseException:
        # 조립이 어디서 실패하든 먼저 뜬 세션·자식 프로세스를 남기지 않는다 — 넘겨준 뒤에는
        # 실행기(binding.tools.mcp)를 받은 쪽이 정리한다
        if mcp is not None:
            await mcp.shutdown()
        raise


async def _standard_executor(
    manifest: AgentManifest, registry: Any, mcp: Any, *, metrics: Metrics | None
) -> Any:
    from aaestro.agentd.executor import Executor, ExecutorServices
    from aaestro.agentd.models import ModelChains

    binding = await load_modules(
        manifest,
        registry,
        memory=_memory_access(manifest.name),
        # 03 은 "실행 중 peer 에게 위임한다" 를 규정하는데, 이 조립이 없어
        # 에이전트는 **받을 수는 있고 걸 수는 없었다** (#193)
        peers=build_peer_client(manifest),
        mcp=mcp,
    )
    # 05 Retry Layering — 모델 호출의 재시도 주체는 agentd 다. 체인이 기본 정책
    # (MODEL_RETRY_POLICIES) 을 들고 온다 — 선언의 retry 가 있으면 그것을 쓴다
    chains = ModelChains(
        agent=manifest.name, factory=_anthropic_factory(manifest.name), metrics=metrics
    )
    executor = Executor(
        agent=manifest.name,
        model=chains.for_config(manifest.spec.model),
        tools=binding.tools,
        render=binding.render,
        tool_schemas=binding.tool_schemas,
        system=binding.system,
        services=ExecutorServices(
            telemetry=_telemetry_for(manifest, metrics),
            artifacts=_artifact_store(manifest),
            # 09 Context Assembly — 태스크 진입 시 1회 회상해 프롬프트에 붙인다.
            # 이것이 없으면 memoryset 의 recall 선언이 아무 일도 하지 않는다
            recall=_task_recall(manifest, registry, binding.tools.memory),
            output_keys=binding.output_keys,
            # 그래프가 해석한 태스크의 모델 선언(#358) — 없으면 노드·배포 override 가 무시된다
            chains=chains.for_config,
        ),
    )
    # 기동 묶음을 통째로 건다 — 생성자는 일부 필드만 받아, 뜨지 못한 optional 서버(degraded)가
    # 빠진 채 health 가 healthy 로 답한다. 리로드와 같은 경로다
    executor.rebind(binding)
    return executor


async def load_modules(
    manifest: AgentManifest,
    registry: Any,
    *,
    memory: Any,
    peers: Any,
    mcp: Any = None,
    generation: int = 0,
) -> Any:
    """Load the promptset and skillsets a manifest declares into one binding.

    기동과 리로드가 **같은 조립**을 탄다 — 두 벌이면 리로드만 다른 도구 목록을 만든다.
    메모리·peer·MCP 연결은 모듈이 아니라 배선이라 호출자가 넘긴다: 리로드가 그것을 새로
    만들면 토큰과 세션이 바뀐다.

    Raises:
        AaestroError: MODULE 계열 — 모듈을 해석하거나 로드하지 못함.
    """
    from aaestro.agentd.bootstrap import Bootstrap
    from aaestro.agentd.executor import ModuleBinding
    from aaestro.modules.promptset import PromptsetLoader
    from aaestro.modules.skillset import SkillsetLoader

    result = await Bootstrap(
        manifest,
        promptset_loader=PromptsetLoader(registry),
        skillset_loader=SkillsetLoader(registry, generation=generation),
        # 리로드도 같은 클라이언트를 넘긴다 — 떠 있는 세션은 다시 띄우지 않고 그 도구를 보고한다
        mcp_launcher=mcp,
    ).run()
    tools = AgentToolRegistry(
        agent=manifest.name, skillsets=result.skillsets, memory=memory, peers=peers, mcp=mcp
    )
    return ModuleBinding(
        tools=tools,
        render=lambda task: _render(result, task),
        tool_schemas=tuple(_executable_schemas(result, tools)),
        output_keys=lambda task: _template_output_keys(result, task),
        system=_system(result),
        degraded=result.degraded,
    )


def build_reload(manifest: AgentManifest, executor: Any) -> Callable[[], Awaitable[dict[str, Any]]]:
    """The reload hook for the standard executor (#274).

    02 Hot Reload 는 무중단 리로드를 약속하는데, 이 훅이 런타임에 넘어가지 않아 엔드포인트가
    아무것도 다시 읽지 않고 ``reloaded`` 로 답했다.

    새 묶음을 **먼저 끝까지 조립한 뒤** 갈아 끼운다. 조립이 실패하면 예외가 올라가고 실행기는
    이전 묶음 그대로다 — 반쯤 로드된 도구 목록으로 태스크를 받지 않는다.
    """
    from aaestro.modules.registry import ModuleRegistry

    generations = itertools.count(1)

    async def reload() -> dict[str, Any]:
        current = executor.binding.tools
        registry = ModuleRegistry.under(Path(os.environ.get(ROOT_ENV, DEFAULT_ROOT)))
        binding = await load_modules(
            manifest,
            registry,
            memory=current.memory,
            peers=current.peers,
            mcp=current.mcp,
            # 세대를 올려야 고친 스킬 코드가 다시 실행된다 — 같은 세대면 import 캐시가
            # 옛 함수를 준다
            generation=next(generations),
        )
        executor.rebind(binding)
        log.info("agent modules reloaded", agent=manifest.name, tools=len(binding.tool_schemas))
        card: dict[str, Any] = build_card(manifest, binding.tool_schemas).model_dump(mode="json")
        return card

    return reload


def _artifact_store(manifest: AgentManifest) -> Any:
    """이 에이전트가 닿을 수 있는 artifact 스코프들 — 경로 미주입 시 None.

    02 Output Discipline 의 참조 전달 경로다. 주입하지 않으면 skill 이
    ``ctx.artifacts is None`` 을 받아 대용량 산출물을 남길 곳이 없다.

    스코프는 **소속이 정한다** (01 Resource Scoping): local 은 늘, group 은
    소속이 있을 때만, global 은 항상. 쓰기는 local 로만 가므로 에이전트가
    group/global 을 임의로 오염시킬 수 없다.
    """
    root = os.environ.get(ARTIFACT_ROOT_ENV)
    if not root:
        return None

    from aaestro.artifacts import FilesystemArtifactStore
    from aaestro.artifacts.scope import ArtifactScope, ScopedArtifacts

    base = Path(root)
    stores = {
        ArtifactScope.LOCAL: FilesystemArtifactStore(root=base, scope=manifest.name),
        ArtifactScope.GLOBAL: FilesystemArtifactStore(root=base, scope=GLOBAL_SCOPE),
    }
    group = manifest.metadata.group
    if group:
        stores[ArtifactScope.GROUP] = FilesystemArtifactStore(root=base, scope=group)

    return ScopedArtifacts(stores=stores, quotas=_artifact_quotas())


def _artifact_quotas() -> dict[Any, int]:
    """runtime 이 주입한 스코프별 상한.

    컨테이너는 ``groups/*.yaml`` 을 볼 수 없다 — 그것을 읽는 쪽이 값만
    넘겨준다 (02 Secrets Injection 과 같은 방향).
    """
    from aaestro.artifacts.scope import ArtifactScope

    declared = os.environ.get(ARTIFACT_QUOTA_ENV, "").strip()
    if not declared:
        return {}

    quotas: dict[Any, int] = {}
    for entry in declared.split(","):
        scope, _, limit = entry.partition("=")
        if scope.strip() and limit.strip().isdigit():
            quotas[ArtifactScope(scope.strip())] = int(limit)
    return quotas


MEMORY_TOKEN_FILE_ENV = "AAESTRO_MEMORY_TOKEN_FILE"  # noqa: S105 — 키 이름이지 값이 아니다
"""토큰이 담긴 파일 — Memory Service 가 **기동하며 발급**하므로, 정적 env 로는
순서가 맞지 않는 배치에서 쓴다. 값 자체는 여전히 불투명 토큰이다.

`{agent: token}` JSON 이면 이 에이전트의 것을 고르고, 아니면 파일 내용을 그대로
토큰으로 본다.
"""


def _memory_token(agent: str) -> str | None:
    """이 에이전트의 메모리 토큰 — env 우선, 없으면 파일."""
    token = os.environ.get(MEMORY_TOKEN_ENV)
    if token:
        return token

    path = os.environ.get(MEMORY_TOKEN_FILE_ENV)
    if not path:
        return None
    try:
        raw = Path(path).read_text("utf-8").strip()
    except OSError as err:
        # 파일이 있어야 붙을 수 있는 배치인데 못 읽었다 — 조용히 넘기면
        # 에이전트가 메모리 없이 떠서 원인이 한참 뒤에 드러난다
        log.warning("memory token file unreadable", agent=agent, exc_info=err)
        return None

    try:
        issued = json.loads(raw)
    except ValueError:
        return raw or None
    return issued.get(agent) if isinstance(issued, dict) else None


def _recall_policy(manifest: AgentManifest, registry: Any) -> Any:
    """이 에이전트에게 적용되는 auto-recall 정책.

    space 를 여럿 선언하면 **첫 번째**의 정책을 쓴다 — 회상은 태스크당 1회이고
    (09 Rule 1), 정책이 여럿이면 어느 예산을 쓸지가 모호해진다.
    """
    spaces = manifest.spec.memory.spaces
    if not spaces:
        return None

    from aaestro.modules.memoryset import MemorysetLoader

    return MemorysetLoader(registry).load(spaces[0].ref).manifest.spec.recall


def _task_recall(manifest: AgentManifest, registry: Any, memory: Any) -> Any:
    """Build the once-per-task recall the executor calls.

    태스크마다 1회 회상하는 콜러블을 만듭니다 — 미선언이거나 메모리가 없으면
    None 이라 실행기가 회상 단계를 건너뜁니다.
    """
    policy = _recall_policy(manifest, registry)
    if memory is None or policy is None or not policy.auto:
        return None

    recall_for_task = getattr(memory, "recall_for_task", None)
    if recall_for_task is None:
        return None

    async def recall(task: TaskRequest) -> str:
        # 회상 질의는 태스크 입력이다 — 프롬프트 전체를 쓰면 템플릿 문구가
        # 검색을 지배한다
        return str(await recall_for_task(json.dumps(task.input, ensure_ascii=False), policy=policy))

    return recall


def _memory_access(agent: str) -> Any:
    """Build memory access from the injected endpoint.

    runtime 이 주소와 불투명 토큰을 주입했을 때만 만듭니다 — **DB 자격증명은
    컨테이너에 들어오지 않습니다** (09 Access Enforcement 1).
    """
    url = os.environ.get(MEMORY_URL_ENV)
    token = _memory_token(agent)
    if not url or not token:
        return None

    from aaestro.runtime.memory_http import HttpMemoryAccess

    return HttpMemoryAccess(base_url=url, token=token)


def _anthropic_factory(agent: str) -> Callable[[ModelConfig], Any]:
    """provider 이름으로 어댑터를 고르는 팩토리 — 지금 묶인 provider 는 anthropic 하나다.

    체인을 만드는 시점에 검사한다: 기동이면 기동 실패, 태스크가 실어 온 선언이면 그 태스크 실패.
    """
    from aaestro.agentd.models import unsupported_provider
    from aaestro.agentd.providers.anthropic import AnthropicModel

    def build(config: ModelConfig) -> Any:
        if config.provider != ANTHROPIC_PROVIDER:
            raise unsupported_provider(agent, config.provider)
        return AnthropicModel(config=config, agent=agent)

    return build


def _telemetry_for(manifest: AgentManifest, metrics: Metrics | None) -> ExecutorTelemetry | None:
    """이 에이전트의 계측기 — 메트릭 미주입 시 None.

    ``graph`` 는 여기서 정해지지 않는다: 태스크마다 다르므로 기록 시점에
    ``TraceContext`` 에서 읽는다 (#113).
    """
    if metrics is None:
        return None
    return ExecutorTelemetry(
        metrics,
        agent=manifest.name,
        group=manifest.metadata.group or "",
        provider=manifest.spec.model.provider,
        model=manifest.spec.model.name,
    )


def _executable_schemas(result: Any, tools: AgentToolRegistry) -> list[SkillSpec]:
    """Advertise only the tools this executor can actually run.

    실행할 수 없는 tool 을 모델에게 보이면 **tool 에러 루프**에 빠집니다 —
    모델이 고를 때마다 거부되고, 그 실패를 보고 다시 고릅니다.

    지금 빠지는 것:

    - ``memory_search`` — ``MemoryAccess`` 주입 경로가 아직 없습니다 (#111)
    - MCP tool — 세션이 없으면 부를 수 없습니다
    """
    runnable: list[SkillSpec] = []
    for name, spec in result.tools.items():
        if name == MEMORY_SEARCH_TOOL and tools.memory is None:
            continue
        if name == ASK_PEER_TOOL and tools.peers is None:
            continue
        if is_mcp_tool(name) and tools.mcp is None:
            # 세션이 없으면 부를 수 없다 — 실행할 수 없는 tool 을 광고하면
            # 모델이 고를 때마다 거부되어 루프에 빠진다
            continue
        runnable.append(spec)
    return runnable


def _template_output_keys(result: Any, task: Any) -> tuple[str, ...]:
    """이 태스크가 쓰는 템플릿이 요구하는 응답 키.

    계약은 **템플릿에** 붙어 있다 — 같은 에이전트가 노드마다 다른 키를 내야
    하고, 요구하는 곳과 선언하는 곳이 같아야 드리프트가 불가능하다 (#150).
    """
    if result.promptset is None:
        return ()
    template = result.promptset.manifest.spec.templates.get(task.template_name)
    return () if template is None else template.output_keys


SYSTEM_TEMPLATE = "system"
"""운영자 지시 템플릿 이름 — 모든 promptset 이 선언한다. 태스크 입력과 **따로** 보낸다 (#308)."""


def _system(result: Any) -> str:
    """promptset 의 ``system`` 템플릿 — 없으면 빈 지시.

    선언돼 있는데 렌더되지 않으면 도구 결과를 믿지 말라는 운영자 지시가 모델에 닿지 않는다.
    변수 없이 렌더한다: 운영자 지시는 태스크마다 바뀌는 입력이 아니다.
    """
    if result.promptset is None or SYSTEM_TEMPLATE not in result.promptset.template_names:
        return ""
    rendered: str = result.promptset.render(SYSTEM_TEMPLATE)
    return rendered.strip()


def _render(result: Any, task: Any) -> str:
    """promptset 으로 태스크 프롬프트를 렌더링한다.

    ``node_id`` 가 없는 direct 요청은 ``default`` 템플릿을 쓴다
    (04 Compatibility Rules 4).
    """
    if result.promptset is None:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message="agent has no promptset to render with",
        )
    rendered: str = result.promptset.render(task.template_name, **task.input)
    return rendered


def _configure_logging() -> None:
    """Configure structured logging — 기동 판정보다 먼저 불려야 한다."""
    from aaestro.observability.logging import configure

    configure(
        level=os.environ.get(LOG_LEVEL_ENV, "INFO"),
        json_output=os.environ.get(LOG_FORMAT_ENV, "json") == "json",
    )


def _setup_observability() -> Metrics:
    """Expose metrics for this process (logging is configured by ``_configure_logging``).

    메트릭을 노출합니다. 계측 로직이 있어도 여기서 registry 를
    만들어 주입하지 않으면 **런타임에서는 아무 것도 흐르지 않습니다** (#95).

    Returns:
        The process-wide metric registry.
    """
    from aaestro.observability.metrics import Metrics, start_metrics_server

    metrics = Metrics()
    # registry 를 함께 넘겨야 한다 — 빠뜨리면 prometheus 기본 registry 를
    # 노출하게 되고, 우리가 채우는 것과 **다른 곳**이라 endpoint 가 늘 비어 있다
    start_metrics_server(
        int(os.environ.get(METRICS_PORT_ENV, DEFAULT_METRICS_PORT)),
        registry=metrics.registry,
    )
    return metrics


def main() -> None:
    """Run the agent daemon.

    에이전트 데몬을 실행합니다 — manifest 로드 → 실행기 선택 → Control API 서빙.
    """
    # 거절 WARN/에러가 구조화 로그로 나가도록 로깅을 먼저 — 메트릭 포트는 거절된 뒤에 연다
    _configure_logging()
    serving = _serving_from_env()
    manifest = load_manifest(Path(os.environ.get(MANIFEST_ENV, DEFAULT_MANIFEST_PATH)))
    metrics = _setup_observability()
    asyncio.run(_run(manifest, metrics, serving))


@dataclass(frozen=True)
class Serving:
    """기동 판정의 결과 — 판정에 쓴 바인드 주소와 토큰을 **그대로** 서빙까지 나른다."""

    token: str | None
    bind_host: str


def _serving_from_env() -> Serving:
    # env 는 여기서 한 번만 읽는다 — 판정과 바인드가 다른 값을 보면 검사가 무의미하다
    bind_host = os.environ.get(BIND_HOST_ENV, DEFAULT_BIND_HOST)
    token = resolve_required_token(
        os.environ, token_env=TOKEN_ENV, bind_host=bind_host, component="agentd"
    )
    return Serving(token=token, bind_host=bind_host)


async def _run(manifest: AgentManifest, metrics: Metrics, serving: Serving) -> None:
    """조립과 서빙을 **한 이벤트 루프**에서 — MCP 세션(자식 프로세스·HTTP 스트림)은 연 루프에 산다.

    조립을 따로 ``asyncio.run`` 하면 그 루프가 닫히며 세션이 죽은 채 서빙이 시작된다.
    """
    executor = await build_executor(manifest, metrics=metrics)
    # 실행기를 받은 순간부터 세션 정리는 여기 몫이다 — 앱 조립이 실패해도 세션을 남기지 않는다
    try:
        from aaestro.agentd.executor import Executor

        app = build_app(
            manifest,
            executor,
            token=serving.token,
            # 광고와 실행이 같은 목록을 봐야 peer 가 부를 수 없는 skill 을 보지 않는다
            tools=getattr(executor, "tool_schemas", ()),
            # 모듈에서 도구를 만드는 표준 실행기만 리로드할 것이 있다. 커스텀 entrypoint 가
            # Executor 를 돌려주더라도 그 배선은 표준 조립과 다를 수 있으므로 리로드하지 않는다
            reload=(
                build_reload(manifest, executor)
                if manifest.spec.entrypoint is None and isinstance(executor, Executor)
                else None
            ),
        )
        log.info(
            "agentd starting",
            agent=manifest.name,
            agent_version=manifest.metadata.version,
            port=CONTROL_PORT,
        )
        await _serve(app, manifest, executor, serving.bind_host)
    finally:
        await _close_wiring(executor)


async def _close_wiring(executor: Any) -> None:
    """종료 시 배선이 쥔 연결을 정리한다.

    MCP 세션과 자식 프로세스 — 좀비 금지 (03 Session Management 2). Memory Service 클라이언트 —
    열린 커넥션을 두고 끝내지 않는다 (#321).
    """
    tools = getattr(getattr(executor, "binding", None), "tools", None)
    mcp = getattr(tools, "mcp", None)
    if mcp is not None:
        await mcp.shutdown()
    close_memory = getattr(getattr(tools, "memory", None), "aclose", None)
    if close_memory is not None:
        await close_memory()


async def _serve(app: Any, manifest: AgentManifest, executor: Any, bind_host: str) -> None:
    """Control API 를 서빙하고, 선언되어 있으면 A2A 도 함께 띄운다.

    03 기동 순서 5단계 — 두 포트는 **별개 서버**다: Control 은 runtime 만,
    A2A 는 allowlist 된 peer 만 접근한다 (02 Network 5).
    """
    from aaestro.agentd.a2a_server import a2a_port, build_a2a_app

    peer_app = build_a2a_app(manifest, executor.execute)
    port = a2a_port()
    if peer_app is None or port is None:
        config = uvicorn.Config(app, host=bind_host, port=CONTROL_PORT, log_config=None)
        await uvicorn.Server(config).serve()
        return

    await _serve_both(app, peer_app, port, bind_host)


async def _serve_both(control: Any, peer: Any, peer_port: int, bind_host: str) -> None:
    """두 서버를 함께 돌린다 — 하나가 끝나면 다른 하나도 정리한다."""
    servers = [
        uvicorn.Server(uvicorn.Config(control, host=bind_host, port=CONTROL_PORT, log_config=None)),
        uvicorn.Server(uvicorn.Config(peer, host=bind_host, port=peer_port, log_config=None)),
    ]
    async with asyncio.TaskGroup() as group:
        for server in servers:
            group.create_task(server.serve())


if __name__ == "__main__":
    main()
