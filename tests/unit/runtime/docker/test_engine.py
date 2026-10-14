"""Unit tests for the Docker SDK call layer.

daemon 없이 검증한다 — 이 계층의 계약은 에러 변환과 정리 보장이지
Docker 그 자체가 아니다.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.runtime.docker.engine import (
    ContainerHandle,
    DockerClient,
    DockerEngine,
    agent_of,
    control_port_of,
)
from aaestro.runtime.spec import build_container_spec
from tests.fixtures.builders import make_manifest
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import until


def spec(**overrides):
    """manifest 에서 파생한 컨테이너 스펙."""
    return build_container_spec(make_manifest(**overrides))


def engine(client: FakeDockerClient | None = None) -> DockerEngine:
    return DockerEngine(client=client or FakeDockerClient())


# --- 스펙 해석 ----------------------------------------------------------------


def test_agent_name_comes_from_the_label():
    """라벨이 곧 소유자 표시다 — 별도 필드를 만들지 않는다."""
    assert agent_of(spec()) == "test-agent"


def test_control_port_is_found_by_name():
    assert control_port_of(spec()) == 8080


def test_spec_without_a_control_port_is_rejected():
    """control 포트가 없으면 runtime 이 에이전트에 닿을 수 없다."""
    without = spec()
    stripped = type(without)(**{**without.__dict__, "ports": ()})

    with pytest.raises(AaestroError) as exc_info:
        control_port_of(stripped)

    assert exc_info.value.code == "RT_001"
    # 결정적 계약 위반이다 — 재시도 가능으로 표시하면 재시작 루프가 돈다
    assert exc_info.value.retryable is False


def test_fake_client_satisfies_the_contract():
    assert isinstance(FakeDockerClient(), DockerClient)


# --- 기동 --------------------------------------------------------------------


async def test_start_creates_and_starts_the_container():
    client = FakeDockerClient()

    handle = await engine(client).start(spec())

    assert handle.agent == "test-agent"
    assert client.started == [handle.container_id]
    assert handle.control_port == 49152


async def test_start_uses_the_declared_network():
    """에이전트는 전용 bridge 에만 붙는다."""
    client = FakeDockerClient()

    await engine(client).start(spec())

    assert client.networks == ["aaestro-net"]
    assert client.created[0]["network"] == "aaestro-net"


async def test_an_isolated_engine_publishes_nothing_and_is_reached_inside_the_network():
    """내부 네트워크에는 게시가 먹지 않는다 — control plane 은 네트워크 안 주소로 닿는다 (#280)."""
    client = FakeDockerClient(address="172.30.0.7")

    handle = await DockerEngine(client=client, network="agents", internal=True).start(spec())

    assert client.internal_requests == [True]
    assert client.created[0]["ports"] == {}
    assert client.addressed == [(handle.container_id, "agents")]
    assert (handle.control_host, handle.control_port) == ("172.30.0.7", 8080)
    assert handle.control_url == "http://172.30.0.7:8080"


async def test_extra_networks_are_joined_before_the_container_starts():
    """프록시 없는 배포의 사이드카 네트워크 (#304) — 기동 전에 붙어야 agentd 가 세션을 연다."""
    order: list[str] = []

    class Ordered(FakeDockerClient):
        def connect(self, network, container, *, aliases=()):
            order.append(f"connect {network}")
            super().connect(network, container, aliases=aliases)

        def start(self, container_id):
            order.append("start")
            super().start(container_id)

    client = Ordered()
    manifest = make_manifest()
    handle = await engine(client).start(
        build_container_spec(manifest, extra_networks=("aaestro-test-agent--mcp",))
    )

    assert client.connected == [("aaestro-test-agent--mcp", handle.container_id, ())]
    assert order == ["connect aaestro-test-agent--mcp", "start"]


async def test_an_isolated_agent_never_joins_another_network():
    """격리된 에이전트가 다른 네트워크에 붙으면 그 길로 프록시를 건너뛴다 (#304)."""
    client = FakeDockerClient()
    isolated = DockerEngine(client=client, network="agents", internal=True)

    with pytest.raises(AaestroError) as caught:
        await isolated.start(build_container_spec(make_manifest(), extra_networks=("side",)))

    assert caught.value.code == "RT_001"
    assert client.created == [] and client.connected == []


def test_extra_networks_cannot_be_shared_or_host_networks():
    with pytest.raises(AaestroError) as caught:
        build_container_spec(make_manifest(), extra_networks=("host",))

    assert caught.value.code == "RT_001"


async def test_a_plain_engine_publishes_on_loopback_and_asks_for_a_normal_network():
    client = FakeDockerClient(host_port=49153)

    handle = await engine(client).start(spec())

    assert client.internal_requests == [False]
    assert client.created[0]["ports"] == {"8080/tcp": ("127.0.0.1", None)}
    assert client.addressed == []
    assert handle.control_url == "http://127.0.0.1:49153"


async def test_a_network_with_the_wrong_isolation_is_refused_before_creating_anything():
    from aaestro.runtime.docker.errors import NetworkIsolationError

    client = FakeDockerClient(
        network_error=NetworkIsolationError("agents", expected=True, actual=False)
    )

    with pytest.raises(AaestroError) as exc_info:
        await DockerEngine(client=client, network="agents", internal=True).start(spec())

    assert (exc_info.value.code, exc_info.value.retryable) == ("RT_001", False)
    assert exc_info.value.details["network"] == "agents"
    assert client.created == []


async def test_missing_image_is_rt_004():
    """이미지 문제는 설정 문제 — 재시도해도 같다."""
    client = FakeDockerClient(image_error=RuntimeError("not found"))

    with pytest.raises(AaestroError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_004"
    assert exc_info.value.category is ErrorCategory.RUNTIME
    assert exc_info.value.retryable is False
    assert exc_info.value.details["image"]


async def test_create_failure_is_retryable_rt_001():
    """일시적 자원 부족일 수 있으므로 재시도 가능하다."""
    client = FakeDockerClient(create_error=RuntimeError("no space"))

    with pytest.raises(AaestroError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_001"
    assert exc_info.value.retryable is True


async def test_network_failure_is_rt_001():
    client = FakeDockerClient(network_error=RuntimeError("network down"))

    with pytest.raises(AaestroError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_001"


async def test_start_failure_discards_the_half_built_container():
    """반쯤 만들어진 컨테이너를 남기면 유령 컨테이너가 쌓인다."""
    client = FakeDockerClient(start_error=RuntimeError("boom"))

    with pytest.raises(AaestroError):
        await engine(client).start(spec())

    assert len(client.removed) == 1


class GatedClient(FakeDockerClient):
    """지정한 단계를 스레드 안에서 문 앞에 붙잡는다 — 취소돼도 스레드는 문이 열리면 끝까지 간다."""

    def __init__(self, step: str) -> None:
        super().__init__()
        self.step = step
        self.gate = threading.Event()
        self.entered = threading.Event()

    def _hold(self, step: str) -> None:
        if step == self.step:
            self.entered.set()
            self.gate.wait(5.0)

    def create(self, **kwargs):
        self._hold("create")
        return super().create(**kwargs)

    def start(self, container_id: str) -> None:
        self._hold("start")
        super().start(container_id)


@pytest.mark.parametrize("step", ["create", "start"])
async def test_a_cancelled_start_discards_the_container_its_thread_built(step: str):
    """#482 — 취소는 스레드를 멈추지 못한다: 끝까지 만든 컨테이너를 아무도 지우지 않았다."""
    client = GatedClient(step)
    docker = engine(client)
    starting = asyncio.create_task(docker.start(spec()))
    await until(client.entered.is_set)

    starting.cancel()
    await asyncio.sleep(0)
    assert not starting.done(), "기동이 스레드를 기다리지 않고 취소를 올렸다"
    client.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert len(client.created) == 1
    assert client.removed == ["container-0001" + "0" * 20]
    assert docker.started == {}


async def test_a_cancelled_start_hands_a_hung_cleanup_to_the_engine():
    """#482 — 걸린 Docker 호출을 그 자리에서 끝없이 기다리면 정지가 묶인다: 상한을 넘기면 넘긴다."""
    client = GatedClient("create")
    docker = DockerEngine(client=client, cancel_wait_s=0.01)
    starting = asyncio.create_task(docker.start(spec()))
    await until(client.entered.is_set)

    try:
        starting.cancel()
        await until(starting.done, timeout_s=1.0)
        assert starting.cancelled()
        assert docker._cleanups, "넘긴 정리를 엔진이 쥐지 않았다"
    finally:
        client.gate.set()

    await docker.settle_cleanups()
    assert client.removed == ["container-0001" + "0" * 20]
    assert not docker._cleanups


async def test_settling_abandons_a_cleanup_that_never_finishes():
    """종료는 끝없이 기다리지 않는다 — 상한을 넘긴 정리는 끊고 넘어간다."""
    client = GatedClient("create")
    docker = DockerEngine(client=client, cancel_wait_s=0.01)
    starting = asyncio.create_task(docker.start(spec()))
    await until(client.entered.is_set)

    try:
        starting.cancel()
        await until(starting.done, timeout_s=1.0)
        await docker.settle_cleanups(timeout_s=0.01)
        assert not docker._cleanups
        assert client.removed == []
    finally:
        client.gate.set()


async def test_failed_start_is_not_tracked():
    client = FakeDockerClient(start_error=RuntimeError("boom"))
    docker = engine(client)

    with pytest.raises(AaestroError):
        await docker.start(spec())

    assert docker.started == {}


# --- OOM 감지 -----------------------------------------------------------------


async def test_oom_kill_is_rt_003():
    """OOM 은 메모리 상한을 올리지 않으면 재시도해도 같은 결과다."""
    client = FakeDockerClient(state={"OOMKilled": True, "ExitCode": 137})
    docker = engine(client)
    handle = await docker.start(spec())

    with pytest.raises(AaestroError) as exc_info:
        await docker.check_exit(handle)

    assert exc_info.value.code == "RT_003"
    assert exc_info.value.retryable is False


async def test_oom_exit_code_alone_is_detected():
    """OOMKilled 플래그가 없어도 137 은 OOM 으로 본다."""
    client = FakeDockerClient(state={"OOMKilled": False, "ExitCode": 137})
    docker = engine(client)
    handle = await docker.start(spec())

    with pytest.raises(AaestroError) as exc_info:
        await docker.check_exit(handle)

    assert exc_info.value.code == "RT_003"


async def test_clean_exit_is_not_an_error():
    client = FakeDockerClient(state={"OOMKilled": False, "ExitCode": 0})
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.check_exit(handle)


# --- drain -------------------------------------------------------------------


async def test_drain_returns_when_work_finishes():
    docker = engine()
    handle = await docker.start(spec())
    remaining = [2, 1, 0]
    drained = []

    async def sleep(_delay: float) -> None:
        remaining.pop(0)

    await docker.drain(
        handle,
        request_drain=lambda: drained.append(True),
        inflight=lambda: remaining[0],
        sleep=sleep,
        clock=lambda: 0.0,
    )

    assert drained == [True]


async def test_drain_awaits_an_async_request():
    docker = engine()
    handle = await docker.start(spec())
    called = []

    async def request() -> None:
        called.append(True)

    await docker.drain(handle, request_drain=request, inflight=lambda: 0, clock=lambda: 0.0)

    assert called == [True]


async def test_drain_timeout_is_rt_005():
    """남은 태스크를 조용히 버리지 않는다."""
    docker = engine()
    handle = await docker.start(spec())
    ticks = iter([0.0, 10.0, 40.0, 80.0])

    async def sleep(_delay: float) -> None:
        pass

    with pytest.raises(AaestroError) as exc_info:
        await docker.drain(
            handle,
            request_drain=lambda: None,
            inflight=lambda: 1,
            timeout_s=30.0,
            sleep=sleep,
            clock=lambda: next(ticks),
        )

    assert exc_info.value.code == "RT_005"
    assert exc_info.value.details["inflight"] == 1


# --- 정지 --------------------------------------------------------------------


async def test_stop_sends_the_grace_period():
    client = FakeDockerClient()
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert client.stopped == [(handle.container_id, 30.0)]
    assert client.removed == [handle.container_id]


async def test_stop_untracks_the_agent():
    docker = engine()
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert docker.started == {}


async def test_stop_survives_a_failing_stop():
    """정지 실패가 정리를 막으면 유령 컨테이너가 남는다."""
    client = FakeDockerClient(stop_error=RuntimeError("already gone"))
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert client.removed == [handle.container_id]


async def test_stop_survives_a_failing_removal():
    client = FakeDockerClient(remove_error=RuntimeError("in use"))
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert docker.started == {}


async def test_stop_can_keep_the_container():
    client = FakeDockerClient()
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.stop(handle, remove=False)

    assert client.removed == []


def test_handle_short_id_is_truncated():
    handle = ContainerHandle(
        agent="a", container_id="0123456789abcdef0123", image="i", control_port=1
    )

    assert handle.short_id == "0123456789ab"


async def test_drain_awaits_a_future_request():
    """coroutine 이 아닌 awaitable 도 기다려야 한다 — 건너뛰면 에이전트가
    아직 태스크를 받는 중인데 drain 이 진행된다."""
    docker = engine()
    handle = await docker.start(spec())
    completed = []

    def request():
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        completed.append(True)
        return future

    await docker.drain(handle, request_drain=request, inflight=lambda: 0, clock=lambda: 0.0)

    assert completed == [True]


async def test_removal_failure_log_carries_agent_and_image(monkeypatch):
    """제거 실패 로그에 소유자와 이미지가 없으면 사고 추적이 끊긴다 (05 필수 필드)."""
    recorded: list[dict] = []

    def capture(_event: str, **fields: object) -> None:
        recorded.append(dict(fields))

    from aaestro.runtime.docker import engine as engine_module

    monkeypatch.setattr(engine_module.log, "warning", capture)

    client = FakeDockerClient(start_error=RuntimeError("boom"), remove_error=RuntimeError("in use"))

    with pytest.raises(AaestroError):
        await engine(client).start(spec())

    assert recorded[-1]["agent"] == "test-agent"
    assert recorded[-1]["image"]
    assert recorded[-1]["container_id"]


async def test_drain_success_log_includes_the_image(monkeypatch):
    """컨테이너 조작 로그는 container_id 와 image 를 모두 실어야 한다."""
    recorded: list[dict] = []

    def capture(_event: str, **fields: object) -> None:
        recorded.append(dict(fields))

    from aaestro.runtime.docker import engine as engine_module

    monkeypatch.setattr(engine_module.log, "info", capture)

    docker = engine()
    handle = await docker.start(spec())
    recorded.clear()

    await docker.drain(handle, request_drain=lambda: None, inflight=lambda: 0, clock=lambda: 0.0)

    assert recorded[-1]["image"] == handle.image
    assert recorded[-1]["container_id"] == handle.short_id
