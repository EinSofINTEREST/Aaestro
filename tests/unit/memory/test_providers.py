"""Embedding provider binding tests.

memoryset 이 provider 를 선언하는데(`embedding.provider`) 그것을 읽는 코드가
없어 `HashEmbedder` 가 기본값으로 박혀 있었다 (#159).

**06 은 실 embedding API 호출을 금지한다** — 여기서는 대역 endpoint 로
실제 HTTP 경로·직렬화·차원 검증을 태운다.
"""

from __future__ import annotations

import json

import httpx
import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.memory.providers import (
    BASE_URL_ENV,
    OpenAICompatibleEmbedder,
    build_embedder,
    build_index_registry,
)
from aaestro.modules.memoryset import EmbeddingSpec

DIMENSIONS = 8


def endpoint(*, dimensions: int = DIMENSIONS, status: int = 200, body=None):
    """대역 embeddings endpoint — 요청 하나당 벡터 하나."""

    def handler(request: httpx.Request) -> httpx.Response:
        if status >= httpx.codes.BAD_REQUEST:
            return httpx.Response(status, json={"error": "nope"})
        if body is not None:
            return httpx.Response(200, json=body)
        payload = json.loads(request.content)
        rows = [{"embedding": [0.5] * dimensions} for _ in payload["input"]]
        return httpx.Response(200, json={"data": rows})

    return httpx.Client(transport=httpx.MockTransport(handler))


def embedder(**overrides) -> OpenAICompatibleEmbedder:
    options = {
        "model": "text-embedding-3-small",
        "dimensions": DIMENSIONS,
        "base_url": "http://embeddings.test",
        "client": endpoint(),
    }
    return OpenAICompatibleEmbedder(**{**options, **overrides})


# --- 정상 경로 ------------------------------------------------------------------


def test_a_batch_returns_one_vector_per_text():
    vectors = embedder().embed(["first", "second", "third"])

    assert len(vectors) == 3
    assert all(len(vector) == DIMENSIONS for vector in vectors)


def test_an_empty_batch_does_not_call_the_endpoint():
    """빈 요청을 보내면 provider 마다 다르게 실패한다 — 아예 부르지 않는다."""

    def explode(_request):  # pragma: no cover - 호출되면 안 된다
        raise AssertionError("endpoint was called for an empty batch")

    built = embedder(client=httpx.Client(transport=httpx.MockTransport(explode)))

    assert built.embed([]) == ()


def test_the_declared_model_reaches_the_request():
    """모델은 memoryset 버전에 묶인다 — 다른 모델로 색인하면 공간이 갈라진다."""
    seen: dict = {}

    def capture(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"embedding": [0.5] * DIMENSIONS}]})

    embedder(client=httpx.Client(transport=httpx.MockTransport(capture))).embed(["x"])

    assert seen["model"] == "text-embedding-3-small"


# --- 실패 경로 ------------------------------------------------------------------


def test_wrong_dimensions_are_refused():
    """차원이 어긋난 벡터를 섞으면 같은 인덱스 안에 두 공간이 생긴다 (09)."""
    built = embedder(client=endpoint(dimensions=DIMENSIONS + 4))

    with pytest.raises(AaestroError) as exc_info:
        built.embed(["x"])

    assert exc_info.value.code == ErrorCode.MEM_003
    assert exc_info.value.details["declared"] == str(DIMENSIONS)


def test_an_endpoint_error_becomes_mem_003():
    built = embedder(client=endpoint(status=500))

    with pytest.raises(AaestroError) as exc_info:
        built.embed(["x"])

    assert exc_info.value.code == ErrorCode.MEM_003


def test_an_unreachable_endpoint_becomes_mem_003():
    built = embedder(base_url="http://127.0.0.1:1", client=httpx, timeout_s=0.2)

    with pytest.raises(AaestroError) as exc_info:
        built.embed(["x"])

    assert exc_info.value.code == ErrorCode.MEM_003


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"data": "not a list"},
        {"data": [{}]},
        {"data": [{"embedding": 7}]},
        {"data": [{"embedding": ["qqnot-a-number"] * DIMENSIONS}]},
        {"data": []},
        {"data": [{"embedding": [0.5] * DIMENSIONS}] * 2},
    ],
)
def test_an_uninterpretable_response_becomes_mem_003(body):
    """조용히 빈 벡터로 처리하면 그 기억은 검색에서 영원히 사라진다."""
    built = embedder(client=endpoint(body=body))

    with pytest.raises(AaestroError) as exc_info:
        built.embed(["x"])

    assert exc_info.value.code == ErrorCode.MEM_003


