"""Per-agent A2A server and client.

에이전트별 A2A 노출과 호출. 연결 권한은 그래프 배선 선언이 정하며,
에이전트 간 우열 관계는 존재하지 않는다.
"""

from aaestro.protocols.a2a.allowlist import (
    DEFAULT_MAX_DEPTH,
    Allowlist,
    Edge,
    edges_from,
    issue_token,
)
from aaestro.protocols.a2a.card import AgentCard, SkillCard, build_card
from aaestro.protocols.a2a.client import (
    DEFAULT_CALL_TIMEOUT_S,
    A2AClient,
    A2AServer,
    PeerTransport,
    map_status,
)
from aaestro.protocols.a2a.errors import (
    a2a_error,
    depth_exceeded,
    not_allowed,
    submit_failed,
    task_rejected,
    unreachable,
)

__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_MAX_DEPTH",
    "A2AClient",
    "A2AServer",
    "AgentCard",
    "Allowlist",
    "Edge",
    "PeerTransport",
    "SkillCard",
    "a2a_error",
    "build_card",
    "depth_exceeded",
    "edges_from",
    "issue_token",
    "map_status",
    "not_allowed",
    "submit_failed",
    "task_rejected",
    "unreachable",
]
