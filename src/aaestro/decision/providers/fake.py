"""Scripted decision model for tests.

질문별로 스크립트된 확률을 돌려주는 대역 — 구간은 실제 코드 (``bands``) 가 읽는다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from aaestro.core.decision import Decision, Question
from aaestro.core.errors import AaestroError
from aaestro.decision.bands import decision_from

Answer = float | Sequence[float] | AaestroError


class FakeDecisionModel:
    """Answers each question from a script; an ``AaestroError`` value is raised (unavailable)."""

    def __init__(self, answers: Mapping[str, Answer]) -> None:
        self._answers = answers
        self.asked: list[tuple[str, str]] = []  # (question name, state) 검증용

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]:
        self.asked.extend((q.name, state) for q in questions)
        decisions: list[Decision] = []
        for q in questions:
            answer = self._answers[q.name]
            if isinstance(answer, AaestroError):
                raise answer
            decisions.append(decision_from(q, answer))
        return decisions
