"""Memoryset schema and loader.

메모리 space 정책(scope, 인덱스, 보존, recall) 선언 모듈. 정책만 담고
권한(누가 접근하는가)은 부착 위치가 결정한다.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import SemVer
from aaestro.modules.registry import ModuleRegistry, reject_module


class MemoryScope(StrEnum):
    """메모리 스코프 — 부착 위치와 반드시 일치해야 한다."""

    RUN = "run"
    LOCAL = "local"
    GROUP = "group"
    GLOBAL = "global"


class MemoryKind(StrEnum):
    """메모리 항목 종류 — 자유 문자열 금지 (검색 필터 일관성)."""

    OBSERVATION = "observation"
    FACT = "fact"
    SUMMARY = "summary"
    ARTIFACT_REF = "artifact_ref"
    MESSAGE = "message"


class EmbeddingSpec(BaseModel):
    """Embedding model pinning.

    임베딩 모델 고정 — 변경은 version bump + 전체 재인덱싱을 수반한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: str
    model: str
    dimensions: int = Field(gt=0)


class ChunkSpec(BaseModel):
    """Chunking policy for long content."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_tokens: int = Field(default=400, gt=0)
    overlap_tokens: int = Field(default=40, ge=0)

    @model_validator(mode="after")
    def _overlap_within_chunk(self) -> ChunkSpec:
        """오버랩이 청크보다 크면 분할이 진행되지 않는다."""
        if self.overlap_tokens >= self.max_tokens:
            raise ValueError("overlap_tokens must be smaller than max_tokens")
        return self


class HybridSpec(BaseModel):
    """Hybrid search merge weights (RRF)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    vector_weight: float = Field(default=0.6, ge=0.0)
    lexical_weight: float = Field(default=0.4, ge=0.0)

    @model_validator(mode="after")
    def _at_least_one_positive(self) -> HybridSpec:
        """두 가중치가 모두 0이면 검색 결과가 사라진다."""
        if self.vector_weight <= 0 and self.lexical_weight <= 0:
            raise ValueError("at least one of vector_weight/lexical_weight must be positive")
        return self


class IndexSpec(BaseModel):
    """Per-space index configuration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    embedding: EmbeddingSpec
    chunk: ChunkSpec = Field(default_factory=ChunkSpec)
    hybrid: HybridSpec = Field(default_factory=HybridSpec)


class CompactionSpec(BaseModel):
    """Compaction policy — raw entries collapse into summaries."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    trigger_entries: int = Field(gt=0)
    strategy: Literal["summarize"] = "summarize"
    keep_kinds: tuple[MemoryKind, ...] = (MemoryKind.FACT, MemoryKind.SUMMARY)


class RetentionSpec(BaseModel):
    """Retention policy.

    보존 정책 — 영구 스코프는 ttl 또는 compaction 중 하나 이상을 선언해야 한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    ttl_days: int | None = Field(default=None, gt=0)
    compaction: CompactionSpec | None = None

    @property
    def is_declared(self) -> bool:
        """보존 정책이 실제로 선언되었는지."""
        return self.ttl_days is not None or self.compaction is not None


class RecallSpec(BaseModel):
    """Auto-recall defaults injected into prompts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    auto: bool = True
    k: int = Field(default=6, gt=0)
    min_score: float = Field(default=0.35, ge=0.0, le=1.0)
    budget_tokens: int = Field(default=2000, gt=0)


class MemorysetSpec(BaseModel):
    """Memoryset body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scope: MemoryScope
    index: IndexSpec
    retention: RetentionSpec = Field(default_factory=RetentionSpec)
    recall: RecallSpec = Field(default_factory=RecallSpec)

    @model_validator(mode="after")
    def _persistent_scopes_declare_retention(self) -> MemorysetSpec:
        """영구 스코프는 보존 정책 필수 — 무한 성장 방지 (09 Compaction 5)."""
        persistent = {MemoryScope.LOCAL, MemoryScope.GROUP, MemoryScope.GLOBAL}
        if self.scope in persistent and not self.retention.is_declared:
            raise ValueError(
                f"scope '{self.scope}' requires a retention policy (ttl_days or compaction)"
            )
        return self


class MemorysetMetadata(BaseModel):
    """Memoryset metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: SemVer
    description: str | None = None


class MemorysetManifest(BaseModel):
    """``memoryset.yaml`` document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Memoryset"]
    metadata: MemorysetMetadata
    spec: MemorysetSpec


class LoadedMemoryset(BaseModel):
    """A loaded memoryset policy."""

    model_config = ConfigDict(frozen=True)

    ref: str
    manifest: MemorysetManifest

    @property
    def scope(self) -> MemoryScope:
        """선언된 스코프."""
        return self.manifest.spec.scope

    @property
    def declares_compaction(self) -> bool:
        """compaction 선언 여부 — service 그래프의 run scope 에서 필수."""
        return self.manifest.spec.retention.compaction is not None


class MemorysetLoader:
    """Loads memorysets through the registry."""

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    def load(self, ref: str) -> LoadedMemoryset:
        """Load a memoryset policy.

        메모리셋 정책을 로드합니다.

        Args:
            ref: Memoryset reference (``memorysets/{name}@{version}``).

        Returns:
            The loaded memoryset.

        Raises:
            AaestroError: MODULE/``MOD_001`` if unresolved, ``MOD_003`` if the
                declaration fails schema validation.
        """
        _, document = self._registry.load_document(ref)
        try:
            manifest = MemorysetManifest.model_validate(document)
        except ValidationError as err:
            reject_module(ref, err)
        return LoadedMemoryset(ref=ref, manifest=manifest)


def check_attachment_scope(memoryset: LoadedMemoryset, attachment_scope: MemoryScope) -> None:
    """Verify that a memoryset is attached at its declared scope.

    부착 위치와 memoryset 의 ``spec.scope`` 일치를 검증합니다
    (04-module-system.md Attachment 규칙 4).

    Raises:
        AaestroError: MODULE/``MOD_003`` on mismatch.
    """
    if memoryset.scope is not attachment_scope:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=(
                f"memoryset scope mismatch: declared '{memoryset.scope}', "
                f"attached at '{attachment_scope}'"
            ),
            details={"module_ref": memoryset.ref},
        )
