"""Probability to band reading, per question kind.

확률 → act / uncertain / reject. 질문 종류가 확률과 reject 의 뜻을 정한다 (01 Decision Models).
"""

from __future__ import annotations

from collections.abc import Sequence

from aaestro.core.decision import Band, Bands, Decision, Question, QuestionKind
from aaestro.core.errors import AaestroError, ErrorCategory

# provider 가 확률을 소수 몇 자리로 반올림해 합이 1 에서 조금 벗어나므로 이 안이면 정규화해 받는다
_SUM_TOLERANCE = 0.01
# 0.99 · 1.01 은 부동소수 오차로 0.01 을 살짝 넘어 계산되므로 경계 포함을 이 여유로 보장한다
_SUM_EPSILON = 1e-9
# 참일 확률이 이 값을 넘으면 predicate 의 value 는 참 — 구간 (act_at/reject_at) 과는 별개의 값 판정
PREDICATE_TRUE_ABOVE = 0.5
# 서열 척도의 중앙값 — 누적 확률이 처음 이 값에 닿는 등급이 rating 의 value
_MEDIAN = 0.5


def band_for(probability: float, bands: Bands) -> Band:
    """Read a probability against the declared thresholds."""
    if probability >= bands.act_at:
        return Band.ACT
    if probability <= bands.reject_at:
        return Band.REJECT
    return Band.UNCERTAIN


def _invalid(reason: str) -> AaestroError:
    # 받은 값은 싣지 않는다 — 사유 종류만
    return AaestroError(
        ErrorCategory.DECISION,
        "DEC_004",
        f"decision answer rejected: {reason}",
        retryable=False,
    )


def _probability(x: object) -> float:
    if isinstance(x, bool) or not isinstance(x, float | int):
        raise _invalid("probability is not a number")
    # 범위 비교만으로 NaN · inf 도 걸린다 — isfinite 는 거대한 int 에서 OverflowError 를 낸다
    if not 0.0 <= x <= 1.0:
        raise _invalid("probability out of range")
    return float(x)


def _distribution(question: Question, answer: object) -> list[float]:
    if isinstance(answer, str | bytes) or not isinstance(answer, Sequence):
        raise _invalid("distribution is not a sequence")
    if len(answer) != len(question.options):
        raise _invalid("distribution length does not match options")
    probs = [_probability(x) for x in answer]
    total = sum(probs)
    if abs(total - 1.0) > _SUM_TOLERANCE + _SUM_EPSILON:
        raise _invalid("distribution does not sum to 1")
    return [p / total for p in probs]


def decision_from(question: Question, answer: object) -> Decision:
    """Build a ``Decision`` from a provider's raw probabilities.

    predicate 는 참일 확률 하나, rating · choice 는 ``options`` 순서의 분포를 받는다.
    값 집합 · 범위 밖 출력은 ``DEC_004`` (영구) — 쓰임이 unavailable 로 읽는다.
    """
    if question.kind is QuestionKind.PREDICATE:
        p = _probability(answer)
        return Decision(
            value=p > PREDICATE_TRUE_ABOVE,
            probability=p,
            band=band_for(p, question.bands),
        )
    probs = _distribution(question, answer)
    distribution = dict(zip(question.options, probs, strict=True))
    if question.kind is QuestionKind.RATING:
        if question.act_level is None:  # Question 검증이 막지만 타입 좁히기용
            raise ValueError("rating needs act_level")
        probability = sum(probs[question.options.index(question.act_level) :])
        cumulative = 0.0
        median = len(probs) - 1  # 부동소수 오차로 끝까지 못 닿아도 마지막 등급
        for i, p in enumerate(probs):
            cumulative += p
            if cumulative >= _MEDIAN:
                median = i
                break
        return Decision(
            value=question.options[median],
            probability=min(probability, 1.0),
            distribution=distribution,
            band=band_for(probability, question.bands),
        )
    top = max(range(len(probs)), key=probs.__getitem__)  # 동률은 먼저 선언된 보기
    band = band_for(probs[top], question.bands)
    # 반대 보기가 없으므로 choice 의 reject 는 "어느 보기도 믿을 수 없다" — uncertain 이다
    if band is Band.REJECT:
        band = Band.UNCERTAIN
    return Decision(
        value=question.options[top],
        probability=probs[top],
        distribution=distribution,
        band=band,
    )
