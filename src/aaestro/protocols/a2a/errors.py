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
    """태스크 제출 실패 — 일시적일 수 있으므로 retryable."""
    return a2a_error(
        ErrorCode.A2A_001,
        f"a2a task submission failed: {callee}",
        caller=caller,
        callee=callee,
        retryable=True,
        **details,
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


def task_rejected(caller: str, callee: str, **details: Any) -> AaestroError:
    """Callee 가 태스크를 거부/실패 — 재시도해도 같은 결과."""
    return a2a_error(
        ErrorCode.A2A_003,
        f"a2a task failed on peer: {callee}",
        caller=caller,
        callee=callee,
        **details,
    )


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
    "a2a_error",
    "depth_exceeded",
    "not_allowed",
    "submit_failed",
    "task_rejected",
    "unreachable",
]
