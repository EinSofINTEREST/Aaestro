"""Agent daemon — the in-container execution runtime.

에이전트 컨테이너 내부 실행 데몬. BaseAgent 계약을 Agent Control API 로 서빙하며,
대부분의 에이전트는 manifest 선언만으로 이 기본 실행 루프를 사용한다.
"""

from aaestro.agentd.bootstrap import (
    MCP_STARTUP_TIMEOUT_S,
    Bootstrap,
    BootstrapResult,
    McpLauncher,
    build_tool_registry,
)
from aaestro.agentd.executor import (
    Executor,
    ExecutorConfig,
    Model,
    ModelResponse,
    ToolCall,
    ToolRegistry,
)
from aaestro.agentd.server import (
    DEFAULT_MAX_CONCURRENT_TASKS,
    AgentRuntime,
    create_app,
)
from aaestro.core.tools import namespaced

__all__ = [
    "DEFAULT_MAX_CONCURRENT_TASKS",
    "MCP_STARTUP_TIMEOUT_S",
    "AgentRuntime",
    "Bootstrap",
    "BootstrapResult",
    "Executor",
    "ExecutorConfig",
    "Model",
    "ModelResponse",
    "ToolCall",
    "McpLauncher",
    "ToolRegistry",
    "build_tool_registry",
    "create_app",
    "namespaced",
]
