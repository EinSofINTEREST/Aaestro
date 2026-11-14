import pytest
from pydantic import ValidationError

from aaestro.core.decision import Band, Bands, DecisionModel, Question, QuestionKind
from aaestro.core.errors import DECISION_RETRY, AaestroError, ErrorCategory
from aaestro.decision.bands import band_for, decision_from
from tests.fixtures.fake_decision import FakeDecisionModel

PRED = Question(name="p", kind=QuestionKind.PREDICATE, ask="x")
RATING = Question(
    name="r",
    kind=QuestionKind.RATING,
    ask="x",
    options=("unusable", "weak", "acceptable", "strong"),
    act_level="acceptable",
)
CHOICE = Question(name="c", kind=QuestionKind.CHOICE, ask="x", options=("bug", "feature", "doc"))


@pytest.mark.parametrize(
    ("p", "band"),
    [(0.8, Band.ACT), (0.79, Band.UNCERTAIN), (0.21, Band.UNCERTAIN), (0.2, Band.REJECT)],
)
def test_band_for_boundaries_inclusive(p, band):
    assert band_for(p, Bands()) is band


def test_decision_from_rating_uses_cumulative_at_act_level():
    d = decision_from(RATING, [0.05, 0.05, 0.4, 0.5])
    assert d.probability == pytest.approx(0.9)
    assert d.band is Band.ACT


def test_decision_from_rating_below_act_level_rejects():
    d = decision_from(RATING, [0.7, 0.2, 0.05, 0.05])
    assert d.band is Band.REJECT


def test_decision_from_choice_uses_top_probability():
    d = decision_from(CHOICE, [0.85, 0.1, 0.05])
    assert (d.value, d.band) == ("bug", Band.ACT)


def test_decision_from_choice_top_at_reject_at_is_uncertain():
    # 3 보기라 1위가 reject_at 이하이려면 reject_at 을 올려야 한다
    q = CHOICE.model_copy(update={"bands": Bands(act_at=0.9, reject_at=0.4)})
    d = decision_from(q, [0.34, 0.33, 0.33])
    assert d.band is Band.UNCERTAIN


@pytest.mark.parametrize(
    ("p", "band", "value"),
    [
        (1.0, Band.ACT, True),
        (0.8, Band.ACT, True),
        (0.5, Band.UNCERTAIN, False),
        (0.2, Band.REJECT, False),
        (0.0, Band.REJECT, False),
    ],
)
def test_decision_from_predicate_boundaries(p, band, value):
    d = decision_from(PRED, p)
    assert (d.band, d.value) == (band, value)


@pytest.mark.parametrize(
    ("dist", "value", "band"),
    [
        ([0.4, 0.0, 0.6, 0.0], "acceptable", Band.UNCERTAIN),  # act 확률 0.6, 중앙값도 act_level
        ([0.0, 0.55, 0.0, 0.45], "weak", Band.UNCERTAIN),
        ([0.05, 0.05, 0.4, 0.5], "acceptable", Band.ACT),
        ([0.7, 0.2, 0.05, 0.05], "unusable", Band.REJECT),
    ],
)
def test_decision_from_rating_value_is_cumulative_median(dist, value, band):
    d = decision_from(RATING, dist)
    assert (d.value, d.band) == (value, band)


def test_decision_from_choice_tie_picks_first_option():
    assert decision_from(CHOICE, [0.4, 0.4, 0.2]).value == "bug"


@pytest.mark.parametrize(
    "answer",
    [
        [0.5, 0.4, 0.15, -0.05],
        [-0.5, 0.0, 0.5, 1.0],
        [1.2, -0.1, -0.1],
        [float("nan"), 0.5, 0.5],
        [float("inf"), 0.0, 0.0],
        [True, False, False],
        "abc",
        0.5,
        [0.5, 0.5],
        [0.5, 0.2, 0.28],
    ],
)
def test_decision_from_bad_distribution_is_dec_004(answer):
    q = RATING if isinstance(answer, list) and len(answer) == 4 else CHOICE
    with pytest.raises(AaestroError) as exc:
        decision_from(q, answer)
    assert (exc.value.category, exc.value.code, exc.value.retryable) == (
        ErrorCategory.DECISION,
        "DEC_004",
        False,
    )


@pytest.mark.parametrize("answer", [-0.1, 1.1, float("nan"), float("inf"), True, "0.5", None])
def test_decision_from_bad_predicate_is_dec_004(answer):
    with pytest.raises(AaestroError) as exc:
        decision_from(PRED, answer)
    assert exc.value.code == "DEC_004"


def test_decision_from_rounded_sum_is_normalized():
    d = decision_from(CHOICE, [0.5, 0.3, 0.195])  # 0.995
    assert sum(d.distribution.values()) == pytest.approx(1.0)  # type: ignore[union-attr]


def test_question_rejects_act_level_outside_options():
    with pytest.raises(ValidationError):
        Question(name="r", kind="rating", ask="x", options=("a", "b"), act_level="z")


def test_question_rejects_eleven_options():
    with pytest.raises(ValidationError):
        Question(name="c", kind="choice", ask="x", options=tuple(str(i) for i in range(11)))


def test_bands_reject_unordered():
    with pytest.raises(ValidationError):
        Bands(act_at=0.3, reject_at=0.5)


def test_decision_retry_policy():
    assert DECISION_RETRY.max_attempts == 2
    assert set(DECISION_RETRY.retryable_categories) == {
        ErrorCategory.NETWORK,
        ErrorCategory.TIMEOUT,
        ErrorCategory.RATE_LIMIT,
    }


async def test_fake_records_asks_and_reads_bands():
    model: DecisionModel = FakeDecisionModel({"p": 0.95})
    [d] = await model.decide("state", [PRED])
    assert d.band is Band.ACT
    assert model.asked == [("p", "state")]  # type: ignore[attr-defined]


async def test_fake_raises_scripted_error_as_unavailable():
    err = AaestroError(ErrorCategory.DECISION, "DEC_001", "down", retryable=True)
    with pytest.raises(AaestroError):
        await FakeDecisionModel({"p": err}).decide("s", [PRED])


def test_rating_value_is_independent_of_band():
    q = RATING.model_copy(update={"bands": Bands(act_at=0.5, reject_at=0.2)})
    d = decision_from(q, [0.25] * 4)  # act 확률 0.5 → act, 중앙값은 act_level 미만
    assert (d.value, d.band) == ("weak", Band.ACT)


@pytest.mark.parametrize("answer", [[0.5, 0.3, 0.19], [1.0, 0.005, 0.005]])
def test_decision_from_sum_tolerance_bound_is_inclusive(answer):
    assert sum(decision_from(CHOICE, answer).distribution.values()) == pytest.approx(1.0)  # type: ignore[union-attr]


@pytest.mark.parametrize("answer", [[0.5, 0.3, 0.189], [1.0, 0.0055, 0.0055]])
def test_decision_from_sum_just_outside_tolerance_is_dec_004(answer):
    with pytest.raises(AaestroError) as exc:
        decision_from(CHOICE, answer)
    assert exc.value.code == "DEC_004"
