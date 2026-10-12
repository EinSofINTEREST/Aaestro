"""Embedding contracts.

임베딩 계약. 모델과 차원은 memoryset 버전에 고정된다 — 혼합된 임베딩 공간에서는
거리 비교가 의미를 잃으므로, 모델 교체는 version bump + 전체 재인덱싱을 수반한다.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence


@runtime_checkable
class Embedder(Protocol):
    """Turns text into a vector.

    텍스트를 벡터로 만드는 계약. 실제 provider 는 이 뒤에 감춰지고, 테스트는
    결정적 대역으로 대체한다 — 실 embedding API 호출 금지 (06 Testing).
    """

    @property
    def dimensions(self) -> int:
        """벡터 차원 — memoryset 선언과 일치해야 한다."""
        ...

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """텍스트 묶음을 벡터로 변환한다."""
        ...


@dataclass(frozen=True)
class HashEmbedder:
    """Deterministic embedder for tests and offline indexing.

    해시 기반 결정적 임베더. 같은 입력은 항상 같은 벡터를 준다 — 테스트가
    외부 API 나 비결정성에 의존하지 않게 한다.

    의미 유사도를 흉내내지는 않지만, 동일/유사 토큰을 공유하는 문장이
    가까워지도록 토큰 단위로 누적한다.
    """

    dimensions: int = 64

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """텍스트 묶음을 결정적 벡터로 변환한다."""
        return tuple(self._embed_one(text) for text in texts)

    def _embed_one(self, text: str) -> tuple[float, ...]:
        """토큰별 해시를 차원에 누적한 뒤 L2 정규화한다."""
        vector = [0.0] * self.dimensions
        for token in tokenize(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            # 부호를 함께 뽑아 서로 다른 토큰이 상쇄되도록 한다
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign
        return normalize(vector)


def tokenize(text: str) -> tuple[str, ...]:
    """Split text into lowercase word tokens.

    텍스트를 소문자 토큰으로 나눕니다. 식별자(``mcp__fs__read``, ``MCP_004``)가
    통째로 남도록 구분자를 보수적으로 잡습니다 — 잘게 쪼개면 lexical 검색이
    식별자를 놓칩니다.

    Args:
        text: The text to tokenize.

    Returns:
        Lowercase tokens in order of appearance.
    """
    tokens: list[str] = []
    current: list[str] = []
    for char in text:
        if char.isalnum() or char in {"_", "-", "."}:
            current.append(char)
        elif current:
            tokens.append("".join(current).lower())
            current = []
    if current:
        tokens.append("".join(current).lower())
    return tuple(tokens)


def normalize(vector: Sequence[float]) -> tuple[float, ...]:
    """Scale a vector to unit length.

    벡터를 단위 길이로 만듭니다 — 코사인 유사도를 내적으로 계산하기 위해서입니다.
    영벡터는 그대로 돌려줍니다 (0으로 나누지 않습니다).
    """
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return tuple(vector)
    return tuple(value / magnitude for value in vector)


def cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity between two vectors.

    두 벡터의 코사인 유사도.

    입력이 단위벡터가 아니어도 올바른 값을 돌려주도록 크기로 나눕니다 —
    공개 유틸이라 호출자가 정규화 여부를 알기 어렵고, 내적을 코사인이라 부르면
    정규화되지 않은 벡터에서 점수가 조용히 부풀려집니다.

    길이가 다르면 비교가 성립하지 않으므로 0을 돌려줍니다 — 혼합된 임베딩
    공간을 조용히 비교하지 않기 위해서입니다. 영벡터도 0입니다.
    """
    if len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    scale = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if scale == 0:
        return 0.0
    return dot / scale


__all__ = ["Embedder", "HashEmbedder", "cosine", "normalize", "tokenize"]
