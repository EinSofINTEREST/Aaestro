"""Egress proxy refusals, told apart from provider failures (#398).

모델 호출은 이그레스 프록시를 지난다. 프록시가 권한 판정으로 거절한 응답 (403 ``ACC_001`` · 503
``ACC_002``) 을 provider 실패로 읽으면 403 은 설정 오류 (INTERNAL) 로, 503 은 provider 장애
(``LLM_003``) 로 올라가 fallback 과 모델 circuit 을 움직인다 — 다른 모델도 같은 프록시가 막는다.

식별은 **프록시만 붙이는 응답 헤더** 로 한다. 본문 모양은 upstream 이 그대로 흉내 낼 수 있고,
프록시는 upstream 응답 헤더를 허용 목록으로만 돌려주므로 이 헤더는 upstream 에서 올 수 없다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Mapping

EGRESS_ERROR_CODE_HEADER: Final = "x-aaestro-egress-error-code"
"""프록시가 자기 거절 응답에 싣는 에러 코드 — ``egress/providers.py`` 와 같은 이름이어야 한다.

agentd 는 egress 를 import 하지 않으므로 (import 경계) 이름을 따로 둔다. 둘이 어긋나면
``tests/unit/agentd/test_openai_compat_proxy.py`` 의 프록시 경유 테스트가 실패한다."""


def egress_refusal(headers: Mapping[str, str], status: int, *, agent: str) -> AaestroError | None:
    """The typed error for a proxy refusal, or None for anything else.

    프록시의 권한 거절이면 그 코드의 에러를, 아니면 None 을 돌려준다 — None 이면 provider 응답이다.
    ``ACC_001`` 은 다시 보내도 같은 답이라 비재시도, ``ACC_002`` 는 레지스트리가 돌아오면 풀리므로
    NETWORK 로 재시도한다 (05 Error Codes).

    응답 객체가 아니라 헤더와 상태를 받는다 — anthropic SDK 의 응답은 ``httpx`` 가 아니라 자기가
    묶은 ``httpx2`` 의 것이다.
    """
    code = headers.get(EGRESS_ERROR_CODE_HEADER)
    details = {"cause": f"http_{status}"}
    if code == ErrorCode.ACC_001:
        return AaestroError(
            category=ErrorCategory.FORBIDDEN,
            code=ErrorCode.ACC_001,
            message="model api call denied by the egress proxy",
            agent=agent,
            retryable=False,
            details=details,
        )
    if code == ErrorCode.ACC_002:
        return AaestroError(
            category=ErrorCategory.NETWORK,
            code=ErrorCode.ACC_002,
            message="egress proxy could not decide the model api call",
            agent=agent,
            retryable=True,
            details=details,
        )
    return None


__all__ = ["EGRESS_ERROR_CODE_HEADER", "egress_refusal"]
