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


@pytest.mark.parametrize("body", [{}, {"data": "not a list"}, {"data": [{}]}])
def test_an_uninterpretable_response_becomes_mem_003(body):
    """조용히 빈 벡터로 처리하면 그 기억은 검색에서 영원히 사라진다."""
    built = embedder(client=endpoint(body=body))

    with pytest.raises(AaestroError) as exc_info:
        built.embed(["x"])

    assert exc_info.value.code == ErrorCode.MEM_003


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

    assert build_embedder(spec) is None


def test_no_declaration_keeps_the_default():
    assert build_embedder(None) is None


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
