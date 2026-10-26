"""The Memory Service process wiring for registry mode (#278)."""

from __future__ import annotations

import asyncio

import pytest

from aaestro.access.client import ACCESS_URL_ENV, ENFORCER_TOKEN_ENV, AccessClient
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.memory import __main__ as entry
from aaestro.memory.bootstrap import MemoryDeployment
from aaestro.observability.metrics import Metrics


def test_registry_mode_is_off_without_settings(monkeypatch):
    monkeypatch.delenv(ACCESS_URL_ENV, raising=False)
    monkeypatch.delenv(ENFORCER_TOKEN_ENV, raising=False)

    assert entry._access(Metrics()) is None  # noqa: SLF001


def test_registry_mode_needs_both_settings(monkeypatch):
    """하나만 있으면 조용히 토큰 모드로 뜬다 — 운영자는 실시간 회수가 된다고 믿는다."""
    monkeypatch.setenv(ACCESS_URL_ENV, "http://control-plane:8700")
    monkeypatch.delenv(ENFORCER_TOKEN_ENV, raising=False)

    with pytest.raises(AaestroError) as exc_info:
        entry._access(Metrics())  # noqa: SLF001

    assert exc_info.value.code == ErrorCode.CFG_001


def test_registry_mode_builds_a_memory_client(monkeypatch):
    monkeypatch.setenv(ACCESS_URL_ENV, "http://control-plane:8700")
    monkeypatch.setenv(ENFORCER_TOKEN_ENV, "enforcer")
    metrics = Metrics()

    client = entry._access(metrics)  # noqa: SLF001

    assert isinstance(client, AccessClient)
    assert (client.base_url, client.enforcer_token, client.component) == (
        "http://control-plane:8700",
        "enforcer",
        "memory",
    )
    assert client.metrics is metrics, "노출되지 않는 계측기에 쓰면 아무도 못 본다"


async def test_the_process_follows_the_change_feed_while_serving(monkeypatch):
    """알림을 따라가는 주체가 없으면 회수가 캐시 뒤에 묻힌다."""
    watched = asyncio.Event()
    closed = []

    class Client:
        async def watch(self) -> None:
            watched.set()
            await asyncio.Event().wait()

        async def aclose(self) -> None:
            closed.append(True)

    async def serve(self) -> None:
        await asyncio.wait_for(watched.wait(), 1)

    monkeypatch.setattr(entry.uvicorn.Server, "serve", serve)
    deployment = MemoryDeployment(app=object(), access=Client())  # type: ignore[arg-type]

    await entry._run(deployment, 0, interval_s=60)  # noqa: SLF001

    assert watched.is_set() and closed == [True]


async def test_shutdown_yields_to_an_outer_cancellation(monkeypatch):
    """서버가 끝난 뒤 정리가 바깥 취소를 삼키면 종료 timeout 이 사라진다 (#490)."""

    watchers: list[asyncio.Task[None]] = []

    class Client:
        async def watch(self) -> None:
            watchers.append(asyncio.current_task())  # type: ignore[arg-type]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await asyncio.sleep(0.5)
                raise

        async def aclose(self) -> None:
            return None

    async def serve(self) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(entry.uvicorn.Server, "serve", serve)
    deployment = MemoryDeployment(app=object(), access=Client())  # type: ignore[arg-type]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entry._run(deployment, 0, interval_s=60), 0.2)  # noqa: SLF001

    assert all(task.done() and task.cancelled() for task in watchers)


async def test_shutdown_still_closes_and_drains_when_cancelled_from_outside(monkeypatch):
    """바깥 취소가 와도 연결 정리와 마지막 색인은 건너뛰지 않는다 (#490)."""
    closed: list[bool] = []
    drained: list[bool] = []
    cancelled: list[str] = []
    watchers: list[asyncio.Task[None]] = []

    class Client:
        async def watch(self) -> None:
            watchers.append(asyncio.current_task())  # type: ignore[arg-type]
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.append("watch")
                await asyncio.sleep(0.5)
                raise

        async def aclose(self) -> None:
            closed.append(True)

    async def serve(self) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(entry.uvicorn.Server, "serve", serve)
    monkeypatch.setattr(entry, "_drain_once", lambda indexer: drained.append(True))
    deployment = MemoryDeployment(app=object(), access=Client(), indexer=object())  # type: ignore[arg-type]

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(entry._run(deployment, 0, interval_s=60), 0.2)  # noqa: SLF001

    assert closed == [True] and drained == [True] and cancelled == ["watch"]
    assert all(task.done() and task.cancelled() for task in watchers)
