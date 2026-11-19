"""A2A connection allowlist — double defense.

에이전트 간 호출 권한은 **그래프 배선 선언**이 정한다. 그룹 소속은 어떤 권한도
주지 않으며(group neutrality), 방향은 순전히 선언의 문제다(peer symmetry).

이중 방어: caller 측이 선언에 없는 호출을 거부하고, callee 측이 runtime 이
발급한 per-edge token 을 검증한다. 한쪽만으로는 우회가 가능하다.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.protocols.a2a.errors import depth_exceeded, not_allowed

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from aaestro.core.agent import TraceContext

DEFAULT_MAX_DEPTH = 3
"""위임 체인 깊이 상한 — 순환 위임 폭주를 막는 유일한 장치다."""

ALLOW_TOKEN_MODE_ENV = "AAESTRO_ALLOW_A2A_TOKEN_MODE"  # noqa: S105 — 키 이름이지 값이 아니다
"""토큰 모드 opt-in — 개발 · 단일 신뢰 영역 전용.

토큰 모드는 레지스트리 없이 배포가 공유하는 서명 키로 호출자를 확인한다.

그래프의 모든 에이전트가 같은 키를 쥐므로 한 에이전트가 다른 에이전트 이름으로 토큰을 만들 수 있다
(#415). 그래서 명시적으로 켠 구성에서만 받는다 — 무인증 opt-in (``AAESTRO_ALLOW_UNAUTHENTICATED``)
과 같은 결이다."""

_OPT_IN_VALUES = frozenset({"1", "true"})
_OPT_OUT_VALUES = frozenset({"", "0", "false"})

log = structlog.get_logger(__name__)


def token_mode_allowed(environ: Mapping[str, str]) -> bool:
    """Read the token-mode opt-in.

    토큰 모드 opt-in 을 읽는다. 모르는 값을 조용히 끄면 오설정이 늦게 드러나므로 거절한다.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the value is not 1, true, 0 or false.
    """
    raw = environ.get(ALLOW_TOKEN_MODE_ENV, "").strip().lower()
    if raw not in _OPT_IN_VALUES | _OPT_OUT_VALUES:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"{ALLOW_TOKEN_MODE_ENV} must be 1, true, 0 or false",
            details={"setting": ALLOW_TOKEN_MODE_ENV},
        )
    return raw in _OPT_IN_VALUES


def admit_token_mode(*, allowed: bool, component: str, **context: str) -> None:
    """Refuse token mode unless opted in; warn when it is.

    토큰 모드가 선택됐을 때 부른다 — opt-in 이 없으면 거절하고, 있으면 사칭을 막지 못한다고 WARN.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if token mode is not explicitly allowed.
    """
    if not allowed:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=(
                "a2a token mode cannot stop one agent from impersonating another — "
                f"configure the access registry, or set {ALLOW_TOKEN_MODE_ENV}=1 "
                "for development or a single trust domain"
            ),
            details={"component": component, "setting": ALLOW_TOKEN_MODE_ENV, **context},
        )
    log.warning(
        "a2a token mode allowed by opt-in — it does not stop peer impersonation",
        component=component,
        setting=ALLOW_TOKEN_MODE_ENV,
        **context,
    )


@dataclass(frozen=True)
class Edge:
    """One declared caller → callee direction.

    선언된 호출 방향 하나. 역방향은 별도 선언이 필요하다 — 방향은 선언의
    문제이지 우열의 문제가 아니다.
    """

    caller: str
    callee: str


def edges_from(connections: Iterable[object]) -> frozenset[Edge]:
    """Build the edge set from graph connection declarations.

    그래프의 ``connections`` 선언에서 edge 집합을 만듭니다.

    Args:
        connections: Objects exposing ``caller`` and ``callee``.

    Returns:
        The declared directed edges.
    """
    return frozenset(
        Edge(caller=str(c.caller), callee=str(c.callee))  # type: ignore[attr-defined]
        for c in connections
    )


def issue_token(secret: bytes, edge: Edge) -> str:
    """Issue the per-edge token for a declared connection.

    선언된 연결 하나에 대한 per-edge token 을 발급합니다. runtime 이 발급하고
    callee 가 검증합니다 — caller 의 주장만 믿지 않기 위해서입니다.

    Args:
        secret: The runtime signing secret.
        edge: The declared direction.

    Returns:
        The hex token for this edge.
    """
    message = f"{edge.caller}->{edge.callee}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Allowlist:
    """The declared A2A connections for one graph.

    한 그래프의 A2A 연결 선언. 에이전트는 이 목록 밖의 peer 를 알지도, 부르지도
    못한다.

    ``secret`` 은 필수다 — 기본값을 두면 runtime 이 주입을 빠뜨렸을 때 token 이
    공개된 키로 HMAC 되어 누구나 계산할 수 있고, callee 측 방어가 통째로
    무력화된다.
    """

    edges: frozenset[Edge]
    secret: bytes
    max_depth: int = DEFAULT_MAX_DEPTH

    def __post_init__(self) -> None:
        if not self.secret:
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_002,
                message="a2a allowlist requires a non-empty signing secret",
            )

    def allows(self, caller: str, callee: str) -> bool:
        """이 방향의 호출이 선언되었는지 — 같은 그룹이어도 선언이 없으면 False."""
        return Edge(caller=caller, callee=callee) in self.edges

    def peers_of(self, caller: str) -> tuple[str, ...]:
        """Peers this agent may call.

        이 에이전트가 부를 수 있는 peer 목록. 에이전트는 주소를 직접 알지 못하고
        이 목록으로만 discovery 합니다 (03 Discovery).

        Args:
            caller: The calling agent name.

        Returns:
            Callee names, sorted for stable ordering.
        """
        return tuple(sorted(e.callee for e in self.edges if e.caller == caller))

    def check_call(self, caller: str, callee: str, trace: TraceContext) -> None:
        """Enforce the caller-side allowlist and depth limit.

        caller 측 방어입니다 — 선언에 없는 호출과 상한을 넘은 위임을 거부합니다.

        Args:
            caller: The calling agent.
            callee: The called agent.
            trace: The trace context carrying delegation depth.

        Raises:
            AaestroError: A2A/``A2A_004`` if the direction is not declared,
                ``A2A_005`` if the delegation chain is too deep.
        """
        if not self.allows(caller, callee):
            raise not_allowed(caller, callee)
        # 깊이는 이 호출이 만들 자식의 깊이로 판정한다
        if trace.depth + 1 > self.max_depth:
            raise depth_exceeded(caller, callee, depth=trace.depth + 1, limit=self.max_depth)

    def token_for(self, caller: str, callee: str) -> str:
        """Issue the token a caller must present.

        caller 가 제시해야 할 token 을 발급합니다.

        Raises:
            AaestroError: A2A/``A2A_004`` if the direction is not declared —
                선언되지 않은 방향에는 token 을 발급하지 않습니다.
        """
        edge = Edge(caller=caller, callee=callee)
        if edge not in self.edges:
            raise not_allowed(caller, callee)
        return issue_token(self.secret, edge)

    def verify(self, caller: str, callee: str, token: str) -> None:
        """Enforce the callee-side token check.

        callee 측 방어입니다 — caller 가 자기 이름을 주장하는 것만으로는
        부족하므로 runtime 이 발급한 token 을 검증합니다.

        Args:
            caller: The claimed calling agent.
            callee: This agent.
            token: The presented token.

        Raises:
            AaestroError: A2A/``A2A_004`` if the direction is undeclared or the
                token does not match.
        """
        # 수신 측 판정이므로 에러는 이 에이전트(callee)에 귀속시킨다 —
        # caller 에 귀속하면 남의 이름으로 우리 로그가 오염된다
        edge = Edge(caller=caller, callee=callee)
        if edge not in self.edges:
            raise not_allowed(caller, callee, owner=callee)
        expected = issue_token(self.secret, edge)
        # 타이밍 공격을 피해 상수 시간 비교
        if not hmac.compare_digest(expected, token):
            raise not_allowed(caller, callee, owner=callee, reason="invalid edge token")


__all__ = [
    "ALLOW_TOKEN_MODE_ENV",
    "DEFAULT_MAX_DEPTH",
    "Allowlist",
    "Edge",
    "admit_token_mode",
    "edges_from",
    "issue_token",
    "token_mode_allowed",
]
