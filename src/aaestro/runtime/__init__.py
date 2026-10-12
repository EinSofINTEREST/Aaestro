"""Agent runtime — container lifecycle, control API, resource scoping.

에이전트 런타임. Docker SDK 를 직접 만지는 유일한 레이어이며,
오케스트레이터에게는 에이전트를 async callable 로 노출한다.
"""

from aaestro.runtime.control import (
    DEFAULT_CONTROL_PORT,
    ControlClient,
    control_url,
)
from aaestro.runtime.launcher import AgentLauncher, LaunchedAgent
from aaestro.runtime.lifecycle import (
    AgentLifecycle,
    AgentState,
    RestartPolicy,
)
from aaestro.runtime.quota import (
    ResourceTotals,
    check_group_quota,
    check_host_capacity,
    demand_of,
    total_demand,
)
from aaestro.runtime.scope import ResolvedSecret, ScopedSecrets, SecretScope
from aaestro.runtime.spec import (
    DEFAULT_NETWORK,
    ContainerSpec,
    PortBinding,
    build_container_spec,
    container_name,
)
from aaestro.runtime.tokens import (
    AGENT_TOKEN_ENV,
    TokenIssuer,
    authenticated_env,
    generate_token,
)

__all__ = [
    "AgentLauncher",
    "LaunchedAgent",
    "AGENT_TOKEN_ENV",
    "DEFAULT_CONTROL_PORT",
    "DEFAULT_NETWORK",
    "AgentLifecycle",
    "AgentState",
    "ContainerSpec",
    "ControlClient",
    "PortBinding",
    "ResolvedSecret",
    "ResourceTotals",
    "RestartPolicy",
    "ScopedSecrets",
    "SecretScope",
    "build_container_spec",
    "check_group_quota",
    "check_host_capacity",
    "container_name",
    "control_url",
    "demand_of",
    "total_demand",
    "TokenIssuer",
    "authenticated_env",
    "generate_token",
]
