"""Unit tests for the memoryset loader."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError
from aaestro.modules.memoryset import (
    MemoryKind,
    MemoryScope,
    MemorysetLoader,
    check_attachment_scope,
)
from aaestro.modules.registry import ModuleRegistry
from tests.fixtures.registry import fixture_registry


@pytest.fixture
def loader() -> MemorysetLoader:
    return MemorysetLoader(fixture_registry())


def test_load_local_scope_policy(loader):
    memoryset = loader.load("memorysets/agent-longterm@0.1.0")
    spec = memoryset.manifest.spec

    assert memoryset.scope is MemoryScope.LOCAL
    assert spec.index.embedding.dimensions == 1536
    assert spec.recall.budget_tokens == 2000
    assert spec.retention.compaction is not None
    assert spec.retention.compaction.keep_kinds == (MemoryKind.FACT, MemoryKind.SUMMARY)


def test_run_scope_with_compaction(loader):
    memoryset = loader.load("memorysets/run-scratch@0.1.0")

    assert memoryset.scope is MemoryScope.RUN
    assert memoryset.declares_compaction is True


def test_attachment_scope_match_passes(loader):
    memoryset = loader.load("memorysets/agent-longterm@0.1.0")

    check_attachment_scope(memoryset, MemoryScope.LOCAL)


def test_attachment_scope_mismatch_raises_mod_003(loader):
    """부착 위치와 선언 스코프 불일치 — 04 Attachment 규칙 4."""
    memoryset = loader.load("memorysets/agent-longterm@0.1.0")

    with pytest.raises(AaestroError) as exc_info:
        check_attachment_scope(memoryset, MemoryScope.GROUP)

    assert exc_info.value.code == "MOD_003"


def _write_memoryset(tmp_path, spec_body: str):
    module = tmp_path / "modules" / "memorysets" / "custom" / "0.1.0"
    module.mkdir(parents=True)
    (module / "memoryset.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Memoryset\nmetadata:\n"
        "  name: custom\n  version: 0.1.0\nspec:\n" + spec_body,
        encoding="utf-8",
    )
    return MemorysetLoader(ModuleRegistry.under(tmp_path))


INDEX = """  index:
    embedding:
      provider: p
      model: m
      dimensions: 8
"""


@pytest.mark.parametrize("scope", ["local", "group", "global"])
def test_persistent_scope_without_retention_raises_mod_003(tmp_path, scope):
    """영구 스코프는 보존 정책 필수 — 09 Compaction & Retention 5."""
    loader = _write_memoryset(tmp_path, f"  scope: {scope}\n" + INDEX)

    with pytest.raises(AaestroError) as exc_info:
        loader.load("memorysets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_run_scope_without_retention_is_allowed(tmp_path):
    loader = _write_memoryset(tmp_path, "  scope: run\n" + INDEX)

    assert loader.load("memorysets/custom@0.1.0").scope is MemoryScope.RUN


def test_overlap_must_be_smaller_than_chunk(tmp_path):
    loader = _write_memoryset(
        tmp_path,
        "  scope: run\n" + INDEX + "    chunk:\n      max_tokens: 100\n      overlap_tokens: 100\n",
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("memorysets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_zero_hybrid_weights_are_rejected(tmp_path):
    loader = _write_memoryset(
        tmp_path,
        "  scope: run\n" + INDEX + "    hybrid:\n      vector_weight: 0\n      lexical_weight: 0\n",
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("memorysets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_invalid_scope_raises_mod_003(tmp_path):
    loader = _write_memoryset(tmp_path, "  scope: task\n" + INDEX)

    with pytest.raises(AaestroError) as exc_info:
        loader.load("memorysets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"
