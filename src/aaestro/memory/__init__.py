"""Memory Service — context memory spaces and access control.

컨텍스트 메모리 space 와 접근 제어. 저장소 자격증명은 서비스만 보유한다.
"""

from aaestro.memory.backend import create_store
from aaestro.memory.compaction import (
    CompactionPlan,
    Summarizer,
    build_summary,
    expired_entries,
    plan_compaction,
)
from aaestro.memory.embedding import Embedder, HashEmbedder, cosine, normalize, tokenize
from aaestro.memory.entry import MAX_CONTENT_BYTES, MemoryEntry, MemorySource
from aaestro.memory.index import (
    Chunk,
    Hit,
    IndexQueue,
    IndexRegistry,
    SpaceIndex,
    index_error,
    split_chunks,
)
from aaestro.memory.recall import (
    AutoRecall,
    Recall,
    ScoredEntry,
    apply_budget,
    reciprocal_rank_fusion,
    render_context,
)
from aaestro.memory.service import (
    SCOPE_PRECEDENCE,
    AccessToken,
    MemoryService,
    MemorySpace,
    access_denied,
    build_token,
)
from aaestro.memory.store import (
    MemoryStore,
    SqliteMemoryStore,
    storage_error,
    validate_entry,
)

__all__ = [
    "MAX_CONTENT_BYTES",
    "SCOPE_PRECEDENCE",
    "AccessToken",
    "AutoRecall",
    "Chunk",
    "CompactionPlan",
    "Embedder",
    "HashEmbedder",
    "Hit",
    "IndexQueue",
    "IndexRegistry",
    "MemoryEntry",
    "MemoryService",
    "MemorySource",
    "MemorySpace",
    "MemoryStore",
    "Recall",
    "ScoredEntry",
    "SpaceIndex",
    "SqliteMemoryStore",
    "Summarizer",
    "access_denied",
    "apply_budget",
    "build_summary",
    "build_token",
    "cosine",
    "create_store",
    "expired_entries",
    "index_error",
    "normalize",
    "plan_compaction",
    "reciprocal_rank_fusion",
    "render_context",
    "split_chunks",
    "storage_error",
    "tokenize",
    "validate_entry",
]
