"""Control API retry.

05 Retry Layering 은 Agent Control API 의 재시도 주체를 **runtime** 으로
정한다. 어댑터가 있어도(#176) 부르는 쪽이 없으면 아무 일도 하지 않는다.

**읽기만 재시도한다**: `/invoke` 는 부수효과를 낳고, node 재시도
(`NodeSpec.retry`, #191)와 곱해진다 — 05 는 재시도를 한 계층에서만 하라고
규정한다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from aaestro.core.errors import NETWORK_RETRY, AaestroError, ErrorCode
from aaestro.core.manifest import AgentManifest
from aaestro.runtime.control import ControlClient
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from tests.fixtures.builders import make_task
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]

HEALTHY = {"status": "healthy", "components": {}, "checked_at": "2026-01-01T00:00:00Z"}


class FlakyServer:
    """정해진 횟수만큼 전송에 실패한 뒤 응답하는 서버 대역."""

    def __init__(self, failures: int, *, body: dict[str, Any] | None = None) -> None:
        self._left = failures
        self._body = body if body is not None else HEALTHY
        self.calls: list[str] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request.url.path)
        if self._left > 0:
            self._left -= 1
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json=self._body)


@pytest.fixture
def waits() -> list[float]:
    return []


def client(server: FlakyServer, waits: list[float], **kwargs: Any) -> ControlClient:
    async def sleep(delay: float) -> None:
        waits.append(delay)

    kwargs.setdefault("retry", NETWORK_RETRY)
    return ControlClient(
        "http://agent.test",
        agent="researcher",
        client=httpx.AsyncClient(transport=httpx.MockTransport(server.handle)),
        retry_sleep=sleep,
        **kwargs,
    )


async def test_health_is_not_retried(waits):
    """#217 — health 는 **위층에** 재시도를 갖고 있다. 안에서 또 하면 이중이다.

    `HealthMonitor` 가 연속 실패를 세어 임계에서 Unhealthy 로 전이시킨다.
    여기서 backoff 를 돌면 한 번의 확인이 monitor 의 timeout(3초)을 넘겨
    모든 확인이 timeout 으로 잘리고 실제 원인이 사라진다.
    """
    server = FlakyServer(failures=1)

    with pytest.raises(AaestroError) as excinfo:
        await client(server, waits).health()

    assert excinfo.value.code == ErrorCode.NET_001
    assert len(server.calls) == 1, "health 가 안에서 재시도하면 이중 재시도다"
    assert waits == []


async def test_the_card_read_is_retried(waits):
    """카드 조회도 순수 읽기다 — 재시도해도 부수효과가 없다."""
    server = FlakyServer(failures=1, body={"name": "researcher"})

    await client(server, waits).card()

    assert len(server.calls) == 2


async def test_invoke_is_not_retried(waits):
    """부수효과를 낳고 node 재시도와 곱해진다 — 05 는 한 계층만 허용한다."""
    server = FlakyServer(failures=1)

    with pytest.raises(AaestroError) as excinfo:
        await client(server, waits).invoke(make_task())

    assert excinfo.value.code == ErrorCode.NET_001
    assert len(server.calls) == 1
    assert waits == []


async def test_retry_is_off_unless_the_assembly_turns_it_on(waits):
    """미주입이면 재시도하지 않는다 — 조립하는 쪽이 켠다."""
    server = FlakyServer(failures=1, body={"name": "researcher"})

    with pytest.raises(AaestroError):
        await client(server, waits, retry=None).card()

    assert len(server.calls) == 1


async def test_exhausted_retries_surface_the_last_error(waits):
    """감싸서 올리면 호출자의 카테고리 기반 처리가 깨진다."""
    server = FlakyServer(failures=99, body={"name": "researcher"})

    with pytest.raises(AaestroError) as excinfo:
        await client(server, waits).card()

    assert excinfo.value.code == ErrorCode.NET_001
    assert len(server.calls) == NETWORK_RETRY.max_attempts


async def test_the_production_assembly_turns_retry_on():
    """정책을 정의만 하고 조립에서 켜지 않으면 아무 일도 없다.

    runtime 이 **실제로 만드는** 클라이언트를 본다 — 여기서 놓치면 어댑터가
    있어도 부르는 곳이 없는 상태로 돌아간다.
    """
    manifest = AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )

    launched = await AgentLauncher(engine=DockerEngine(client=FakeDockerClient())).start(manifest)

    assert launched.client._retry is NETWORK_RETRY
