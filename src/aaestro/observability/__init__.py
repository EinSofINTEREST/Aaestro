"""Observability — structured logging and metrics.

관측성. 로그는 run_id 로 전 레이어를 관통해 추적되고, 메트릭 이름은
대시보드·알림이 의존하는 계약이므로 고정된다.
"""

from aaestro.observability.logging import (
    REDACTED,
    STANDARD_FIELDS,
    TRUNCATED,
    LogField,
    bind_a2a,
    bind_agent,
    bind_mcp,
    bind_module,
    bind_run,
    bind_task,
    configure,
    get_logger,
    mask_secrets,
)
from aaestro.observability.metrics import (
    DEFAULT_METRICS_PORT,
    METRIC_SPECS,
    Metrics,
    MetricSpec,
    snapshot,
    start_metrics_server,
)

__all__ = [
    "DEFAULT_METRICS_PORT",
    "METRIC_SPECS",
    "REDACTED",
    "TRUNCATED",
    "STANDARD_FIELDS",
    "LogField",
    "MetricSpec",
    "Metrics",
    "bind_a2a",
    "bind_agent",
    "bind_mcp",
    "bind_module",
    "bind_run",
    "bind_task",
    "configure",
    "get_logger",
    "mask_secrets",
    "snapshot",
    "start_metrics_server",
]
