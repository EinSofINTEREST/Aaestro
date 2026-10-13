"""Framework core contracts.

프레임워크 핵심 계약 — 모든 레이어가 의존하고, 내부적으로는 아무것도 의존하지 않는다.
순수 스키마/인터페이스/에러만 두며 I/O 를 수행하지 않는다.
"""

from aaestro.core.agent import (
    AgentContext,
    BaseAgent,
    ComponentHealth,
    HealthState,
    HealthStatus,
    ModelUsage,
    TaskConfig,
    TaskKey,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TraceContext,
)
from aaestro.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    AaestroError,
    AaestroErrorPayload,
    CircuitBreaker,
    CircuitState,
    ErrorCategory,
    RetryPolicy,
)
from aaestro.core.events import (
    DoneEvent,
    ErrorEvent,
    TaskEvent,
    TokenEvent,
    ToolCallEvent,
    ToolResultEvent,
)
from aaestro.core.manifest import (
    AgentManifest,
    AgentSpec,
    CircuitDeclaration,
    FallbackModel,
    GroupManifest,
    GroupSpec,
    McpServerSpec,
    MemorySpaceRef,
    ModelConfig,
    ModelOverride,
    ModelRetry,
    ModuleRefStr,
    ResourceSpec,
    RetryDeclaration,
)

# ``skill`` 데코레이터는 여기서 re-export 하지 않는다 — 같은 이름의 하위 모듈을
# 가려서 ``aaestro.core.skill`` 이 모듈이 아니라 함수로 잡힌다 (#87).
# 데코레이터는 ``from aaestro.core.skill import skill`` 로 가져온다
from aaestro.core.skill import SkillContext, SkillSpec

__all__ = [
    "NETWORK_RETRY",
    "RATE_LIMIT_RETRY",
    "AgentContext",
    "AgentManifest",
    "AgentSpec",
    "BaseAgent",
    "CircuitBreaker",
    "CircuitDeclaration",
    "CircuitState",
    "ComponentHealth",
    "DoneEvent",
    "ErrorCategory",
    "ErrorEvent",
    "FallbackModel",
    "GroupManifest",
    "GroupSpec",
    "HealthState",
    "HealthStatus",
    "AaestroError",
    "AaestroErrorPayload",
    "McpServerSpec",
    "MemorySpaceRef",
    "ModelConfig",
    "ModelOverride",
    "ModelRetry",
    "ModelUsage",
    "ModuleRefStr",
    "ResourceSpec",
    "RetryDeclaration",
    "RetryPolicy",
    "SkillContext",
    "SkillSpec",
    "TaskConfig",
    "TaskKey",
    "TaskEvent",
    "TaskRequest",
    "TaskResult",
    "TaskStatus",
    "TokenEvent",
    "ToolCallEvent",
    "ToolResultEvent",
    "TraceContext",
]
