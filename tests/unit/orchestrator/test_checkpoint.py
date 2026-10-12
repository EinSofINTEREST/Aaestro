"""Unit tests for checkpointer configuration and error mapping."""

from __future__ import annotations

import asyncio

import pytest
from langgraph.checkpoint.memory import MemorySaver

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.orchestrator.checkpoint import (
    DEFAULT_CHECKPOINTER,
    CheckpointerKind,
    build_checkpointer,
    guarded_restore,
    guarded_save,
)


def test_default_is_in_memory():
    assert DEFAULT_CHECKPOINTER is CheckpointerKind.MEMORY
    assert isinstance(build_checkpointer(), MemorySaver)


def test_default_keyword_resolves_to_framework_default():
    """토폴로지의 checkpointer: default 는 프레임워크 기본값으로 해석된다."""
    assert isinstance(build_checkpointer("default"), MemorySaver)


def test_memory_kind_builds_saver():
    assert isinstance(build_checkpointer(CheckpointerKind.MEMORY), MemorySaver)


@pytest.mark.parametrize("kind", [CheckpointerKind.REDIS, CheckpointerKind.POSTGRES])
def test_external_backend_requires_url(kind):
    with pytest.raises(AaestroError) as exc_info:
        build_checkpointer(kind)

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.category is ErrorCategory.CONFIG
    assert "connection url" in exc_info.value.message


def test_unknown_backend_is_rejected_with_cfg_001():
    """설정 오류는 비구조화 ValueError 가 아니라 CFG_001 로 보고돼야 한다."""
    with pytest.raises(AaestroError) as exc_info:
        build_checkpointer("cassandra")

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.category is ErrorCategory.CONFIG


async def test_guarded_save_converts_failure_to_stor_001():
    async def failing():
        raise OSError("disk full")

    with pytest.raises(AaestroError) as exc_info:
        await guarded_save(failing, graph="g", run_id="r")

    assert exc_info.value.code == "STOR_001"
    assert exc_info.value.category is ErrorCategory.STORAGE
    assert exc_info.value.retryable is True
    assert isinstance(exc_info.value.__cause__, OSError)


async def test_guarded_restore_converts_failure_to_stor_002():
    async def failing():
        raise OSError("corrupt")

    with pytest.raises(AaestroError) as exc_info:
        await guarded_restore(failing, graph="g", run_id="r")

    assert exc_info.value.code == "STOR_002"


async def test_guarded_helpers_pass_through_values():
    async def ok():
        return "value"

    assert await guarded_save(ok, graph="g", run_id="r") == "value"
    assert await guarded_restore(ok, graph="g", run_id="r") == "value"


async def test_guarded_helpers_do_not_rewrap_aaestro_errors():
    """이미 구조화된 에러는 그대로 전파한다 (이중 변환 방지)."""
    original = AaestroError(
        category=ErrorCategory.STORAGE, code="STOR_003", message="registry error"
    )

    async def failing():
        raise original

    with pytest.raises(AaestroError) as exc_info:
        await guarded_save(failing, graph="g", run_id="r")

    assert exc_info.value is original


async def test_guarded_helpers_do_not_swallow_cancellation():
    """취소는 STOR_* 로 감싸지 않고 그대로 전파돼야 한다 (drain/shutdown 경로)."""

    async def cancelled():
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await guarded_save(cancelled, graph="g", run_id="r")

    with pytest.raises(asyncio.CancelledError):
        await guarded_restore(cancelled, graph="g", run_id="r")
