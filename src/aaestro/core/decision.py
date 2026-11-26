"""Decision model contract — typed questions, calibrated answers, probability bands.

결정 모델 계약 (01 Decision Models). 시스템은 provider 가 아니라 ``DecisionModel`` 에
의존한다. provider 장애 (없음 · 실패 · 시한 초과) 는 ``AaestroError`` (``DEC_*`` / ``TO_004``)
로 던지고, 쓰임 (회상 필터 등) 이 그것을 ``unavailable`` 로 읽어 원래 경로로 간다 —
계약에 별도 결과 타입을 두지 않는다 (boundary 변환 규칙, 05 Layer Rules).
"""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# provider 는 보기가 많을수록 정확도가 떨어진다 (01 Decision Models 6, 04 Decisionset Rules 3)
MAX_OPTIONS = 10
DEFAULT_ACT_AT = 0.8
DEFAULT_REJECT_AT = 0.2
LOCALE_PATTERN = r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$"
"""질문 문구의 언어 태그 (``en`` · ``ko`` · ``pt-BR``).

decisionset ``locale`` 과 provider ``locales`` 가 같은 규칙을 쓴다. 대소문자는 정규화하지
않는다 — 두 선언이 같은 표기로 적어야 대조가 맞는다."""


class Band(StrEnum):
    """Probability band a decision falls in.

    확률을 선언된 구간으로 읽은 결과. ``uncertain`` 은 원래 경로로 간다.
    """

    ACT = "act"
    UNCERTAIN = "uncertain"
    REJECT = "reject"


class QuestionKind(StrEnum):
    """The three question kinds; the value set is closed by the declaration."""

    PREDICATE = "predicate"
    RATING = "rating"
    CHOICE = "choice"


class Bands(BaseModel):
    """Thresholds that turn a probability into a ``Band``.

    구간 경계 — 라벨 데이터로 정하는 선언이다 (06 Calibration).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    act_at: float = Field(default=DEFAULT_ACT_AT, ge=0.0, le=1.0)
    reject_at: float = Field(default=DEFAULT_REJECT_AT, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.reject_at >= self.act_at:
            raise ValueError("reject_at must be below act_at")
        return self


class Question(BaseModel):
    """A typed question: ``predicate``, ``rating`` (ordered levels) or ``choice``.

    ``options`` 는 rating 의 등급 (서열 순서) 또는 choice 의 보기이고 predicate 에는 없다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    kind: QuestionKind
    ask: str = Field(min_length=1)
    options: tuple[str, ...] = ()
    act_level: str | None = None  # rating 전용 — 이 등급 이상의 누적 확률로 구간을 읽는다
    bands: Bands = Field(default_factory=Bands)

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.kind is QuestionKind.PREDICATE:
            if self.options or self.act_level is not None:
                raise ValueError("predicate takes no options or act_level")
            return self
        if not 2 <= len(self.options) <= MAX_OPTIONS:
            raise ValueError(f"{self.kind} needs 2..{MAX_OPTIONS} options")
        if len(set(self.options)) != len(self.options):
            raise ValueError("options must be unique")
        if self.kind is QuestionKind.RATING:
            if self.act_level not in self.options:
                raise ValueError("rating needs act_level among its options")
        elif self.act_level is not None:
            raise ValueError("act_level is rating only")
        return self


class Decision(BaseModel):
    """A decided value with its calibrated probability and band.

    ``probability`` 는 구간을 읽은 확률이다 — predicate 는 참일 확률, rating 은 ``act_level``
    이상의 누적 확률, choice 는 1위 보기의 확률. ``value`` 는 predicate 는 참일 확률이
    ``aaestro.decision.bands.PREDICATE_TRUE_ABOVE`` 를 넘는지, rating 은 누적 분포의 중앙값 등급
    (아래에서부터 누적이 0.5 이상이 되는 첫 등급), choice 는 1위 보기 (동률이면 먼저 선언된 보기).
    ``value`` 는 ``band`` 와 독립이다 — 구간은 라벨 데이터로 정한 선언이라 둘이 엇갈릴 수 있다
    (예: rating 에서 ``act_at <= 0.5``). 분기 · 행동은 ``band`` 로 읽는다.
    토큰은 provider 가 usage 를 줄 때만 싣는다 (시한을 넘긴 응답도 집계한다).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    value: bool | str
    probability: float = Field(ge=0.0, le=1.0)
    distribution: dict[str, float] | None = None  # rating · choice 의 분포
    band: Band
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)


class DecisionModel(Protocol):
    """Contract every decision provider satisfies.

    ``questions`` 와 같은 순서로 ``Decision`` 을 돌려준다. 호출 실패는 ``AaestroError`` 로 던진다.
    """

    async def decide(self, state: str, questions: Sequence[Question]) -> list[Decision]: ...
