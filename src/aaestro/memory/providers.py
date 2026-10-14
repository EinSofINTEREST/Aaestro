"""Embedding provider bindings.

``Embedder`` 계약 뒤에 실제 provider 를 붙인다. memoryset 이 provider 를
선언하는데(``embedding.provider``) 그것을 읽는 코드가 없어 ``HashEmbedder``
가 기본값으로 박혀 있었다 — 선언은 있는데 배선이 없던 자리다 (#159).

값이 대역에서 오더라도 **HTTP 경로 · 직렬화 · 차원 검증 · 에러 변환**이
실제로 실행된다. ``HashEmbedder`` 는 그 경로를 통째로 건너뛴다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import httpx
import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Sequence

    from aaestro.memory.embedding import Embedder
    from aaestro.modules.memoryset import EmbeddingSpec

OPENAI_COMPATIBLE = "openai-compatible"
"""memoryset 이 선언하는 provider 이름 — 09 의 예시가 곧 계약이다."""

BASE_URL_ENV = "AAESTRO_EMBEDDING_BASE_URL"
API_KEY_ENV = "AAESTRO_EMBEDDING_API_KEY"  # noqa: S105 — 키 이름이지 값이 아니다

DEFAULT_TIMEOUT_S = 30.0

log = structlog.get_logger(__name__)


def embedding_error(code: ErrorCode, message: str, **details: str) -> AaestroError:
    """임베딩 실패를 MEMORY 카테고리로 — 인덱싱 재시도 판단이 여기 걸린다."""
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=code,
        message=message,
        details=details,
    )


@dataclass
class OpenAICompatibleEmbedder:
    """Embeds through an OpenAI-compatible ``/v1/embeddings`` endpoint.

    OpenAI 호환 endpoint 로 임베딩합니다.

    Attributes:
        model: 고정된 임베딩 모델 — memoryset 버전에 묶입니다 (09).
        dimensions: 선언된 차원. 응답이 다르면 실패시킵니다.
        base_url: endpoint 주소.
    """

    model: str
    dimensions: int
    base_url: str
    api_key: str = ""
    timeout_s: float = DEFAULT_TIMEOUT_S
    client: Any = field(default=None, repr=False)

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """Embed a batch of texts.

        텍스트 묶음을 벡터로 변환합니다.

        Args:
            texts: The texts to embed.

        Returns:
            One vector per input, in the same order.

        Raises:
            AaestroError: MEMORY/``MEM_003`` if the endpoint is unreachable,
                returns an error, or answers with the wrong dimensions —
                차원이 어긋난 벡터를 인덱스에 섞으면 검색이 조용히 망가진다.
        """
        if not texts:
            return ()

        payload = {"model": self.model, "input": list(texts)}
        try:
            response = self._http().post(
                f"{self.base_url.rstrip('/')}/v1/embeddings",
                json=payload,
                headers=self._headers(),
                timeout=self.timeout_s,
            )
        except httpx.HTTPError as err:
            raise embedding_error(
                ErrorCode.MEM_003,
                "embedding endpoint is unreachable",
                model=self.model,
                cause=type(err).__name__,
            ) from err

        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise embedding_error(
                ErrorCode.MEM_003,
                "embedding endpoint returned an error",
                model=self.model,
                status=str(response.status_code),
            )

        return self._vectors(response, expected=len(texts))

    def _vectors(self, response: Any, *, expected: int) -> tuple[tuple[float, ...], ...]:
        """응답에서 벡터를 꺼내고 개수와 차원을 검증한다.

        외부 응답의 해석 실패는 전부 여기서 구조화 에러가 된다 — JSON 이 아닌 본문, 모양이 다른
        본문, 수가 아닌 값. 인덱스 · 검색 경로는 ``AaestroError`` 만 실패로 기록하므로 여기서 새는
        예외는 감사 · 메트릭을 건너뛴다. 본문은 싣지 않는다 (예외 종류 이름만).
        """
        try:
            rows = [
                tuple(float(value) for value in item["embedding"])
                for item in response.json()["data"]
            ]
        except (KeyError, TypeError, ValueError) as err:
            # ValueError 는 JSON 이 아닌 본문 (JSONDecodeError · UnicodeDecodeError) 과
            # 수로 읽히지 않는 값이다
            raise embedding_error(
                ErrorCode.MEM_003,
                "embedding response could not be interpreted",
                model=self.model,
                cause=type(err).__name__,
            ) from err

        if len(rows) != expected:
            # 개수가 어긋나면 인덱스의 zip(strict) · 검색의 언패킹이 ValueError 로 터진다
            raise embedding_error(
                ErrorCode.MEM_003,
                "embedding response does not answer every input",
                model=self.model,
                expected=str(expected),
                received=str(len(rows)),
            )
        for row in rows:
            if len(row) != self.dimensions:
                # 09 는 모델/차원 변경을 version bump + 전체 재인덱싱 대상으로
                # 규정한다 — 섞이면 같은 인덱스 안에 두 공간이 생긴다
                raise embedding_error(
                    ErrorCode.MEM_003,
                    "embedding dimensions do not match the declaration",
                    model=self.model,
                    declared=str(self.dimensions),
                    received=str(len(row)),
                )
        return tuple(rows)

    def _http(self) -> Any:
        """요청에 쓸 클라이언트 — 주입하지 않으면 모듈 기본을 쓴다."""
        return self.client or httpx

    def _headers(self) -> dict[str, str]:
        """인증 헤더 — 키가 없으면 붙이지 않는다 (대역은 무인증)."""
        return {"authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def build_embedder(spec: EmbeddingSpec | None) -> Embedder | None:
    """Select the embedder a memoryset declares.

    memoryset 이 선언한 embedder 를 고릅니다.

    Args:
        spec: The declared embedding policy, if any.

    Returns:
        The bound embedder, or ``None`` to keep the offline default —
        endpoint 가 주입되지 않은 환경에서도 동작해야 합니다 (06).

    Raises:
        AaestroError: MEMORY/``MEM_003`` if the provider is declared but this
            build has no binding for it — 조용히 대역으로 떨어지면 운영에서
            의미 없는 벡터로 검색하게 됩니다.
    """
    if spec is None:
        return None

    if spec.provider != OPENAI_COMPATIBLE:
        raise embedding_error(
            ErrorCode.MEM_003,
            "no embedding provider bound for this provider",
            provider=spec.provider,
        )

    base_url = os.environ.get(BASE_URL_ENV, "").strip()
    if not base_url:
        # 주소가 없으면 오프라인 기본값을 유지한다 — 실패시키면 endpoint 없이
        # 돌던 개발/테스트가 전부 막힌다. 다만 선언이 있는데 해시 벡터로 도는 것은
        # 운영에서 조용히 의미 검색이 망가진 상태이므로 눈에 띄게 남긴다
        log.warning(
            "semantic search is running on a hash embedder",
            provider=spec.provider,
            model=spec.model,
            reason=f"{BASE_URL_ENV} is not set",
        )
        return None

    return OpenAICompatibleEmbedder(
        model=spec.model,
        dimensions=spec.dimensions,
        base_url=base_url,
        api_key=os.environ.get(API_KEY_ENV, ""),
    )


def build_index_registry(spec: EmbeddingSpec | None, **kwargs: Any) -> Any:
    """Build an index registry using the declared embedder.

    선언된 embedder 로 인덱스 레지스트리를 만듭니다.

    embedder 를 고르는 **유일한 자리**입니다 — 호출부마다 따로 고르면 한 곳이
    빠져 그 space 만 조용히 다른 벡터 공간을 쓰게 됩니다.

    Args:
        spec: The declared embedding policy, if any.
        **kwargs: Passed through to ``IndexRegistry``.

    Returns:
        The registry, using the offline default when nothing is declared.
    """
    from aaestro.memory.index import IndexRegistry

    embedder = build_embedder(spec)
    if embedder is not None:
        kwargs["embedder"] = embedder
    return IndexRegistry(**kwargs)


__all__ = [
    "API_KEY_ENV",
    "BASE_URL_ENV",
    "OPENAI_COMPATIBLE",
    "OpenAICompatibleEmbedder",
    "build_embedder",
    "build_index_registry",
    "embedding_error",
]
