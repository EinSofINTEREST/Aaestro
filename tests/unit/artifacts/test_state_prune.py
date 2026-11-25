"""State original pruning (#665) — 보존 기간과 진행 중 run 보호."""

from __future__ import annotations

import os

import pytest

from aaestro.artifacts import FilesystemArtifactStore
from aaestro.artifacts.store import TEMP_PREFIX
from aaestro.core.errors import AaestroError
from aaestro.orchestrator.state_artifacts import (
    PrivateArtifactStore,
    artifact_key,
    prune_originals,
)

DAY = 86400
NOW = 1_000_000_000.0  # fake clock — 실제 시간을 기다리지 않는다


@pytest.fixture
def store(tmp_path) -> PrivateArtifactStore:
    return PrivateArtifactStore(FilesystemArtifactStore(root=tmp_path, scope="orchestrator--state"))


async def _put(store: PrivateArtifactStore, key: str, age_days: float) -> None:
    await store.put(key, b"x")
    stamp = NOW - age_days * DAY
    os.utime(store.root / store.scope / key, (stamp, stamp))


async def test_entries_list_keys_with_mtime_and_hide_temp_files(store):
    await _put(store, "a/b", 1)
    (store.root / store.scope / "a" / f"{TEMP_PREFIX}x").write_bytes(b"half")

    assert await store.entries() == [("a/b", NOW - DAY)]


async def test_delete_removes_one_key_and_tolerates_a_missing_one(store):
    await _put(store, "a/b", 1)

    await store.delete("a/b")
    await store.delete("a/b")

    assert await store.entries() == []
    with pytest.raises(AaestroError):
        await store.delete("../escape")


async def test_prune_deletes_only_originals_past_retention(store):
    old = artifact_key("g", "r-old", "notes", "b1")
    young = artifact_key("g", "r-young", "notes", "b1")
    await _put(store, old, 31)
    await _put(store, young, 29)

    result = await prune_originals(store, retention_days=30, now=NOW, read_live_runs=list)

    assert (result.deleted, result.skipped_young, result.skipped_live) == (1, 1, 0)
    assert [key for key, _ in await store.entries()] == [young]


async def test_prune_keeps_originals_of_live_runs_however_old(store):
    live = artifact_key("g", "r-live", "notes", "b1")
    same_id_other_graph = artifact_key("other", "r-live", "notes", "b1")
    await _put(store, live, 400)
    await _put(store, same_id_other_graph, 400)

    result = await prune_originals(
        store, retention_days=30, now=NOW, read_live_runs=lambda: [("g", "r-live")]
    )

    assert (result.deleted, result.skipped_live) == (1, 1)
    assert [key for key, _ in await store.entries()] == [live]


async def test_prune_counts_failures_and_continues(store, monkeypatch):
    first = artifact_key("g", "r1", "notes", "b1")
    second = artifact_key("g", "r2", "notes", "b1")
    await _put(store, first, 90)
    await _put(store, second, 80)
    real = store.delete

    async def flaky(key: str) -> None:
        if key == first:
            raise AaestroError(category="storage", code="STOR_003", message="boom")  # type: ignore[arg-type]
        await real(key)

    monkeypatch.setattr(PrivateArtifactStore, "delete", lambda self, key: flaky(key))

    result = await prune_originals(store, retention_days=30, now=NOW, read_live_runs=list)

    assert (result.deleted, result.failed) == (1, 1)


async def test_prune_leaves_a_key_rewritten_after_the_listing(store):
    key = artifact_key("g", "r1", "notes", "b1")
    await _put(store, key, 90)
    reads = []

    def live():
        # 두 번째 읽기는 목록 뒤 — 이 사이에 같은 key 가 다시 쓰인 것으로 본다
        reads.append(1)
        if len(reads) == 2:
            path = store.root / store.scope / key
            os.utime(path, (NOW, NOW))
        return []

    result = await prune_originals(store, retention_days=30, now=NOW, read_live_runs=live)

    assert (result.deleted, result.skipped_young) == (0, 1)
    assert [k for k, _ in await store.entries()] == [key]


async def test_prune_protects_a_run_that_started_while_listing(store):
    key = artifact_key("g", "r1", "notes", "b1")
    await _put(store, key, 90)
    calls = []

    def live():
        calls.append(1)
        return [("g", "r1")] if len(calls) == 2 else []

    result = await prune_originals(store, retention_days=30, now=NOW, read_live_runs=live)

    assert (result.deleted, result.skipped_live) == (0, 1)


async def test_prune_ignores_files_outside_the_original_key_shape_and_links(store):
    scope = store.root / store.scope
    await _put(store, "keep.db", 400)
    victim = artifact_key("g", "r1", "notes", "b1")
    await _put(store, victim, 400)
    link = scope / "state-compaction" / "link"
    link.symlink_to(scope / "keep.db")

    result = await prune_originals(store, retention_days=30, now=NOW, read_live_runs=list)

    assert result.deleted == 1
    assert (scope / "keep.db").exists()
    assert link.is_symlink()
    await store.delete("state-compaction/link")  # 링크를 따라가 지우지 않는다
    assert (scope / "keep.db").exists() and link.is_symlink()
