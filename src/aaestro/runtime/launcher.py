"""Agent launch — spec, token, and client in one place.

에이전트 기동의 조립 지점. 컨테이너 스펙 생성 · 토큰 발급 · Control API
클라이언트 구성을 **한 곳에서** 수행한다.

흩어져 있으면 주입한 토큰과 호출에 싣는 토큰이 어긋나 컨테이너는 떴는데
모든 호출이 401 이 되는 상태가 만들어진다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog

from aaestro.core.errors import NETWORK_RETRY, AaestroError, ErrorCategory, ErrorCode
from aaestro.memory.client import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
from aaestro.runtime.control import ControlClient
from aaestro.runtime.docker.engine import DEFAULT_DRAIN_TIMEOUT_S, ContainerHandle
from aaestro.runtime.health import DEFAULT_STARTUP_GRACE_S
from aaestro.runtime.lifecycle import AgentLifecycle, AgentState
from aaestro.runtime.ports import A2APortAllocator
from aaestro.runtime.spec import LOOPBACK, build_container_spec
from aaestro.runtime.tokens import TokenIssuer, authenticated_env

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping, Sequence

    from aaestro.core.manifest import AgentManifest
    from aaestro.observability.metrics import Metrics
    from aaestro.runtime.docker.engine import ContainerHandle, DockerEngine

log = structlog.get_logger(__name__)

RESTART_REASONS = ("unhealthy", "failed", "gave_up")
"""`aaestro_container_restarts_total{reason}` 의 값 — 교체됨 · 교체 기동 실패 · 상한 도달."""


@dataclass
class LaunchedAgent:
    """A started agent and the client that can reach it.

    기동된 에이전트와 그것에 닿을 수 있는 클라이언트. 토큰은 이미 양쪽에
    반영되어 있다.
    """

    agent: str
    handle: ContainerHandle
    client: ControlClient
    lifecycle: AgentLifecycle = field(default_factory=lambda: AgentLifecycle(agent=""))
    """이 레플리카의 02 lifecycle 상태 — 기동·health·정지가 여기로 모인다."""
    # 포트 회수는 **어느 레플리카였는지** 알아야 한다 — 기동 시점의 값을
    # 들고 있지 않으면 정지가 엉뚱한 레플리카의 포트를 놓아준다
    replica: int = 0
    restart_args: dict[str, Any] = field(default_factory=dict, repr=False)
    a2a_port: int | None = None
    """할당받은 A2A 포트 — 배포 기록이 재시작 뒤 같은 포트를 돌려줘야 한다 (#243)."""
    """재기동에 필요한 인자 — 컨테이너를 새로 만들려면 원래 선언이 있어야 한다."""
    stopped: bool = False
    """이 handle 의 컨테이너를 이미 정지·제거했다 — 실패한 교체가 되돌린 레플리카를
    다음 재시도가 또 정지시켜 이미 없는 컨테이너에 대한 거짓 WARN 을 남기지 않게 막는다."""

    async def aclose(self) -> None:
        """클라이언트 연결을 정리한다."""
        await self.client.aclose()


async def cancel_and_wait(task: asyncio.Task[Any]) -> None:
    """Cancel a task and wait for it, re-raising a cancellation aimed at the waiter.

    다른 태스크를 끊고 끝날 때까지 기다린다. 삼키는 것은 **끊은 태스크의** `CancelledError`
    뿐이다 — 기다리는 쪽이 받은 취소까지 삼키면 `wait_for(stop(...))` 의 timeout 이 사라지고,
    정지는 끊은 태스크가 끝날 때까지 조용히 묶인다 (#482). 그 밖의 예외는 그대로 올린다.
    """
    if task.done():
        return
    task.cancel()
    current = asyncio.current_task()
    before = current.cancelling() if current is not None else 0
    try:
        await task
    except asyncio.CancelledError:
        if current is not None and current.cancelling() > before:
            raise


@dataclass(frozen=True)
class MemoryEndpoint:
    """Where an agent reaches the Memory Service, and with what token.

    저장소 자격증명이 아니라 **주소와 불투명 토큰**만 담는다 — 그것이 컨테이너에
    들어가도 되는 전부다.
    """

    url: str
    token: str


def _with_memory(env: dict[str, str], memory: MemoryEndpoint | None) -> dict[str, str]:
    """메모리 접속 정보를 환경에 합친다 — 없으면 그대로 둔다."""
    if memory is None:
        return env
    return {**env, MEMORY_URL_ENV: memory.url, MEMORY_TOKEN_ENV: memory.token}


@dataclass
class AgentLauncher:
    """Starts agents with authentication wired end to end.

    인증이 양쪽에 배선된 채로 에이전트를 기동한다.

    Attributes:
        engine: Creates and starts the container.
        issuer: Mints the token injected into the container and presented by
            the client — **같은 발급자**여야 둘이 일치한다.
        ports: Allocates the A2A port from the runtime's range (03 Rule 2).
            None 이면 포트를 배정하지 않는다 — A2A 를 쓰지 않는 배포와,
            포트를 밖에서 정해 넘기는 테스트가 그 경우다.
    """

    engine: DockerEngine
    issuer: TokenIssuer = field(default_factory=TokenIssuer)
    ports: A2APortAllocator | None = None
    metrics: Metrics | None = None
    """`aaestro_agent_health` 를 채울 registry — 미주입 시 계측하지 않는다."""
    health_interval_s: float | None = None
    """health 폴링 주기. **None 이면 감시하지 않는다** — 루프의 소유자가
    분명해야 하므로 감시를 원하는 조립만 켠다 (02 Lifecycle Rules 3)."""
    health_sleep: Callable[[float], object] | None = None
    """06 은 시간 의존 로직이 테스트에서 실제로 자는 것을 금지한다."""
    startup_grace_s: float = DEFAULT_STARTUP_GRACE_S
    """Ready 가 되기 전의 유예 — 이 안의 health 실패는 재시작을 일으키지 않는다 (#297)."""
    drain_timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S
    """정지 전 진행 중 태스크를 기다리는 상한 (02 Lifecycle 4, 기본 30s)."""
    launched: dict[tuple[str, int], LaunchedAgent] = field(default_factory=dict)
    _cursors: dict[str, int] = field(default_factory=dict, init=False)
    _monitors: dict[tuple[str, int], asyncio.Task[None]] = field(default_factory=dict, init=False)
    _restarts: dict[tuple[str, int], asyncio.Task[None]] = field(default_factory=dict, init=False)
    restart_sleep: Callable[[float], Awaitable[None]] | None = None
    """backoff 대기 — 06 은 테스트가 실제로 자는 것을 금지한다."""
    """기동된 레플리카 — 키는 **(에이전트, replica)** 다.

    이름만으로 잡으면 두 번째 레플리카가 첫 핸들을 덮어 컨테이너를 미아로
    만든다. 01 Scalability 는 동일 manifest 의 N replica 기동을 규정한다.
    """

    async def start(
        self,
        manifest: AgentManifest,
        *,
        secrets: Mapping[str, str] | None = None,
        replica: int = 0,
        a2a_port: int | None = None,
        memory: MemoryEndpoint | None = None,
        lifecycle: AgentLifecycle | None = None,
        mounts: Sequence[Mapping[str, Any]] = (),
        image: str | None = None,
        networks: Sequence[str] = (),
    ) -> LaunchedAgent:
        """Start one agent with its token injected and wired.

        토큰을 주입하고 클라이언트에 물린 채로 에이전트를 기동합니다.

        Args:
            manifest: The validated agent manifest.
            secrets: Secret values resolved from the scope chain.
            replica: Replica index for the container name.
            memory: Memory Service address and this agent's opaque token —
                **DB 자격증명은 컨테이너에 넣지 않는다** (09 Access Enforcement 1).
            a2a_port: A2A port to use. 생략하면 할당기가 범위에서 고른다 —
                03 은 포트를 runtime 이 준다고 규정한다.
            mounts: Read-only binds carrying declarations into the image
                (`build_container_spec`). 재시작에도 같은 것을 다시 건다.
            image: Baked image for a custom agent (#266). 재시작에도 같은 이미지로
                다시 세운다 — 빠뜨리면 재시작한 컨테이너만 base 이미지로 돈다.
            networks: 더 붙을 네트워크 — 이그레스 프록시 없는 배포의 사이드카 네트워크 (#304).
                재시작에도 같은 것을 다시 건다.
            lifecycle: 이어붙일 상태. **재시작은 반드시 넘겨야 한다** — 새로
                만들면 `RestartPolicy` 의 창(window)이 리셋되어 crash-loop
                상한(02 Rule 6)이 영원히 걸리지 않는다.

        Returns:
            The launched agent — its client already carries the token.

        Raises:
            AaestroError: RUNTIME/``RT_010`` if this replica is already launched —
                호출자가 겹치는 배포를 해체하면 되는 상태 충돌입니다 (HTTP 409).
        """
        agent = manifest.name
        # 같은 **레플리카**를 두 번 띄우면 첫 핸들을 덮어 컨테이너가 미아가 된다.
        # 다른 레플리카는 별개의 컨테이너이므로 허용한다 (01 Scalability)
        if (agent, replica) in self.launched:
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_010,
                message="agent replica is already launched",
                agent=agent,
                details={"replica": replica},
            )

        self._prime_restart_counter(agent)

        # 03 Rule 2 — 포트는 runtime 이 준다. 호출자가 명시하면 그것을 존중하고,
        # 아니면 범위에서 할당한다. A2A 가 꺼진 에이전트는 범위를 소비하지 않는다
        if a2a_port is None and self.ports is not None and manifest.spec.a2a.enabled:
            a2a_port = self.ports.allocate(agent, replica=replica)

        spec = build_container_spec(
            manifest,
            env=_with_memory(authenticated_env(self.issuer, agent, secrets), memory),
            replica=replica,
            a2a_port=a2a_port,
            network=self.engine.network,
            mounts=mounts,
            image=image,
            extra_networks=networks,
        )

        # 02 Lifecycle — 이미지는 배포 파이프라인이 굽는다 (Rule 1). runtime 이
        # 보는 것은 Built 이후다
        if lifecycle is None:
            lifecycle = AgentLifecycle(agent=agent)
            lifecycle.transition(AgentState.BUILT)
        # 재시작은 Unhealthy/Failed 에서 다시 올라온다 — 두 전이 모두 허용된다
        lifecycle.transition(AgentState.STARTING)

        handle = await self.engine.start(spec)
        client = ControlClient(
            handle.control_url,
            agent=agent,
            # 05 Retry Layering — runtime 이 재시도 주체다 (읽기만)
            retry=NETWORK_RETRY,
            # 컨테이너에 주입한 것과 같은 토큰 — 발급자가 하나이므로 일치한다
            token=self.issuer.issue(agent),
        )

        launched = LaunchedAgent(
            agent=agent,
            handle=handle,
            client=client,
            replica=replica,
            lifecycle=lifecycle,
            restart_args={
                "manifest": manifest,
                "secrets": secrets,
                "memory": memory,
                "mounts": mounts,
                "image": image,
                "networks": networks,
            },
            a2a_port=a2a_port,
        )
        self.launched[agent, replica] = launched
        self._watch(launched)
        log.info(
            "agent launched",
            agent=agent,
            container_id=handle.short_id,
            image=handle.image,
            port=handle.control_port,
        )
        return launched

    def _watch(self, launched: LaunchedAgent, *, ready_once: bool = False) -> None:
        """Start this replica's health loop, when supervision is on.

        02 Lifecycle Rules 3 — Ready 인 에이전트는 주기적으로 확인한다.

        **첫 성공이 Ready 로 올린다**: 02 Rule 2 는 "기동 → initialize() →
        health OK 가 되어야 그래프에 attach" 라고 규정한다. 기동만으로 Ready 를
        선언하면 initialize 가 끝내 실패한 컨테이너가 태스크를 받는다.
        기동 성공 판정은 **runtime 이** 하므로 monitor 가 아니라 여기서 올린다.
        """
        if self.health_interval_s is None:
            return

        from aaestro.runtime.health import HealthMonitor

        monitor = HealthMonitor(
            agent=launched.agent,
            probe=launched.client,
            lifecycle=launched.lifecycle,
            interval_s=self.health_interval_s,
            # 기동이 확인 주기보다 길어도 뜨는 중인 컨테이너를 재시작하지 않는다 (#297)
            startup_grace_s=self.startup_grace_s,
            ready_once=ready_once,
            metrics=self.metrics,
            sleep=self.health_sleep,
            on_state=lambda state, healthy: self._promote(launched, state, healthy=healthy),
        )
        self._monitors[launched.agent, launched.replica] = asyncio.create_task(
            self._poll(launched, monitor)
        )

    async def _poll(self, launched: LaunchedAgent, monitor: Any) -> None:
        """Drive one replica's health loop until it is stopped.

        레플리카 하나의 health 루프를 정지될 때까지 돌립니다.

        루프가 예외로 죽으면 그 레플리카는 **영원히 감시되지 않는다** —
        조용히 사라지지 않도록 남기고, 다른 레플리카는 계속 돈다.
        """
        try:
            await monitor.run()
        except asyncio.CancelledError:
            raise
        except Exception as err:
            log.error(
                "agent health loop stopped",
                agent=launched.agent,
                container_id=launched.handle.short_id,
                exc_info=err,
            )

    def _schedule_restart(self, launched: LaunchedAgent) -> None:
        """Kick off this replica's restart, once.

        재시작을 **한 번만** 건다: 감시 루프는 계속 돌아 Unhealthy 를 반복해서
        보고하므로, 진행 중인 재시작이 있으면 겹쳐 걸지 않는다.
        """
        key = (launched.agent, launched.replica)
        running = self._restarts.get(key)
        if running is not None and not running.done():
            return
        self._restarts[key] = asyncio.create_task(self._restart(launched))

    async def _restart(self, launched: LaunchedAgent) -> None:
        """Replace an unhealthy replica's container after a backoff, until it stands.

        02 Lifecycle Rules 6 — Unhealthy 는 exponential backoff 로 재시작하고,
        상한을 넘으면 Failed 로 간다.

        **재기동이 실패해도 이 태스크 안에서 다시 시도한다** (#482): 교체는 감시 루프를
        먼저 멈추므로, 실패한 뒤 "다음 health 확인" 은 오지 않는다. 재시도마다 같은
        lifecycle 의 `plan_restart` 를 거치므로 실패한 재기동도 창 안의 재시작으로 세어
        crash-loop 상한이 걸린다. 루프는 `_restarts` 가 쥔 이 태스크 하나라 정지가
        끊을 수 있다 (07 Async 5).

        `tenacity` 를 쓰지 않는다: 대기와 포기의 판단 주체는 lifecycle 의 `RestartPolicy` 다
        (창 안의 횟수 — 이 연쇄 밖의 재시작도 센다). 재시도기를 하나 더 두면 상한이 두 곳에서
        갈린다 (05 Retry Layering).

        **재시작은 runtime 계층에서만** 한다 (05 Retry Layering): 노드 재시도나
        Control API 재시도와 곱해지면 한 번의 장애가 여러 겹으로 늘어난다.
        """
        agent = launched.agent
        delay = self._plan_restart(launched)
        if delay is None:
            # 포기한 레플리카를 계속 두드리지 않는다 — 실패한 교체 뒤에는 감시가 이미 없다
            await self._unwatch(launched)
            return
        attempt = 1
        while True:
            await self._sleep_restart(delay)
            try:
                await self._replace(launched)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                # runtime 은 살아 있어야 한다 — 실패를 세고 다음 backoff 로 넘어간다
                self._record_restart(agent, reason="failed")
                delay = self._plan_restart(launched, last_error=err)
                if delay is None:
                    return
                # 05 Retry Rule 4 — delay_ms 는 **다음** 시도 전의 대기다
                log.warning(
                    "agent restart failed; retrying",
                    agent=agent,
                    container_id=launched.handle.short_id,
                    image=launched.handle.image,
                    attempt=attempt,
                    max_attempts=launched.lifecycle.restart_policy.max_in_window,
                    delay_ms=round(delay * 1000),
                    error_code=getattr(err, "code", None),
                    exc_info=err,
                )
                attempt += 1
                continue

            revived = self.launched[agent, launched.replica]
            log.info(
                "agent restarted",
                agent=agent,
                container_id=revived.handle.short_id,
                image=revived.handle.image,
                attempt=attempt,
                delay_ms=round(delay * 1000),
            )
            self._record_restart(agent, reason="unhealthy")
            return

    def _plan_restart(
        self, launched: LaunchedAgent, *, last_error: BaseException | None = None
    ) -> float | None:
        """다음 재시작 전 대기 — 상한을 넘었으면 None 이다 (Failed 로 옮기고 알린다).

        `last_error` 는 직전 재기동의 실패 — 포기 로그가 원인을 잃지 않게 싣는다.
        """
        try:
            return launched.lifecycle.plan_restart()
        except AaestroError as err:
            # RT_008 — 상한 초과. lifecycle 이 이미 Failed 로 옮겼다
            log.error(
                "agent restart limit exceeded",
                agent=launched.agent,
                container_id=launched.handle.short_id,
                image=launched.handle.image,
                error_code=ErrorCode.RT_008,
                # 직전 재기동 실패의 원인 — None 이면 이번 연쇄의 첫 계획에서 포기했다
                last_error_code=getattr(last_error, "code", None),
                exc_info=err,
            )
            self._record_restart(launched.agent, reason="gave_up")
            return None

    async def _sleep_restart(self, delay: float) -> None:
        """backoff 대기 — 06 은 테스트가 실제로 자는 것을 금지하므로 주입을 받는다."""
        sleeper = self.restart_sleep or asyncio.sleep
        await sleeper(delay)

    async def _stop_container(self, launched: LaunchedAgent) -> None:
        """컨테이너를 한 번만 정지·제거한다 (멱등).

        실패한 `_replace` 가 되돌린 레플리카는 같은 handle 로 다시 `_restart` 재시도나
        `stop()` 을 거칠 수 있다 — 이미 없는 컨테이너에 또 `engine.stop` 을 보내면
        `client.stop`/`client.remove` 가 매번 실패해 거짓 WARN 만 쌓인다 (#491).
        """
        if launched.stopped:
            return
        await launched.aclose()
        await self.engine.stop(launched.handle)
        launched.stopped = True

    async def _replace(self, launched: LaunchedAgent) -> None:
        """정지한 자리에 같은 선언으로 컨테이너를 다시 세운다.

        실패하면 **원래 레플리카를 자리에 되돌리고** Unhealthy 로 둔다: 자리를 비우면
        `stop` 이 이 레플리카를 찾지 못해 재시도를 끊지 못하고, 배포를 읽는 쪽은 아무
        일도 없는 것으로 본다 (#482).
        """
        agent, replica = launched.agent, launched.replica
        await self._unwatch(launched)
        replaced = False
        try:
            await self._stop_container(launched)
            del self.launched[agent, replica]
            # A2A 포트는 **놓지 않는다**: peer 들의 env 에 이 포트가 굳어 있다 (#243 배선).
            # 할당기의 예약은 그대로 두고 같은 번호를 명시해 넘기면 start 가 재할당하지 않는다

            args = dict(launched.restart_args)
            # **같은 lifecycle 을 이어붙인다**: 새로 만들면 재시작 횟수가 리셋되어
            # crash-loop 상한이 영원히 걸리지 않는다
            await self.start(
                args.pop("manifest"),
                replica=replica,
                lifecycle=launched.lifecycle,
                a2a_port=launched.a2a_port,
                **args,
            )
            replaced = True
        finally:
            # 실패와 취소 모두 — 취소된 교체도 정지가 이 레플리카를 정리해야 한다
            if not replaced:
                self.launched.setdefault((agent, replica), launched)
                # start 가 STARTING 으로 올린 뒤 넘어졌다 — 뜨는 중으로 보이면 안 된다
                if launched.lifecycle.state is AgentState.STARTING:
                    launched.lifecycle.transition(AgentState.UNHEALTHY)

    def _prime_restart_counter(self, agent: str) -> None:
        """이 에이전트의 재시작 사유를 0 으로 미리 만든다.

        `increase()` 는 처음 나타난 계열의 첫 값을 증가로 보지 않는다 — 빠른 기동 실패 루프는
        계열이 생기자마자 상한에 닿아, `ContainerRestartLoop` 가 조용히 울리지 않는다.
        """
        if self.metrics is None:
            return
        counter = self.metrics.counter("aaestro_container_restarts_total")
        for reason in RESTART_REASONS:
            counter.labels(agent=agent, reason=reason)

    def _record_restart(self, agent: str, *, reason: str) -> None:
        """05 의 `ContainerRestartLoop` 알림이 이 카운터를 본다."""
        if self.metrics is None:
            return
        self.metrics.counter("aaestro_container_restarts_total").labels(
            agent=agent, reason=reason
        ).inc()

    async def _cancel_restarts(self, agent: str, *, replica: int | None) -> None:
        """이 에이전트(레플리카)의 재시작을 끊는다 — 정지한 자리에 다시 세우지 않도록.

        목록이 아니라 **재시작 키로** 찾는다: 교체는 기동을 기다리는 동안 레플리카를
        잠시 목록에서 뺀다. 그 틈의 정지가 목록만 보면 재시작을 놓쳐, 정지한 자리에
        컨테이너가 다시 선다.
        """
        keys = [
            key
            for key in self._restarts
            if key[0] == agent and (replica is None or key[1] == replica)
        ]
        for key in keys:
            await self._cancel_restart(key)

    async def _cancel_restart(self, key: tuple[str, int]) -> None:
        """재시작 태스크 하나를 끊고 끝날 때까지 기다린다.

        끝난 뒤에야 목록에서 뺀다 — 기다리다 바깥에서 취소되면 태스크는 아직 돌고 있으니
        소유자 (`_restarts`) 를 잃지 않게 남긴다. 다음 정지가 다시 끊는다.
        """
        task = self._restarts.get(key)
        if task is not None:
            await cancel_and_wait(task)
        self._restarts.pop(key, None)

    async def _unwatch(self, launched: LaunchedAgent) -> None:
        """이 레플리카의 health 루프를 멈춘다 — 정지한 컨테이너를 계속 두드리지 않는다."""
        key = (launched.agent, launched.replica)
        task = self._monitors.get(key)
        if task is not None:
            await cancel_and_wait(task)
        self._monitors.pop(key, None)

    def _promote(self, launched: LaunchedAgent, state: AgentState, *, healthy: bool) -> None:
        """첫 health **성공**을 Ready 로 올린다.

        02 Rule 2 — 기동만으로 Ready 를 선언하면 `initialize()` 가 끝내
        실패한 컨테이너가 태스크를 받는다. 그 판정은 monitor 가 아니라
        **runtime 이** 한다 (lifecycle 은 성공을 Ready 로 올리지 않는다).
        임계 미만의 실패는 상태를 STARTING 에 두므로 상태만 보면 성공과
        구분되지 않는다 — 성공 여부를 따로 본다.
        """
        if state is AgentState.STARTING:
            if healthy:
                launched.lifecycle.transition(AgentState.READY)
            return
        if state is AgentState.UNHEALTHY:
            self._schedule_restart(launched)

    async def adopt(
        self,
        agent: str,
        *,
        replica: int,
        container_id: str,
        image: str,
        control_port: int,
        token: str,
        control_host: str = LOOPBACK,
        a2a_port: int | None = None,
        restart_args: Mapping[str, Any] | None = None,
    ) -> bool:
        """Pick up a container this process did not start.

        control plane 이 재시작하면 컨테이너는 Docker 가 그대로 들고 있다 —
        기록(#243 `DeploymentRecord`)으로 다시 붙는다. 살아 있지 않으면 False 다:
        조용히 새로 띄우면 기록과 실체가 어긋난 채 두 벌이 된다.

        토큰은 기록에서 온다 — agentd 는 기동 시 받은 토큰을 바꿀 수 없으므로
        재발급이 아니라 **기억**이다.

        `restart_args` 는 이 컨테이너가 죽었을 때 같은 선언으로 다시 세우는 데
        쓴다 — 없으면 health 감시가 첫 재시작에서 넘어진다.
        """
        try:
            state: dict[str, Any] = await asyncio.to_thread(
                self.engine.client.inspect, container_id
            )
        except Exception:  # noqa: BLE001 — 없는 컨테이너는 "못 붙는다" 이지 예외가 아니다
            return False
        if not state.get("Running"):
            return False

        handle = ContainerHandle(
            agent=agent,
            container_id=container_id,
            image=image,
            control_port=control_port,
            control_host=control_host,
        )
        self.issuer.remember(agent, token)
        self._prime_restart_counter(agent)
        client = ControlClient(handle.control_url, agent=agent, retry=NETWORK_RETRY, token=token)
        # Ready 는 **선언하지 않는다** — 살아 있다는 것과 태스크를 받을 수 있다는
        # 것은 다르다. 첫 health 성공이 올린다 (02 Rule 2, `_promote`)
        lifecycle = AgentLifecycle(agent=agent)
        lifecycle.transition(AgentState.BUILT)
        lifecycle.transition(AgentState.STARTING)
        launched = LaunchedAgent(
            agent=agent,
            handle=handle,
            client=client,
            replica=replica,
            lifecycle=lifecycle,
            a2a_port=a2a_port,
            restart_args=dict(restart_args or {}),
        )
        self.launched[agent, replica] = launched
        # 이미 Ready 였던 기록만 붙인다 — control plane 재시작이 떠 있던 에이전트에게 기동 유예를
        # 다시 주면, 그 사이의 고장이 임계만큼 늦게 드러난다 (#297 리뷰)
        self._watch(launched, ready_once=True)
        log.info(
            "agent adopted",
            agent=agent,
            container_id=handle.short_id,
            image=image,
            port=control_port,
        )
        return True

    def replicas_of(self, agent: str) -> list[LaunchedAgent]:
        """이 에이전트의 기동된 레플리카 — replica 순서로."""
        return [launched for (name, _), launched in sorted(self.launched.items()) if name == agent]

    def route(self, agent: str) -> ControlClient:
        """Pick the next replica's client, round-robin.

        레플리카 간 round-robin 으로 클라이언트를 고릅니다 (01 Scalability).

        **준비된 레플리카만 고른다**: 재시작 중이거나 Unhealthy 인 쪽으로
        보내면 그 태스크가 전부 실패한다. lifecycle 이 실제로 도는 배선에서만
        상태가 갱신되므로(#213), 감시를 켜지 않았으면 기동된 전부를 후보로
        본다 — 아니면 감시 없는 조립이 통째로 라우팅 불가가 된다.

        Raises:
            AaestroError: RUNTIME/``RT_009`` if the agent has no launched replica.
        """
        replicas = self.replicas_of(agent)
        if self.health_interval_s is not None:
            ready = [item for item in replicas if item.lifecycle.accepts_tasks]
            replicas = ready or []
        if not replicas:
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_009,
                message="no launched replica for agent",
                agent=agent,
                retryable=True,
            )
        cursor = self._cursors.get(agent, 0)
        self._cursors[agent] = cursor + 1
        return replicas[cursor % len(replicas)].client

    async def stop(self, agent: str, *, replica: int | None = None) -> None:
        """Stop an agent's replicas and forget its token.

        에이전트를 정지하고 토큰을 버립니다 — 죽은 토큰을 들고 있지 않습니다.
        컨테이너 정지가 실패하면 핸들을 **그대로 둡니다** — 유일한 재시도
        수단을 먼저 버리면 미아 컨테이너를 다시 정리할 방법이 없습니다.

        Args:
            agent: The agent to stop.
            replica: One replica, or **every** replica when omitted — 02 의
                drain 은 에이전트 단위이므로 전부가 기본이다.
        """
        # 진행 중인 재시작을 먼저 끊는다 — 놔두면 방금 정지한 자리에 컨테이너를
        # 다시 세워 미아를 만든다. 끊긴 교체는 원래 레플리카를 목록에 되돌린다
        await self._cancel_restarts(agent, replica=replica)
        targets = (
            [self.launched[agent, replica]]
            if replica is not None and (agent, replica) in self.launched
            else self.replicas_of(agent)
        )
        for launched in targets:
            key = (agent, launched.replica)
            await self._unwatch(launched)
            # 감시를 끈 **뒤에** 다시 끊는다: 앞 레플리카를 정지하는 동안 이 레플리카의 감시가
            # 새 재시작을 걸었을 수 있다. 그 재시작이 이미 교체를 끝냈으면 목록의 것이 새 컨테이너다
            await self._cancel_restart(key)
            launched = self.launched.get(key, launched)
            # 02 Lifecycle 5 — 정지 전에 새 태스크 수락을 멈춘다.
            # STARTING 에서 바로 멈추는 경우도 있어 Draining 을 강요하지 않는다
            if launched.lifecycle.state is AgentState.READY:
                launched.lifecycle.transition(AgentState.DRAINING)
                await self._drain(launched)
            await self._stop_container(launched)
            if launched.lifecycle.state is not AgentState.STOPPED:
                launched.lifecycle.transition(AgentState.STOPPED)
            # 재시작이 먼저 지웠을 수 있다 — 없으면 지울 것도 없다
            self.launched.pop((agent, launched.replica), None)
            if self.ports is not None:
                self.ports.release(agent, replica=launched.replica)

        # 토큰은 에이전트 단위다 — 레플리카가 남아 있으면 아직 버리지 않는다
        if not self.replicas_of(agent):
            self.issuer.forget(agent)
            self._cursors.pop(agent, None)

    async def _drain(self, launched: LaunchedAgent) -> None:
        """02 Lifecycle 4 — 진행 중 태스크가 끝나길 기다린 뒤에 정지한다.

        기다림의 상한을 넘기면 **그래도 정지한다** (Rule 5 의 SIGTERM 경로) —
        다만 조용히는 아니다: 남은 태스크가 있었다는 사실을 RT_005 로 남긴다.
        """
        try:
            await launched.client.drain(timeout_s=self.drain_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001 — drain 은 최선의 시도다; 어떤 실패도 정지를 막지 않는다
            log.warning(
                "agent drain did not complete; stopping anyway",
                agent=launched.agent,
                container_id=launched.handle.short_id,
                image=launched.handle.image,
                error_code=ErrorCode.RT_005,
                timeout_s=self.drain_timeout_s,
                exc_info=err,
            )

    async def stop_all(self) -> None:
        """기동된 에이전트를 전부 정지한다 — 하나가 실패해도 나머지를 계속 정리한다."""
        failures: list[BaseException] = []
        # 교체는 기동을 기다리는 동안 레플리카를 목록에서 잠시 뺀다 — 목록만 보면 그 틈의
        # 에이전트를 놓쳐, 정지가 끝난 뒤 재시작이 컨테이너를 다시 세운다 (#482)
        names = [name for name, _ in self.launched] + [name for name, _ in self._restarts]
        for agent in dict.fromkeys(names):
            try:
                await self.stop(agent)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                failures.append(err)
                log.error("agent stop failed", agent=agent, exc_info=err)
        # 취소된 기동이 넘긴 컨테이너 정리 — 종료가 끝까지 기다리거나 끊는다 (07 Async 5)
        await self.engine.settle_cleanups()

        if failures:
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_005,
                message="one or more agents failed to stop",
                details={"agents": len(failures)},
            ) from failures[0]

    def clients(self) -> dict[str, ControlClient]:
        """에이전트별 대표 클라이언트 — **레플리카가 여럿이면 라우팅되지 않는다.**

        분산이 필요하면 `route` 를 쓴다: 이 매핑은 값이 고정이라 같은 레플리카만
        계속 부른다.
        """
        return {name: replicas[0].client for name, replicas in self._by_agent().items()}

    def _by_agent(self) -> dict[str, list[LaunchedAgent]]:
        """에이전트별 레플리카 목록."""
        grouped: dict[str, list[LaunchedAgent]] = {}
        for (name, _), launched in sorted(self.launched.items()):
            grouped.setdefault(name, []).append(launched)
        return grouped


__all__ = ["AgentLauncher", "LaunchedAgent"]
