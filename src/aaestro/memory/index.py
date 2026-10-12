"""Hybrid memory index — vector + lexical + metadata.

space 당 세 계층을 유지한다. 하이브리드가 기본인 이유는 한쪽만으로는 놓치기
때문이다 — 에러 코드나 함수명 같은 식별자는 lexical 이, 표현이 다른 같은 뜻은
vector 가 잡는다.

인덱스는 space 단위로 격리된다 — 검색이 경계를 넘으면 격리가 무너진다.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.embedding import HashEmbedder, cosine, tokenize
from aaestro.memory.telemetry import IndexTelemetry

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from aaestro.memory.embedding import Embedder
    from aaestro.memory.entry import MemoryEntry
    from aaestro.modules.memoryset import ChunkSpec, MemoryKind
    from aaestro.observability.metrics import Metrics

DEFAULT_MAX_INDEX_FAILURES = 3
"""연속 인덱싱 실패 상한 — 넘으면 숨기지 않고 MEM_003 으로 드러낸다."""

log = structlog.get_logger(__name__)


def index_error(code: ErrorCode, message: str, *, space: str, **details: object) -> AaestroError:
    """인덱스 실패를 구조화 에러로 만든다."""
    return AaestroError(
        category=ErrorCategory.MEMORY,
        code=code,
        message=message,
        details={"memory_space": space, **details},
    )


@dataclass(frozen=True)
class Chunk:
    """One indexed slice of an entry.

    항목의 색인 단위. 긴 content 는 여러 chunk 로 나뉘지만, 검색 결과는
    entry 단위로 합쳐진다 — 같은 기억이 여러 번 주입되면 예산만 낭비된다.
    """

    entry_id: str
    ordinal: int
    text: str
    vector: tuple[float, ...] = ()
    tokens: tuple[str, ...] = ()


def split_chunks(text: str, spec: ChunkSpec) -> tuple[str, ...]:
    """Split long content into overlapping chunks.

    긴 content 를 겹치는 chunk 로 나눕니다. 겹침이 있어야 경계에 걸친 문장이
    양쪽 어디서도 검색되지 않는 일을 막습니다.

    Args:
        text: The content to split.
        spec: Chunking policy from the memoryset.

    Returns:
        The chunks in order; a short text yields exactly one chunk.
    """
    tokens = text.split()
    if len(tokens) <= spec.max_tokens:
        return (text,)

    stride = spec.max_tokens - spec.overlap_tokens
    chunks = [
        " ".join(tokens[start : start + spec.max_tokens])
        for start in range(0, len(tokens), stride)
        if tokens[start : start + spec.max_tokens]
    ]
    return tuple(chunks)


@dataclass(frozen=True)
class Hit:
    """One index hit before merging.

    병합 전 단일 인덱스의 히트. ``rank`` 는 1부터 시작하는 순위로, RRF 병합이
    점수 대신 순위를 쓰기 때문에 함께 보관한다.
    """

    entry_id: str
    score: float
    rank: int


@dataclass
class SpaceIndex:
    """The three index layers for one space.

    space 하나의 세 계층 인덱스. 다른 space 의 항목은 절대 담기지 않는다.
    """

    space: str
    embedder: Embedder = field(default_factory=HashEmbedder)
    chunks: list[Chunk] = field(default_factory=list)
    metadata: dict[str, tuple[MemoryKind, tuple[str, ...]]] = field(default_factory=dict)
    """entry_id → (kind, tags) — 구조 필터용."""

    def add(self, entry: MemoryEntry, spec: ChunkSpec) -> None:
        """Index one entry across all three layers.

        항목 하나를 세 계층에 색인합니다.

        Args:
            entry: The entry to index — it must belong to this space.
            spec: Chunking policy.

        Raises:
            AaestroError: MEMORY/``MEM_004`` if the entry belongs elsewhere —
                인덱스가 space 경계를 넘으면 격리가 무너집니다.
        """
        if entry.space != self.space:
            raise index_error(
                ErrorCode.MEM_004,
                "entry does not belong to this space index",
                space=self.space,
                entry_space=entry.space,
            )

        texts = split_chunks(entry.content, spec)
        vectors = self.embedder.embed(texts)
        for ordinal, (text, vector) in enumerate(zip(texts, vectors, strict=True)):
            self.chunks.append(
                Chunk(
                    entry_id=entry.entry_id,
                    ordinal=ordinal,
                    text=text,
                    vector=vector,
                    tokens=tokenize(text),
                )
            )
        self.metadata[entry.entry_id] = (entry.kind, entry.tags)

    def remove(self, entry_id: str) -> None:
        """항목의 색인을 제거한다 — 재인덱싱/retention 경로에서 쓴다."""
        self.chunks = [c for c in self.chunks if c.entry_id != entry_id]
        self.metadata.pop(entry_id, None)

    @property
    def entry_ids(self) -> frozenset[str]:
        """색인된 항목 id."""
        return frozenset(self.metadata)

    def _candidates(
        self, kinds: Sequence[MemoryKind] | None, tags: Sequence[str] | None
    ) -> frozenset[str]:
        """메타데이터 필터를 통과한 항목 — 세 번째 계층."""
        allowed_kinds = set(kinds) if kinds else None
        required_tags = set(tags) if tags else None

        selected = set()
        for entry_id, (kind, entry_tags) in self.metadata.items():
            if allowed_kinds is not None and kind not in allowed_kinds:
                continue
            if required_tags is not None and not required_tags.issubset(entry_tags):
                continue
            selected.add(entry_id)
        return frozenset(selected)

    def search_vector(
        self,
        query: str,
        *,
        k: int,
        kinds: Sequence[MemoryKind] | None = None,
        tags: Sequence[str] | None = None,
    ) -> tuple[Hit, ...]:
        """Semantic search over the vector layer.

        의미 검색 — 표현이 달라도 같은 뜻을 잡습니다.

        Returns:
            Hits ranked best-first, deduplicated to one per entry.
        """
        candidates = self._candidates(kinds, tags)
        (query_vector,) = self.embedder.embed([query])

        best: dict[str, float] = {}
        for chunk in self.chunks:
            if chunk.entry_id not in candidates:
                continue
            score = cosine(query_vector, chunk.vector)
            # 같은 항목의 여러 chunk 중 가장 잘 맞는 것만 남긴다
            if score > best.get(chunk.entry_id, -1.0):
                best[chunk.entry_id] = score
        return _rank(best, k)

    def search_lexical(
        self,
        query: str,
        *,
        k: int,
        kinds: Sequence[MemoryKind] | None = None,
        tags: Sequence[str] | None = None,
    ) -> tuple[Hit, ...]:
        """Keyword search over the lexical layer.

        키워드 검색 — 에러 코드나 함수명 같은 식별자를 정확히 잡습니다.
        vector 만으로는 이런 매칭을 놓칩니다.

        Returns:
            Hits ranked best-first, deduplicated to one per entry.
        """
        candidates = self._candidates(kinds, tags)
        query_tokens = set(tokenize(query))
        if not query_tokens:
            return ()

        document_frequency: dict[str, int] = defaultdict(int)
        by_entry: dict[str, set[str]] = defaultdict(set)
        for chunk in self.chunks:
            if chunk.entry_id in candidates:
                by_entry[chunk.entry_id].update(chunk.tokens)
        for tokens in by_entry.values():
            for token in tokens & query_tokens:
                document_frequency[token] += 1

        total = len(by_entry) or 1
        scores: dict[str, float] = {}
        for entry_id, tokens in by_entry.items():
            matched = tokens & query_tokens
            if not matched:
                continue
            # 흔한 토큰의 가중치를 낮춘다 (idf) — 식별자처럼 희귀한 토큰이 이긴다
            scores[entry_id] = sum(
                math.log(1 + total / document_frequency[token]) for token in matched
            )
        return _rank(scores, k)


def _rank(scores: dict[str, float], k: int) -> tuple[Hit, ...]:
    """점수를 순위로 바꾼다 — 동점은 entry_id 로 안정 정렬한다."""
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    return tuple(
        Hit(entry_id=entry_id, score=score, rank=rank)
        for rank, (entry_id, score) in enumerate(ordered[:k], start=1)
        if score > 0
    )


@dataclass
class IndexQueue:
    """Asynchronous indexing queue.

    비동기 인덱싱 큐. append 는 저장 즉시 commit 되고 색인은 여기 쌓인다 —
    **eventual consistency 가 계약이다**: 방금 저장한 항목이 즉시 검색되지 않을
    수 있으므로, 같은 태스크 안에서 self-read 가 필요한 데이터는 memory 가 아니라
    작업 컨텍스트로 다뤄야 한다.
    """

    max_failures: int = DEFAULT_MAX_INDEX_FAILURES
    pending: list[tuple[MemoryEntry, ChunkSpec]] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)
    since: datetime | None = None
    """지연을 이 시각부터 잰다 — warm-up 시작. 재시작 전에는 검색되던 항목이라, 저장 시각부터
    재면 며칠짜리 지연이 찍혀 실제로 밀린 새 항목과 구분되지 않는다."""

    def submit(self, entry: MemoryEntry, spec: ChunkSpec) -> None:
        """색인 요청을 큐에 넣는다 — 저장 경로를 막지 않는다."""
        self.pending.append((entry, spec))

    @property
    def lag(self) -> int:
        """아직 색인되지 않은 항목 수 — 지연 관측 지표."""
        return len(self.pending)

    def lag_seconds(self, *, now: datetime | None = None) -> dict[str, float]:
        """space 별 가장 오래된 미색인 항목의 나이.

        09 는 목표 지연(``index_lag_target_s``)을 초 단위로 둔다 — 목표를 지키는지
        보려면 개수가 아니라 시간이어야 한다. 시간 판정은 주입 가능하게 둔다.

        Args:
            now: Reference time; the current UTC time when omitted.

        Returns:
            Space name to the age of its oldest pending entry, in seconds.
        """
        reference = now or datetime.now(UTC)
        oldest: dict[str, float] = {}
        for entry, _spec in self.pending:
            queued = entry.created_at if self.since is None else max(entry.created_at, self.since)
            age = (reference - queued).total_seconds()
            oldest[entry.space] = max(oldest.get(entry.space, 0.0), age)
        return oldest

    def drain(self, indexes: dict[str, SpaceIndex]) -> int:
        """Index everything queued.

        큐에 쌓인 항목을 색인합니다. 실패는 재시도를 위해 큐에 남기고,
        누적 실패가 상한을 넘으면 ``MEM_003`` 으로 드러냅니다 — 조용히 색인되지
        않은 기억은 검색에서 영원히 사라집니다.

        Args:
            indexes: Space name to its index.

        Returns:
            The number of entries indexed.

        Raises:
            AaestroError: MEMORY/``MEM_003`` when an entry keeps failing.
        """
        indexed = 0
        retry: list[tuple[MemoryEntry, ChunkSpec]] = []
        remaining = list(self.pending)

        try:
            while remaining:
                entry, spec = remaining[0]
                index = indexes.get(entry.space)
                if index is None:
                    self._record_failure(entry, reason="unknown space")
                    retry.append(remaining.pop(0))
                    continue
                try:
                    index.add(entry, spec)
                except AaestroError:
                    self._record_failure(entry, reason="index rejected the entry")
                    retry.append(remaining.pop(0))
                    continue
                self.failures.pop(entry.entry_id, None)
                remaining.pop(0)
                indexed += 1
        finally:
            # _record_failure 가 MEM_003 을 던져도 진행 상황을 잃지 않는다.
            # 성공한 항목이 큐에 남으면 다음 drain 에서 중복 색인되어
            # dedup 과 idf 랭킹 가정이 깨진다
            self.pending = retry + remaining

        return indexed

    def _record_failure(self, entry: MemoryEntry, *, reason: str) -> None:
        """실패를 세고, 상한을 넘으면 에러로 올린다."""
        count = self.failures.get(entry.entry_id, 0) + 1
        self.failures[entry.entry_id] = count
        log.warning(
            "memory indexing failed",
            memory_space=entry.space,
            entry_id=entry.entry_id,
            attempt=count,
            max_attempts=self.max_failures,
            reason=reason,
        )
        if count >= self.max_failures:
            raise index_error(
                ErrorCode.MEM_003,
                "memory indexing keeps failing",
                space=entry.space,
                entry_id=entry.entry_id,
                attempts=count,
                reason=reason,
            )


@dataclass
class IndexRegistry:
    """All space indexes plus reindexing.

    space 별 인덱스 모음과 재인덱싱. 재인덱싱 중에도 검색은 **구 인덱스로**
    유지되고, 완료 시점에 원자적으로 교체된다 — 절반만 채워진 인덱스로 검색하면
    있는 기억이 없다고 나온다.
    """

    embedder: Embedder = field(default_factory=HashEmbedder)
    indexes: dict[str, SpaceIndex] = field(default_factory=dict)
    queue: IndexQueue = field(default_factory=IndexQueue)
    metrics: Metrics | None = None
    warming: bool = False
    """기동 warm-up 이 큐에 남아 있는지 — 큐가 처음 빌 때 완료를 한 번 알린다."""

    def warm(self, entries: Iterable[MemoryEntry], spec: ChunkSpec) -> int:
        """Queue stored entries so a restarted service can search them again.

        저장소의 항목을 색인 큐에 넣습니다. 인덱스는 프로세스 메모리에만 있어, 재시작하면
        저장된 기억이 검색되지 않습니다 (#312). 별도 경로를 두지 않고 **평소의 색인 큐**를
        탑니다 — 진행은 ``aaestro_memory_index_lag_seconds`` 로, 실패는 ``MEM_003`` 으로
        드러나는 것이 평소와 같습니다.

        Args:
            entries: Every stored entry, oldest first.
            spec: Chunking policy.

        Returns:
            The number of entries queued.
        """
        queued = 0
        spaces: set[str] = set()
        self.queue.since = datetime.now(UTC)
        for entry in entries:
            self.submit(entry, spec)
            spaces.add(entry.space)
            queued += 1
        self.warming = queued > 0
        log.info("memory index warm-up queued", entries=queued, spaces=len(spaces))
        return queued

    def index_for(self, space: str) -> SpaceIndex:
        """space 의 인덱스 — 없으면 만든다."""
        if space not in self.indexes:
            self.indexes[space] = SpaceIndex(space=space, embedder=self.embedder)
        return self.indexes[space]

    def submit(self, entry: MemoryEntry, spec: ChunkSpec) -> None:
        """비동기 색인 요청 — 대상 space 인덱스를 미리 만들어 둔다."""
        self.index_for(entry.space)
        self.queue.submit(entry, spec)
        # 지연은 **쌓여 있는 동안** 관측해야 한다 — drain 후에만 재면 큐가 비어
        # 있어 항상 0 이고, 밀리고 있다는 사실이 메트릭에 영원히 안 나온다
        self._observe()

    def drain(self) -> int:
        """큐를 비우고 space 별 크기·지연을 관측한다."""
        try:
            return self.queue.drain(self.indexes)
        finally:
            # 실패해 큐에 남아도 관측한다 — 그때가 지연이 가장 중요한 시점이다
            self._observe()
            if self.warming and self.queue.lag == 0:
                self.warming = False
                log.info(
                    "memory index warm-up completed",
                    entries=sum(len(index.entry_ids) for index in self.indexes.values()),
                    spaces=len(self.indexes),
                )

    def _observe(self, *, now: datetime | None = None) -> None:
        """space 크기와 인덱싱 지연을 게이지에 반영한다 — 메트릭 미주입 시 무동작."""
        if self.metrics is None:
            return

        telemetry = IndexTelemetry(self.metrics)
        for space, index in self.indexes.items():
            telemetry.entries(space=space, count=len(index.metadata))

        lag = self.queue.lag_seconds(now=now)
        # 큐가 빈 space 는 지연이 0 이다 — 값을 남겨두면 해소된 지연이 계속 보인다
        for space in self.indexes:
            telemetry.index_lag(space=space, seconds=lag.get(space, 0.0))

    def reindex(
        self, space: str, entries: Iterable[MemoryEntry], spec: ChunkSpec, *, embedder: Embedder
    ) -> SpaceIndex:
        """Rebuild a space index with a new embedder.

        새 임베더로 space 인덱스를 다시 만듭니다. **완성될 때까지 기존 인덱스로
        검색이 유지되고**, 끝난 뒤 한 번에 교체됩니다 (혼합 임베딩 공간 금지).

        Args:
            space: The space to rebuild.
            entries: Every entry belonging to the space.
            spec: Chunking policy.
            embedder: The new embedder — its dimensions replace the old ones.

        Returns:
            The newly built index, already swapped in.
        """
        rebuilt = SpaceIndex(space=space, embedder=embedder)
        for entry in entries:
            rebuilt.add(entry, spec)

        # 원자적 교체 — 이 대입 전까지 검색은 구 인덱스를 본다
        self.indexes[space] = rebuilt
        self.embedder = embedder
        log.info(
            "memory space reindexed",
            memory_space=space,
            entries=len(rebuilt.entry_ids),
        )
        return rebuilt


__all__ = [
    "DEFAULT_MAX_INDEX_FAILURES",
    "Chunk",
    "Hit",
    "IndexQueue",
    "IndexRegistry",
    "SpaceIndex",
    "index_error",
    "split_chunks",
]