def capture_warnings(monkeypatch) -> list[tuple[str, dict]]:
    """providers 의 WARN 로그를 모은다 — 전역 structlog 설정이 바뀌므로 직접 가로챈다."""
    from aaestro.memory import providers as providers_module

    warned: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        providers_module.log, "warning", lambda event, **fields: warned.append((event, fields))
    )
    return warned


# --- 선언에서 고르기 -------------------------------------------------------------


def test_a_declared_provider_is_bound(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV, "http://embeddings.test")
    spec = EmbeddingSpec(provider="openai-compatible", model="m", dimensions=DIMENSIONS)

    assert isinstance(build_embedder(spec), OpenAICompatibleEmbedder)


def test_an_unbound_provider_is_refused(monkeypatch):
    """조용히 대역으로 떨어지면 운영에서 의미 없는 벡터로 검색하게 된다."""
    monkeypatch.setenv(BASE_URL_ENV, "http://embeddings.test")
    spec = EmbeddingSpec(provider="some-other-vendor", model="m", dimensions=DIMENSIONS)

    with pytest.raises(AaestroError) as exc_info:
        build_embedder(spec)

    assert exc_info.value.code == ErrorCode.MEM_003


def test_without_an_endpoint_the_offline_default_stays(monkeypatch):
    """주소 없이 실패시키면 endpoint 없이 돌던 개발/테스트가 전부 막힌다."""
    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    spec = EmbeddingSpec(provider="openai-compatible", model="m", dimensions=DIMENSIONS)

    warned = capture_warnings(monkeypatch)
    assert build_embedder(spec) is None

    # 선언은 있는데 해시 벡터로 도는 것은 운영에서 조용한 결함이다 — 한 번 경고한다
    assert [event for event, _ in warned] == ["semantic search is running on a hash embedder"]
    assert warned[0][1]["provider"] == "openai-compatible"


def test_a_bound_endpoint_does_not_warn(monkeypatch):
    monkeypatch.setenv(BASE_URL_ENV, "http://embeddings.test")
    spec = EmbeddingSpec(provider="openai-compatible", model="m", dimensions=DIMENSIONS)
    warned = capture_warnings(monkeypatch)

    build_embedder(spec)

    assert warned == []


def test_no_declaration_keeps_the_default(monkeypatch):
    """아무것도 선언하지 않은 것은 명시적 선택이다 — 경고하지 않는다."""
    warned = capture_warnings(monkeypatch)

    assert build_embedder(None) is None
    assert warned == []


# --- 레지스트리 조립 -------------------------------------------------------------


def test_the_registry_uses_the_declared_embedder(monkeypatch):
    """embedder 를 고르는 자리가 흩어지면 한 space 만 다른 공간을 쓰게 된다."""
    from aaestro.memory.embedding import HashEmbedder

    monkeypatch.setenv(BASE_URL_ENV, "http://embeddings.test")
    spec = EmbeddingSpec(provider="openai-compatible", model="m", dimensions=DIMENSIONS)

    built = build_index_registry(spec)

    assert isinstance(built.embedder, OpenAICompatibleEmbedder)
    assert not isinstance(built.embedder, HashEmbedder)


def test_the_registry_falls_back_offline(monkeypatch):
    from aaestro.memory.embedding import HashEmbedder

    monkeypatch.delenv(BASE_URL_ENV, raising=False)
    spec = EmbeddingSpec(provider="openai-compatible", model="m", dimensions=DIMENSIONS)

    assert isinstance(build_index_registry(spec).embedder, HashEmbedder)


def raw_endpoint(content: bytes) -> httpx.Client:
    """JSON 이 아닌 본문으로 답하는 대역 — 프록시 오류 페이지, 잘린 응답."""
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content))
    )


@pytest.mark.parametrize("content", [b"<html>qqproxy error</html>", b'{"data": [', b"\xff\xfe"])
def test_a_body_that_is_not_json_becomes_mem_003_without_echoing_it(content):
    built = embedder(client=raw_endpoint(content))

    with pytest.raises(AaestroError) as exc_info:
        built.embed(["x"])

    err = exc_info.value
    assert err.code == ErrorCode.MEM_003
    assert isinstance(err.__cause__, ValueError), "원인 체인을 보존한다"
    assert "qqproxy" not in str(err) + str(err.details)
