"""Docker error mapping.

Docker SDK 예외를 구조화 에러로 변환한다. 재시작 정책이 코드에 따라 갈리므로
(이미지 문제는 재시도 무의미, OOM 은 리소스 조정 필요) boundary 에서 반드시
구분해 올린다.
"""

from __future__ import annotations

from typing import Any

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

OOM_EXIT_CODE = 137
"""SIGKILL(128+9) — OOM killer 가 남기는 종료 코드."""


def runtime_error(
    code: ErrorCode,
    message: str,
    *,
    agent: str,
    retryable: bool = False,
    **details: Any,
) -> AaestroError:
    """Build a structured runtime error.

    런타임 실패를 구조화 에러로 만듭니다 — 컨테이너 조작 로그의 표준 필드
    (``container_id``, ``image``)를 details 로 전달합니다.

    Args:
        code: The runtime error code.
        message: Lowercase message without a trailing period.
        agent: The owning agent.
        retryable: Whether a retry could succeed.
        **details: Extra machine-readable context.

    Returns:
        The structured error.
    """
    return AaestroError(
        category=ErrorCategory.RUNTIME,
        code=code,
        message=message,
        agent=agent,
        retryable=retryable,
        details=details,
    )


def image_unavailable(agent: str, image: str, **details: Any) -> AaestroError:
    """이미지 pull/존재 확인 실패 — 설정 문제이므로 재시도해도 같다."""
    return runtime_error(
        ErrorCode.RT_004,
        f"agent image is unavailable: {image}",
        agent=agent,
        image=image,
        **details,
    )


class NetworkIsolationError(Exception):
    """An existing network's isolation differs from what the runtime needs."""

    def __init__(self, name: str, *, expected: bool, actual: bool) -> None:
        want = "internal" if expected else "not internal"
        super().__init__(f"network {name} must be {want}")
        self.name = name
        self.expected = expected
        self.actual = actual


def network_mismatch(
    agent: str, image: str, err: NetworkIsolationError, **details: Any
) -> AaestroError:
    """에이전트 네트워크의 격리가 runtime 과 다르다 — 설정 문제라 재시도해도 같다."""
    return runtime_error(
        ErrorCode.RT_001,
        "agent network isolation does not match the runtime",
        agent=agent,
        image=image,
        network=err.name,
        reason=str(err),
        **details,
    )


def start_failed(agent: str, image: str, **details: Any) -> AaestroError:
    """컨테이너 기동 실패 — 일시적 자원 부족일 수 있어 재시도 가능."""
    return runtime_error(
        ErrorCode.RT_001,
        "container failed to start",
        agent=agent,
        retryable=True,
        image=image,
        **details,
    )


def invalid_spec(agent: str, image: str, **details: Any) -> AaestroError:
    """컨테이너 스펙 자체가 계약을 어김 — 결정적 오류이므로 재시도 금지.

    재시도 가능으로 표시하면 고쳐지지 않을 스펙으로 재시작 루프가 돈다.
    """
    return runtime_error(
        ErrorCode.RT_001,
        "container spec violates the runtime contract",
        agent=agent,
        image=image,
        **details,
    )


def oom_killed(agent: str, container_id: str, **details: Any) -> AaestroError:
    """OOM kill — 메모리 상한을 올리지 않으면 재시도해도 같은 결과다."""
    return runtime_error(
        ErrorCode.RT_003,
        "container was oom killed",
        agent=agent,
        container_id=container_id,
        **details,
    )


def drain_timeout(agent: str, container_id: str, **details: Any) -> AaestroError:
    """Drain 시간 초과 — 진행 중 태스크가 제때 끝나지 않았다."""
    return runtime_error(
        ErrorCode.RT_005,
        "drain did not complete within the grace period",
        agent=agent,
        container_id=container_id,
        **details,
    )


__all__ = [
    "OOM_EXIT_CODE",
    "drain_timeout",
    "image_unavailable",
    "invalid_spec",
    "oom_killed",
    "runtime_error",
    "start_failed",
]
