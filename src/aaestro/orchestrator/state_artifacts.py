"""Where state compaction keeps the originals it replaces.

state 압축이 접은 원문의 저장소. **오케스트레이터 전용** 이다 — 에이전트가 쥐는 artifact
스코프 (local / group / global) 와 별개이고, 네임스페이스 이름부터 에이전트 · 그룹 이름이
될 수 없는 모양이라 같은 루트를 써도 어느 에이전트의 스코프와도 겹치지 않는다.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import structlog

from aaestro.artifacts.store import FilesystemArtifactStore, parse_ref
from aaestro.core.errors import AaestroError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

log = structlog.get_logger()

STATE_ARTIFACT_SCOPE: Final = "orchestrator--state"
"""원문 저장소의 네임스페이스.

연속 하이픈은 에이전트 · 그룹 이름 규칙 (``NAME_PATTERN``) 이 받지 않는다 — 이름이 ``run``
같은 에이전트가 자기 local 스코프로 이 원문을 읽고 쓰는 일이 구조적으로 생기지 않는다.
"""

STATE_ARTIFACT_SETTING: Final = "orchestrator.state_artifact_root"
"""원문 저장소 경로를 주는 설정 키 — 거절 메시지가 이 이름을 말한다."""

ARTIFACT_KEY_PREFIX: Final = "state-compaction"
_KEY_ROOT: Final = f"{ARTIFACT_KEY_PREFIX}/"

_SECONDS_PER_DAY: Final = 86400
_PRIVATE_DIR_MODE: Final = 0o700
_PRIVATE_FILE_MODE: Final = 0o600

_UNSAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9.-]")
_SLUG_MAX_CHARS: Final = 48
"""key 조각에 남기는 읽을 수 있는 부분의 길이 — 긴 run id 가 파일 이름 한도를 넘지 않게 한다."""
_DIGEST_LENGTH: Final = 16


def _slug(value: str) -> str:
    """artifact key 조각 — 허용 문자 밖은 바꾸고 길이를 자른 뒤, 겹치지 않게 해시를 붙인다."""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:_DIGEST_LENGTH]
    readable = _UNSAFE_KEY_CHARS.sub("-", value)[:_SLUG_MAX_CHARS]
    # 점으로 시작하는 조각은 `validate_key` 가 거절한다 — 해시가 겹침을 막는다
    if not readable or readable.startswith("."):
        readable = "-" + readable[1:]
    return f"{readable}.{digest}"


def run_key_prefix(graph: str, run_id: str) -> str:
    """한 run 의 원문이 모두 쓰는 key 접두 — 정리가 진행 중 run 을 가려내는 기준이다."""
    return f"{ARTIFACT_KEY_PREFIX}/{_slug(graph)}/{_slug(run_id)}/"


def field_key_prefix(graph: str, run_id: str, field: str) -> str:
    """한 run 의 한 필드가 쓰는 key 접두 — 그래프가 달라지면 같은 run id 여도 겹치지 않는다."""
    return f"{run_key_prefix(graph, run_id)}{_slug(field)}"


def artifact_key(graph: str, run_id: str, field: str, boundary: str) -> str:
    """The idempotency key for one field's originals at one boundary.

    한 경계에서 한 필드의 원문이 놓일 key. 같은 경계를 다시 지나면 같은 key 를 덮어쓰고,
    다른 경계 · 필드 · run · 그래프는 절대 같은 key 가 되지 않는다 — 겹치면 앞선 원문을 잃는다.
    """
    return f"{field_key_prefix(graph, run_id, field)}/{_slug(boundary)}"


def is_own_reference(ref: Any, *, graph: str, run_id: str, field: str) -> bool:
    """이 참조가 이 run · 이 필드의 압축이 만든 것인지 — 모양만 같은 참조는 에이전트 산출물이다."""
    if not isinstance(ref, str):
        return False
    try:
        key = parse_ref(ref).key
    except AaestroError:
        return False
    return key.startswith(f"{field_key_prefix(graph, run_id, field)}/")


def encode_originals(older: list[Any], **provenance: Any) -> bytes:
    """Serialize the folded originals for the artifact store.

    접힌 원문을 artifact 로 직렬화합니다 — 출처(그래프 · run · 필드 · 경계)를 함께 실어 참조만
    들고도 어디서 나온 원문인지 알 수 있게 합니다.

    Raises:
        TypeError: If an item is not JSON-serializable.
        ValueError: If an item cannot be encoded.
    """
    return json.dumps({**provenance, "items": older}, ensure_ascii=False).encode("utf-8")


def decode_originals(data: bytes) -> list[Any]:
    """Read the folded originals back from an artifact.

    artifact 에서 접힌 원문을 되읽습니다.
    """
    items: list[Any] = json.loads(data.decode("utf-8"))["items"]
    return items


@dataclass(frozen=True)
class PrivateArtifactStore:
    """Keeps state originals readable by the orchestrator's user only.

    원문 저장소 — 네임스페이스 디렉토리는 0700, 파일은 0600 으로 둔다. state 원문에는 여러
    에이전트의 산출물이 섞여 있어, 같은 호스트의 다른 사용자가 읽을 이유가 없다.
    """

    inner: FilesystemArtifactStore

    @property
    def root(self) -> Path:
        """저장 루트."""
        return self.inner.root

    @property
    def scope(self) -> str:
        """네임스페이스 이름."""
        return self.inner.scope

    def _namespace(self) -> Path:
        return self.inner.root / self.inner.scope

    async def put(self, key: str, data: bytes) -> str:
        """Store originals and return their reference.

        원문을 저장하고 참조를 돌려줍니다 — 저장소 구현의 오류는 그대로 전파합니다.
        """
        namespace = self._namespace()
        await asyncio.to_thread(self._prepare_namespace, namespace)
        ref = await self.inner.put(key, data)
        await asyncio.to_thread((namespace / key).chmod, _PRIVATE_FILE_MODE)
        return ref

    @staticmethod
    def _prepare_namespace(namespace: Path) -> None:
        namespace.mkdir(mode=_PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        # 이미 있던 디렉토리는 mkdir 의 mode 를 따르지 않는다 — 매번 맞춘다
        namespace.chmod(_PRIVATE_DIR_MODE)

    async def get(self, ref: str) -> bytes:
        """참조로 원문을 읽는다."""
        return await self.inner.get(ref)

    async def entries(self) -> list[tuple[str, float]]:
        """저장된 (key, 수정 시각 epoch 초) — 오래된 순."""
        return await self.inner.stored_entries()

    async def modified_at(self, key: str) -> float | None:
        """key 하나의 현재 수정 시각 — 없거나 일반 파일이 아니면 None."""
        return await self.inner.modified_at(key)

    async def delete(self, key: str) -> None:
        """key 하나를 지운다 (없으면 그냥 끝난다)."""
        await self.inner.delete(key)

    def stored_keys(self) -> list[str]:
        """저장된 key 목록 — 운영 점검용."""
        return list(self.inner.stored_keys())


@dataclass(frozen=True)
class PruneResult:
    """What one prune pass did — counts only, never key names."""

    deleted: int = 0
    skipped_live: int = 0
    skipped_young: int = 0
    failed: int = 0


async def prune_originals(
    store: PrivateArtifactStore,
    *,
    retention_days: int,
    now: float,
    read_live_runs: Callable[[], Iterable[tuple[str, str]]],
) -> PruneResult:
    """Delete originals older than the retention period, except those of live runs.

    보존 기간이 지난 원문만 지운다. ``live_runs`` ((graph, run_id)) 의 원문은 기간과 무관하게
    남긴다 — 진행 중인 run 이 접은 원문은 아직 쓰일 수 있다. 개별 삭제가 실패해도 나머지는 계속한다.
    """

    def prefixes() -> tuple[str, ...]:
        return tuple(run_key_prefix(graph, run_id) for graph, run_id in read_live_runs())

    live_prefixes = prefixes()
    cutoff = now - retention_days * _SECONDS_PER_DAY
    deleted = skipped_live = skipped_young = failed = 0
    entries = await store.entries()
    # 목록을 읽는 사이 시작된 run 도 보호한다 — 두 번 읽은 결과를 합친다
    live_prefixes += prefixes()
    for key, modified in entries:
        if not key.startswith(_KEY_ROOT):
            continue  # 원문 key 모양이 아닌 파일은 이 정리의 몫이 아니다
        if key.startswith(live_prefixes):
            skipped_live += 1
        elif modified > cutoff:
            skipped_young += 1
        else:
            try:
                # 목록 뒤에 같은 key 가 다시 쓰였다면 새 원문이다 — 지우지 않는다.
                # shortcut: 잠금이 없어 두 번째 run 조회 뒤 시작된 재개와 modified_at 뒤의 재작성은
                # 못 막는다, 운영은 cron 시각을 피하고 필요하면 잠금을 올린다
                current = await store.modified_at(key)
                if current is None:
                    continue
                if current != modified or current > cutoff:
                    skipped_young += 1
                    continue
                await store.delete(key)
            except AaestroError as err:
                failed += 1
                log.warning("state original could not be deleted", error_code=err.code)
            else:
                deleted += 1
    return PruneResult(deleted, skipped_live, skipped_young, failed)


def state_artifact_store(root: str | Path | None) -> PrivateArtifactStore | None:
    """Open the store that keeps the originals replaced by state compaction.

    state 압축이 접은 원문을 두는 저장소 — 경로 (``orchestrator.state_artifact_root``) 가
    없으면 None 입니다.
    """
    if root is None:
        return None
    return PrivateArtifactStore(
        FilesystemArtifactStore(root=Path(root), scope=STATE_ARTIFACT_SCOPE)
    )


__all__ = [
    "STATE_ARTIFACT_SCOPE",
    "STATE_ARTIFACT_SETTING",
    "PrivateArtifactStore",
    "PruneResult",
    "artifact_key",
    "decode_originals",
    "encode_originals",
    "field_key_prefix",
    "is_own_reference",
    "prune_originals",
    "run_key_prefix",
    "state_artifact_store",
]
