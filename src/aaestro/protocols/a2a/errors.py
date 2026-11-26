"""A2A error mapping.

A2A boundary 변환. 호출 거부와 실패를 구분해야 caller 가 재시도/라우팅을
판단할 수 있다 (05 Layer Rules).
"""

from __future__ import annotations

from typing import Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode


def a2a_error(
    code: ErrorCode | str,
    message: str,
    *,
    caller: str,
    callee: str,
    retryable: bool = False,
    **details: Any,
) -> AaestroError:
    """Build a structured A2A error.

    A2A 실패를 구조화 에러로 만듭니다 — 표준 로그 필드
    (``a2a_caller``, ``a2a_callee``) 를 항상 채웁니다.

    Args:
        code: The A2A error code.
        message: Lowercase message without a trailing period.
        caller: Calling agent name.
        callee: Called agent name.
        retryable: Whether a retry could succeed.
        **details: Extra machine-readable context.

    Returns:
        The structured error.
    """
    return AaestroError(
        category=ErrorCategory.A2A,
        code=code,
        message=message,
        agent=caller,
        retryable=retryable,
        # 표준 필드가 뒤에 온다 — details 가 a2a_caller/a2a_callee 를 덮어쓰면
        # 로그·메트릭의 호출 방향이 조용히 틀어진다
        details={**details, "a2a_caller": caller, "a2a_callee": callee},
    )


def submit_failed(caller: str, callee: str, **details: Any) -> AaestroError:
    """태스크 제출 실패 — 일시적일 수 있으므로 retryable.

    피호출자가 재시도 가능한 사유 (칸 대기 마감 ``TO_001`` · drain ``RT_005`` 등) 로 답한 실패도
    여기다 — 그 사유 (``peer_error``) 를 원인으로 붙인다.
    """
    return _with_peer_reason(
        a2a_error(
            ErrorCode.A2A_001,
            f"a2a task submission failed: {callee}",
            caller=caller,
            callee=callee,
            retryable=True,
            **details,
        ),
        details,
    )


def unreachable(caller: str, callee: str, **details: Any) -> AaestroError:
    """Peer 도달 불가 — 재시도 가능."""
    return a2a_error(
        ErrorCode.A2A_002,
        f"a2a peer unreachable: {callee}",
        caller=caller,
        callee=callee,
        retryable=True,
        **details,
    )


class PeerTaskError(Exception):
    """피호출자가 보고한 실패 사유 — `task_rejected` 의 원인으로만 쓴다."""


def task_rejected(caller: str, callee: str, **details: Any) -> AaestroError:
    """Callee 가 태스크를 거부/실패 — 재시도해도 같은 결과.

    peer 가 준 사유 — ``peer_error`` 의 코드 · 메시지, 없으면 ``reason`` (SDK 가 문자열로만 실어 온
    거부) — 를 원인 예외로 붙인다. details 는 모델에 실리지 않으므로, 원인이 없으면 호출자
    (ask_peer 를 부른 모델) 가 peer 의 실패 사유를 받지 못한다.
    """
    return _with_peer_reason(
        a2a_error(
            ErrorCode.A2A_003,
            f"a2a task failed on peer: {callee}",
            caller=caller,
            callee=callee,
            **details,
        ),
        details,
    )


def peer_failure(caller: str, callee: str, peer_error: Any, **details: Any) -> AaestroError:
    """피호출자가 실패로 답한 태스크를 호출자 쪽 에러로 — SDK · transport 두 경로가 같이 쓴다.

    실행을 시작하지 않은 재시도 가능 거절 (칸 대기 마감 · drain — ``details.started`` 가 false) 만
    제출 실패 (``A2A_001``, retryable) 다. 실행 뒤의 실패는 retryable 이어도 부수효과가 났을 수 있어
    ``A2A_003`` 이다. ``started`` 는 피호출자가 정한 값이라 신뢰할 수 없다 — 자동 재시도 계층이 이
    값으로 판단하게 되면 다시 따진다.
    """
    if isinstance(peer_error, dict):
        retryable, extra = peer_error.get("retryable"), peer_error.get("details")
    else:
        retryable = getattr(peer_error, "retryable", None)
        extra = getattr(peer_error, "details", None)
    if retryable is True and isinstance(extra, dict) and extra.get("started") is False:
        return submit_failed(caller, callee, peer_error=peer_error, **details)
    return task_rejected(caller, callee, peer_error=peer_error, **details)


def _with_peer_reason(error: AaestroError, details: dict[str, Any]) -> AaestroError:
    """peer 가 준 사유를 원인 예외로 붙인다 — details 는 모델에 실리지 않는다."""
    reason = _peer_reason(details.get("peer_error"))
    if not reason and isinstance(details.get("reason"), str):
        reason = details["reason"]
    if reason:
        error.__cause__ = PeerTaskError(reason)
    return error


def _peer_reason(peer_error: Any) -> str:
    """``code: message`` — ``AaestroErrorPayload`` (Control 경로) 또는 dict (SDK 경로) 에서."""
    if isinstance(peer_error, dict):
        code, message = peer_error.get("code"), peer_error.get("message")
    else:
        code, message = getattr(peer_error, "code", None), getattr(peer_error, "message", None)
    parts = [part for part in (code, message) if isinstance(part, str) and part]
    return ": ".join(parts)


def not_allowed(
    caller: str, callee: str, *, owner: str | None = None, **details: Any
) -> AaestroError:
    """Allowlist 위반 — 배선 문제이므로 재시도 무의미.

    Args:
        caller: The calling agent.
        callee: The called agent.
        owner: The agent this error belongs to — 수신 측 판정이면 callee 다.
            생략 시 caller 에 귀속한다.
        **details: Extra context.
    """
    error = a2a_error(
        ErrorCode.A2A_004,
        f"a2a connection not declared: {caller} -> {callee}",
        caller=caller,
        callee=callee,
        **details,
    )
    if owner is None or owner == caller:
        return error
    return AaestroError(
        category=error.category,
        code=error.code,
        message=error.message,
        agent=owner,
        retryable=error.retryable,
        details=error.details,
    )


def depth_exceeded(caller: str, callee: str, *, depth: int, limit: int) -> AaestroError:
    """위임 체인이 상한을 넘음 — 순환 위임 방지."""
    return a2a_error(
        ErrorCode.A2A_005,
        "a2a call depth exceeded",
        caller=caller,
        callee=callee,
        depth=depth,
        limit=limit,
    )


__all__ = [
    "PeerTaskError",
    "a2a_error",
    "depth_exceeded",
    "not_allowed",
    "submit_failed",
    "task_rejected",
    "unreachable",
]
