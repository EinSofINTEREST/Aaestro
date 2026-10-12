"""Agent health for ``GET /v1/health``.

02 는 health 가 "모델 연결, MCP 세션, 의존 모듈 상태" 를 종합한다고 규정한다. 이전에는 항상
healthy 로 답해, MCP 세션이 재연결을 소진해도 runtime 의 재시작 정책은 프로세스가 죽을 때만
움직였다 (#311).

판정 규칙:

- 필수 MCP 서버 세션이 재연결을 소진하면 **unhealthy** — 재시작이 세션을 다시 세운다
- optional 서버의 실패는 **degraded** — 살아있고, 재시작 대상이 아니다
- 모델은 **최근 호출 기록**으로 본다. health 마다 provider 를 부르지 않는다. 실패는 degraded 까지만:
  provider 장애는 컨테이너를 재시작해도 낫지 않는다
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from aaestro.core.agent import ComponentHealth, HealthState, HealthStatus

OPTIONAL_UNAVAILABLE = "optional server unavailable"


@dataclass
class ModelHealth:
    """The most recent model call outcome — 호출이 성공하면 지난 실패는 지워진다."""

    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    last_error_code: str | None = None

    def succeeded(self) -> None:
        self.last_success_at = datetime.now(UTC)

    def failed(self, code: str) -> None:
        self.last_error_at = datetime.now(UTC)
        self.last_error_code = code

    def component(self) -> ComponentHealth:
        """최근 호출이 실패했으면 degraded — 아직 호출이 없으면 판정할 근거가 없어 healthy."""
        failing = self.last_error_at is not None and (
            self.last_success_at is None or self.last_error_at > self.last_success_at
        )
        if failing:
            return ComponentHealth(
                state=HealthState.DEGRADED,
                detail=f"last model call failed: {self.last_error_code}",
            )
        return ComponentHealth(state=HealthState.HEALTHY)


def agent_health(executor: Any) -> HealthStatus:
    """Aggregate modules, MCP sessions and the model into one status.

    모듈·MCP 세션·모델 상태를 하나로 종합합니다. 모듈을 쓰지 않는 커스텀 실행기는 볼 컴포넌트가
    없어 healthy 입니다.

    Args:
        executor: The running executor — 표준 실행기면 ``binding`` 과 ``model_health`` 가 있다.

    Returns:
        The aggregated status.
    """
    components: dict[str, ComponentHealth] = {}
    binding = getattr(executor, "binding", None)
    if binding is not None:
        components["modules"] = ComponentHealth(state=HealthState.HEALTHY)
        components.update(_mcp_components(binding))
    model = getattr(executor, "model_health", None)
    if isinstance(model, ModelHealth):
        components["model"] = model.component()
    return HealthStatus.aggregate(components)


def _mcp_components(binding: Any) -> dict[str, ComponentHealth]:
    """리로드가 묶음을 바꾸면 여기도 새 묶음을 본다 — 매 요청 binding 에서 읽는다."""
    components: dict[str, ComponentHealth] = {}
    mcp = getattr(binding.tools, "mcp", None)
    for name, session in getattr(mcp, "sessions", {}).items():
        health: ComponentHealth = session.health()
        if session.spec.optional and health.state is HealthState.UNHEALTHY:
            # optional 서버가 죽었다고 에이전트를 재시작하면, 없어도 되는 것 때문에 전부를 멈춘다
            health = ComponentHealth(
                state=HealthState.DEGRADED, detail=f"{OPTIONAL_UNAVAILABLE}: {health.detail}"
            )
        components[f"mcp:{name}"] = health
    for name in binding.degraded:
        components.setdefault(
            f"mcp:{name}", ComponentHealth(state=HealthState.DEGRADED, detail=OPTIONAL_UNAVAILABLE)
        )
    return components


__all__ = ["ModelHealth", "agent_health"]
