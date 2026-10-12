"""Automatic restart of an unhealthy agent.

`RestartPolicy` 와 `plan_restart()`(상한 초과 시 `RT_008`)는 구현되어 있었는데
**부르는 곳이 없었다** (#215). #213 이 상태를 `UNHEALTHY` 까지 옮겼고, 여기서
그 다음을 잇는다.

06 에 따라 실제로 자지 않는다 — backoff 대기를 주입해 기록만 한다.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from prometheus_client import CollectorRegistry

from aaestro.core.agent import HealthState, HealthStatus
from aaestro.core.manifest import AgentManifest
from aaestro.observability.metrics import Metrics
from aaestro.runtime.docker.engine import DockerEngine
from aaestro.runtime.launcher import AgentLauncher
from aaestro.runtime.lifecycle import AgentState, RestartPolicy
from tests.fixtures.fake_docker import FakeDockerClient
from tests.fixtures.waiting import spin, until

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def always_sick(monkeypatch: pytest.MonkeyPatch) -> None:
    """모든 Control 클라이언트를 결정적으로 아프게 만든다.

    재기동된 레플리카는 launcher 가 **새로** 만드는 클라이언트를 쓰므로,
    인스턴스만 갈아끼우면 그 뒤가 실제 네트워크를 두드린다 — Control API
    재시도가 실제로 자면서(#217) 테스트가 실시간에 묶인다.
    """
    from aaestro.runtime.control import ControlClient

    async def sick(self: ControlClient) -> HealthStatus:
        return HealthStatus(status=HealthState.UNHEALTHY)

    monkeypatch.setattr(ControlClient, "health", sick)


def manifest() -> AgentManifest:
    return AgentManifest.model_validate(
        yaml.safe_load((REPO_ROOT / "agents" / "echo" / "manifest.yaml").read_text("utf-8"))
    )


class Recorder:
    """대기를 기록만 하는 sleep 대역 — 실제로 자지 않는다."""

    def __init__(self) -> None:
        self.waits: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)


def launcher(client: FakeDockerClient, waits: Recorder, **kwargs) -> AgentLauncher:
    """health 는 매번 통과, 재시작 backoff 만 기록한다."""

    async def health_sleep(_delay: float) -> None:
        await asyncio.sleep(0)

    kwargs.setdefault("health_interval_s", 10.0)
    # 이 파일은 **기동 유예가 끝난 뒤**의 재시작 계약을 본다 — 유예 자체는
    # `test_the_startup_grace_does_not_restart_a_container_that_is_still_starting` 이 본다 (#297)
    kwargs.setdefault("startup_grace_s", 0.0)
    return AgentLauncher(
        engine=DockerEngine(client=client),
        health_sleep=health_sleep,
        restart_sleep=waits,
        **kwargs,
    )


async def start_sick(agents: AgentLauncher) -> tuple:
    """기동한다 — probe 는 fixture 가 클래스 수준에서 아프게 해 두었다."""
    launched = await agents.start(manifest())
    return launched, None


async def restart_now(agents: AgentLauncher, launched) -> None:
    """재시작을 **직접** 돌린다.

    감시 루프를 기다리면 event loop 회차에 묶여, 스위트 전체가 돌 때 경합한다.
    루프가 실제로 트리거하는지는 아래 `test_the_health_loop_triggers_a_restart`
    가 따로 본다 — 여기서는 재시작 자체의 계약을 결정적으로 확인한다.
    """
    launched.lifecycle.record_health(healthy=False, threshold=1)
    await agents._restart(launched)


async def test_an_unhealthy_agent_is_restarted():
    """#215 — RestartPolicy 를 부르는 곳이 없어 아픈 에이전트가 방치됐다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder())
    launched, _ = await start_sick(agents)

    await restart_now(agents, launched)

    assert len(client.created) > 1, "컨테이너가 다시 세워지지 않았다"
    await agents.stop_all()


async def test_a_restart_keeps_the_baked_image():
    """#266 — 재시작만 base 이미지로 돌면 커스텀 실행기가 조용히 사라진다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder())
    launched = await agents.start(manifest(), image="aaestro/agent-echo-baked:0.1.0")

    await restart_now(agents, launched)

    images = [created["image"] for created in client.created]
    assert images == ["aaestro/agent-echo-baked:0.1.0"] * 2
    await agents.stop_all()


async def test_the_startup_grace_does_not_restart_a_container_that_is_still_starting():
    """기동이 확인 주기보다 길면 뜨는 중인 컨테이너가 재시작돼 배포가 끝내 실패한다 (#297)."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder(), startup_grace_s=1_000.0)
    launched, _ = await start_sick(agents)

    await spin(20)

    assert len(client.created) == 1, "유예 안에서 재시작했다"
    assert launched.lifecycle.state is AgentState.STARTING
    assert launched.lifecycle.consecutive_health_failures > 0, "실패를 세지도 않았다"
    await agents.stop_all()


async def test_the_health_loop_triggers_a_restart():
    """감시 루프가 Unhealthy 를 보면 재시작을 건다 — 이 배선이 #215 의 핵심이다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder())
    await start_sick(agents)

    await until(lambda: len(client.created) > 1)

    assert len(client.created) > 1
    await agents.stop_all()


async def test_the_restart_waits_for_the_backoff():
    """02 Rule 6 — 즉시 다시 세우면 crash loop 이 그대로 반복된다."""
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    launched, _ = await start_sick(agents)

    await restart_now(agents, launched)

    assert waits.waits == [RestartPolicy().initial_delay_s]
    await agents.stop_all()


async def test_the_restart_counter_is_filled():
    """05 의 `ContainerRestartLoop` 알림은 이 카운터를 본다."""
    metrics = Metrics(registry=CollectorRegistry())
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits, metrics=metrics)
    launched, _ = await start_sick(agents)

    await restart_now(agents, launched)

    counter = metrics.counter("aaestro_container_restarts_total").labels(
        agent="echo", reason="unhealthy"
    )
    assert counter._value.get() == 1.0
    await agents.stop_all()


async def test_the_restart_ceiling_marks_the_agent_failed():
    """02 Rule 6 — 상한을 넘으면 Failed 로 가고 재시작을 멈춘다 (`RT_008`)."""
    client = FakeDockerClient()
    waits = Recorder()
    agents = launcher(client, waits)
    launched, _ = await start_sick(agents)
    # `should_give_up` 은 **초과**(>)를 본다 — 상한 0 이면 첫 시도에서
    # 포기하므로 재시작 회차를 기다리지 않고 그 경로를 직접 태운다
    launched.lifecycle.restart_policy = RestartPolicy(max_in_window=0)

    await until(lambda: launched.lifecycle.state is AgentState.FAILED)

    assert launched.lifecycle.state is AgentState.FAILED
    await agents.stop_all()


async def test_the_restart_window_survives_the_restart():
    """재시작이 lifecycle 을 **이어받아야** crash-loop 상한이 성립한다.

    새로 만들면 `RestartPolicy` 의 창(window)이 매번 리셋되어
    `should_give_up()` 이 영원히 참이 되지 않는다 — 무한히 되살아난다.

    감시 루프가 걸어 주기를 기다리지 않고 재시작을 **직접** 돌린다 —
    교체 도중에는 `replicas_of` 가 잠시 비므로, 그 틈을 폴링으로 넘겨다보면
    빈 목록을 집는다. 루프가 실제로 트리거하는지는
    `test_the_health_loop_triggers_a_restart` 가 따로 본다.
    """
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    launched, _ = await start_sick(agents)
    before = launched.lifecycle

    await restart_now(agents, launched)

    revived = agents.replicas_of("echo")[0]
    assert revived.lifecycle is before, "재시작이 lifecycle 을 새로 만들면 상한이 리셋된다"
    assert before.restart_policy.restarts_in_window() == 1
    await agents.stop_all()


async def test_a_failed_agent_stops_being_watched():
    """포기한 에이전트를 계속 두드리면 runtime 이 그 대기에 묶인다."""
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    launched, _ = await start_sick(agents)
    launched.lifecycle.restart_policy = RestartPolicy(max_in_window=0)

    await restart_now(agents, launched)

    # 감시가 남아 있으면 포기한 에이전트를 계속 두드린다
    assert not agents._monitors


async def test_routing_skips_an_unhealthy_replica():
    """재시작 중인 레플리카로 보내면 그 태스크가 전부 실패한다."""
    waits = Recorder()
    agents = launcher(FakeDockerClient(), waits)
    healthy = await agents.start(manifest(), replica=0)
    healthy.client.health = _always_well  # type: ignore[method-assign]
    sick, _ = await start_sick_replica(agents, replica=1)

    await until(lambda: not sick.lifecycle.accepts_tasks)

    assert all(agents.route("echo") is healthy.client for _ in range(3))
    await agents.stop_all()


async def _always_well() -> HealthStatus:
    return HealthStatus(status=HealthState.HEALTHY)


async def start_sick_replica(agents: AgentLauncher, *, replica: int) -> tuple:
    launched = await agents.start(manifest(), replica=replica)
    return launched, None


class HangingSleep(Recorder):
    """대기를 기록하고 **영원히 붙잡는다** — 재시작이 끝나지 않게 고정한다."""

    def __init__(self) -> None:
        super().__init__()
        self._blocked = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.waits.append(delay)
        await self._blocked.wait()


async def test_restart_is_not_scheduled_twice():
    """감시 루프는 Unhealthy 를 **매 확인마다** 보고한다 — 겹쳐 걸면 컨테이너가 난립한다.

    재시작을 backoff 에서 붙잡아 두면, 그 사이 들어오는 보고가 새 재시작을
    거는지 대기 횟수로 바로 드러난다.
    """
    waits = HangingSleep()
    agents = launcher(FakeDockerClient(), waits)
    await start_sick(agents)

    await until(lambda: bool(waits.waits))
    await spin(200)

    assert waits.waits == [RestartPolicy().initial_delay_s]


async def test_restart_is_off_without_supervision():
    """감시를 켜지 않은 조립은 재시작도 하지 않는다 — 루프의 소유자가 없다."""
    agents = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))

    await agents.start(manifest())
    await spin(30)

    assert not agents._restarts


async def test_a_reattached_agent_is_restarted_without_waiting_for_a_startup_grace():
    """재부착은 이미 Ready 였던 기록만 붙인다 — 유예를 다시 주면 고장이 늦게 드러난다."""
    client = FakeDockerClient()
    agents = launcher(client, Recorder(), startup_grace_s=1_000.0)
    first = await agents.start(manifest())
    await agents.stop_all()

    adopted = await agents.adopt(
        first.agent,
        replica=0,
        container_id=first.handle.container_id,
        image=first.handle.image,
        control_port=first.handle.control_port,
        token="remembered",  # noqa: S106 — 테스트 값
        restart_args={"manifest": manifest()},
    )

    assert adopted is True
    await until(lambda: len(client.created) > 1)
    await agents.stop_all()
