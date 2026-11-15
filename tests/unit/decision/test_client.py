import asyncio
import random
import traceback

import httpx
import pytest
from structlog.testing import capture_logs

from aaestro.core.decision import Question, QuestionKind
from aaestro.core.errors import AaestroError, CircuitState
from aaestro.decision.client import DecisionClient
from aaestro.decision.providers.jev import JevDecisionModel
from aaestro.observability.metrics import Metrics

PRED = Question(name="triage.relevant", kind=QuestionKind.PREDICATE, ask="x")
RATING = Question(
    name="triage.r", kind=QuestionKind.RATING, ask="x", options=("lo", "hi"), act_level="hi"
)


async def _no_sleep(_: float) -> None:
    return None


class Wire:
    """MockTransport 위에서 응답을 순서대로 내고 HTTP 호출 수를 센다."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return self.responses[min(self.calls, len(self.responses)) - 1]


def _client(handler, *, timeout_s=5.0, sleep=_no_sleep):
    metrics = Metrics()
    provider = JevDecisionModel(
        base_url="https://api.typesafe.ai",
        model="jev-1",
        credential=None,
        timeout_s=timeout_s,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    client = DecisionClient(
        provider=provider,
        agent="planner",
        provider_kind="jev",
        model="jev-1",
        timeout_s=timeout_s,
        metrics=metrics,
        sleep=sleep,
        rng=random.Random(0),  # noqa: S311 — jitter 결정성용, 보안 용도 아님
    )
    return client, metrics


def _sample(metrics, name, **labels):
    return metrics.registry.get_sample_value(name, labels) or 0.0


def _unavailable(metrics, reason):
    return _sample(
        metrics,
        "aaestro_decision_unavailable_total",
        agent="planner",
        provider="jev",
        reason=reason,
    )


def _calls(metrics, status):
    return _sample(
        metrics,
        "aaestro_decision_calls_total",
        agent="planner",
        provider="jev",
        model="jev-1",
        use="recall_filter",
        status=status,
    )


async def _decide(client, questions=(PRED,)):
    return await client.decide("s", list(questions), task_id="t1", use="recall_filter")


async def test_decide_success_records_completed():
    wire = Wire(httpx.Response(200, json={"answers": {"q0": {"noul": 0.9}}}))
    client, metrics = _client(wire)

    decisions = await _decide(client)

    assert decisions[0].value is True
    assert _calls(metrics, "completed") == 1
    assert _unavailable(metrics, "error") == 0


async def test_decide_rate_limited_retries_once_then_unavailable_error():
    wire = Wire(httpx.Response(429))
    client, metrics = _client(wire)

    with pytest.raises(AaestroError) as exc:
        await _decide(client)

    assert exc.value.code == "DEC_002"
    assert wire.calls == 2
    assert _unavailable(metrics, "error") == 1
    assert _calls(metrics, "rate_limited") == 1


async def test_decide_rate_limited_then_answer_succeeds():
    wire = Wire(httpx.Response(429), httpx.Response(200, json={"answers": {"q0": {"noul": 0.1}}}))
    client, metrics = _client(wire)

    decisions = await _decide(client)

    assert decisions[0].value is False
    assert wire.calls == 2
    assert _unavailable(metrics, "error") == 0


async def test_decide_out_of_value_set_is_dec_004_without_retry():
    wire = Wire(httpx.Response(200, json={"answers": {"q0": {"noul": 2}}}))
    client, metrics = _client(wire)

    with pytest.raises(AaestroError) as exc:
        await _decide(client)

    assert exc.value.code == "DEC_004"
    assert wire.calls == 1
    assert _unavailable(metrics, "error") == 1


async def test_decide_over_budget_is_to_004_unavailable_timeout():
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client, metrics = _client(hang, timeout_s=0.05)

    with pytest.raises(AaestroError) as exc:
        await asyncio.wait_for(_decide(client), 5)

    assert (exc.value.code, exc.value.retryable) == ("TO_004", False)
    assert _unavailable(metrics, "timeout") == 1
    assert _calls(metrics, "failed") == 1


async def test_decide_circuit_open_makes_no_http_call():
    wire = Wire(httpx.Response(200, json={"answers": {}}))
    client, metrics = _client(wire)
    for _ in range(client.breaker.max_failures):
        with pytest.raises(AaestroError):
            await _decide(client)
    assert client.breaker.state is CircuitState.OPEN
    before = wire.calls

    with pytest.raises(AaestroError) as exc:
        await _decide(client)

    assert exc.value.code == "DEC_001"
    assert wire.calls == before
    assert _unavailable(metrics, "circuit_open") == 1
    assert _sample(metrics, "aaestro_circuit_state", target="decision:planner") == 1.0


async def test_decide_request_over_limits_does_not_trip_circuit():
    wire = Wire(httpx.Response(200, json={"answers": {"q0": {"noul": 0.9}}}))
    client, _ = _client(wire)
    for _ in range(client.breaker.max_failures + 1):
        with pytest.raises(AaestroError) as exc:
            await _decide(client, questions=(RATING,))
        assert exc.value.code == "DEC_003"

    assert client.breaker.state is CircuitState.CLOSED
    assert wire.calls == 0


async def test_decide_retry_wait_counts_against_the_whole_budget():
    # 첫 시도는 즉시 retryable 실패, 재시도 대기 (0.5s × jitter ≥ 0.375s) 가 실제로 흘러
    # 예산 0.3s 를 넘는다 — 시도마다 timeout 을 주는 배선이면 두 번째 429 로 DEC_002 가 난다
    wire = Wire(httpx.Response(429), httpx.Response(200, json={"answers": {"q0": {"noul": 0.9}}}))
    client, metrics = _client(wire, timeout_s=0.3, sleep=asyncio.sleep)

    with pytest.raises(AaestroError) as exc:
        await asyncio.wait_for(_decide(client), 5)

    assert exc.value.code == "TO_004"
    assert wire.calls == 1
    assert _unavailable(metrics, "timeout") == 1


def _rendered(err: BaseException) -> str:
    return "".join(traceback.format_exception(err))


async def test_decide_transport_error_bytes_never_reach_logs_or_error():
    marker = "LEAKED-STATUS-LINE-7f3a"

    def handler(request):
        # httpcore 가 깨진 상태 줄에서 내는 것과 같은 모양 — 메시지에 받은 바이트가 실린다
        raise httpx.RemoteProtocolError(f"illegal status line: b'HTTP/1.1 {marker}'")

    client, _ = _client(handler)

    with capture_logs() as logs, pytest.raises(AaestroError) as exc:
        await _decide(client)

    assert exc.value.code == "DEC_001"
    assert exc.value.details["cause"] == "RemoteProtocolError"
    assert marker not in _rendered(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert len(logs) >= 2  # 재시도 WARN + unavailable WARN
    for entry in logs:
        text = repr(entry)
        if isinstance(entry.get("exc_info"), BaseException):
            text += _rendered(entry["exc_info"])
        assert marker not in text
