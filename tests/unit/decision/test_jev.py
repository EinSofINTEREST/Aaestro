import json
import traceback

import httpx
import pytest

from aaestro.core.decision import Band, Question, QuestionKind
from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.decision.providers.jev import MAX_RESPONSE_BYTES, JevDecisionModel
from aaestro.decision.providers.registry import adapter_for

PRED = Question(name="relevant", kind=QuestionKind.PREDICATE, ask="This memory helps")
RATING = Question(name="r", kind=QuestionKind.RATING, ask="x", options=("lo", "hi"), act_level="hi")


def _model(handler, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    args = {"base_url": "https://api.typesafe.ai", "model": "jev-1", "credential": "k"}
    return JevDecisionModel(**(args | kwargs), timeout_s=5, client=client)


def _answers(**probs):
    return httpx.Response(
        200,
        json={
            "answers": {k: {"noul": p} for k, p in probs.items()},
            "usage": {"input_tokens": 7, "output_tokens": 1},
        },
    )


async def test_decide_sends_systemone_wire_and_reads_answers():
    seen = []

    def handler(request):
        seen.append(request)
        return _answers(q0=0.9, q1=0.1)

    decisions = await _model(handler).decide("task", [PRED, PRED])

    req = seen[0]
    assert str(req.url) == "https://api.typesafe.ai/v1/systemone"
    assert req.headers["authorization"] == "Bearer k"
    body = json.loads(req.content)
    assert body["state"] == "task"
    assert body["model"] == "jev-1"
    assert body["questions"]["q0"]["type"] == "noul"
    assert body["questions"]["q0"]["instructions"] == {"question": PRED.ask}
    assert [d.band for d in decisions] == [Band.ACT, Band.REJECT]
    assert (decisions[0].input_tokens, decisions[0].output_tokens) == (7, 1)
    assert decisions[1].input_tokens is None


async def test_decide_without_credential_sends_no_authorization():
    seen = []

    def handler(request):
        seen.append(request)
        return _answers(q0=0.5)

    await _model(handler, credential=None).decide("s", [PRED])
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize(
    ("status", "code", "category", "retryable"),
    [
        (429, "DEC_002", ErrorCategory.RATE_LIMIT, True),
        (503, "DEC_001", ErrorCategory.DECISION, True),
        (401, "DEC_001", ErrorCategory.DECISION, False),
        (302, "DEC_001", ErrorCategory.DECISION, False),
        (413, "DEC_003", ErrorCategory.DECISION, False),
    ],
)
async def test_decide_status_maps_to_typed_error(status, code, category, retryable):
    model = _model(lambda _: httpx.Response(status, headers={"location": "https://evil"}))
    with pytest.raises(AaestroError) as exc:
        await model.decide("s", [PRED])
    assert (exc.value.code, exc.value.category, exc.value.retryable) == (
        code,
        category,
        retryable,
    )


async def test_decide_timeout_raises_retryable_to_004():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AaestroError) as exc:
        await _model(handler).decide("s", [PRED])
    assert (exc.value.code, exc.value.category, exc.value.retryable) == (
        "TO_004",
        ErrorCategory.TIMEOUT,
        True,
    )


async def test_decide_transport_failure_raises_retryable_dec_001():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AaestroError) as exc:
        await _model(handler).decide("s", [PRED])
    assert (exc.value.code, exc.value.retryable) == ("DEC_001", True)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json=[1]),
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, json={"answers": {"q0": {"noul": 0.5}, "zz": {"noul": 0.5}}}),
        httpx.Response(200, json={"answers": {"q0": {"noul": 1.5}}}),
        httpx.Response(200, json={"answers": {"q0": {"noul": True}}}),
        httpx.Response(200, json={"answers": {"q0": {"noul": "0.5"}}}),
        httpx.Response(200, json={"answers": {"q0": 0.5}}),
        httpx.Response(200, content=b"[" * 100_000),
        httpx.Response(200, content=b" " * (MAX_RESPONSE_BYTES + 1)),
    ],
)
async def test_decide_out_of_contract_response_raises_dec_004(response):
    with pytest.raises(AaestroError) as exc:
        await _model(lambda _: response).decide("s", [PRED])
    assert (exc.value.code, exc.value.retryable) == ("DEC_004", False)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.parametrize(
    ("state", "questions"),
    [
        ("s", []),
        ("s", [PRED] * 201),
        ("x" * 16_001, [PRED]),
        ("s", [RATING]),
    ],
)
async def test_decide_over_limits_raises_dec_003_without_http(state, questions):
    calls = []

    def handler(request):
        calls.append(request)
        return _answers(q0=0.5)

    with pytest.raises(AaestroError) as exc:
        await _model(handler).decide(state, questions)
    assert exc.value.code == "DEC_003"
    assert calls == []


@pytest.mark.parametrize(
    ("base_url", "kwargs"),
    [
        ("ftp://api.typesafe.ai", {}),
        ("http://api.typesafe.ai", {}),
        ("https://user:pw@api.typesafe.ai", {}),
        ("https://api.typesafe.ai?x=1", {}),
        ("https://api.typesafe.ai", {"credential": "has space"}),
    ],
)
def test_constructor_rejects_unsafe_settings_with_cfg_001(base_url, kwargs):
    with pytest.raises(AaestroError) as exc:
        JevDecisionModel(
            base_url=base_url, model="m", credential=kwargs.get("credential", "k"), timeout_s=1
        )
    assert exc.value.code == "CFG_001"
    assert "has space" not in str(exc.value)


@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8080", "http://fake-provider:8080"])
def test_constructor_accepts_opted_in_plaintext(base_url):
    JevDecisionModel(
        base_url=base_url, model="m", credential=None, timeout_s=1, allow_plaintext=True
    )


def test_constructor_loopback_plaintext_without_opt_in_raises_cfg_001():
    # 설정 (DecisionProviderConfig) 과 같은 규칙 — 루프백도 opt-in 이 필요하다 (#595)
    with pytest.raises(AaestroError) as exc:
        JevDecisionModel(base_url="http://127.0.0.1:8080", model="m", credential=None, timeout_s=1)
    assert exc.value.code == "CFG_001"


def test_adapter_for_known_and_unknown_kind():
    assert adapter_for("jev") is JevDecisionModel
    with pytest.raises(AaestroError) as exc:
        adapter_for("nope")
    assert exc.value.code == "CFG_001"


async def test_decide_undecodable_body_raises_dec_004():
    def handler(_):
        # stream 으로 줘야 압축 해제가 읽는 쪽 (어댑터) 에서 일어난다
        return httpx.Response(
            200, headers={"content-encoding": "gzip"}, stream=httpx.ByteStream(b"not gzip")
        )

    with pytest.raises(AaestroError) as exc:
        await _model(handler).decide("s", [PRED])
    assert (exc.value.code, exc.value.retryable) == ("DEC_004", False)
    assert exc.value.details["cause"] == "DecodingError"
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


@pytest.mark.parametrize(
    "base_url",
    [
        "https://[x-secret]",
        "https://example.invalid:bad-secret",
        "https://secret\x00.example",  # urlsplit 은 받고 httpx 만 거절한다
    ],
)
def test_constructor_rejects_unparsable_base_url_without_echo(base_url):
    with pytest.raises(AaestroError) as exc:
        JevDecisionModel(base_url=base_url, model="m", credential=None, timeout_s=1)
    assert exc.value.code == "CFG_001"
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
    assert "secret" not in "".join(traceback.format_exception(exc.value))
