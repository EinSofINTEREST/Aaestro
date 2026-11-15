"""Decision provider adapter registry — ``kind`` to adapter.

``kind`` → 어댑터 생성자 표. 시스템은 ``DecisionModel`` 계약 하나에 의존하고, provider 는 이
표의 한 줄로 바뀐다 (01 Decision Models).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Protocol

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.decision.providers import jev

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from aaestro.core.decision import Decision, Question


class DecisionProvider(Protocol):
    """A ``DecisionModel`` that also states its per-request limits.

    한 요청의 질문 수 · state 길이 상한 — 배치하는 쪽 (메모리 정리 등) 이 이 안으로 나눈다.
    """

    max_questions: int
    max_state_chars: int

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]: ...


ADAPTERS: Final[Mapping[str, Callable[..., DecisionProvider]]] = {
    jev.KIND: jev.JevDecisionModel,
}


def adapter_for(kind: str) -> Callable[..., DecisionProvider]:
    """Look up the adapter constructor for a provider ``kind``.

    Raises:
        AaestroError: CONFIG/``CFG_001`` for an unknown kind — 모르는 provider 를
            조용히 건너뛰지 않는다.
    """
    try:
        return ADAPTERS[kind]
    except KeyError:
        raise AaestroError(
            ErrorCategory.CONFIG,
            ErrorCode.CFG_001,
            "unknown decision provider kind",
            details={"known": sorted(ADAPTERS)},
        ) from None
