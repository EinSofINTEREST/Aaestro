"""Docker SDK call layer.

Docker 를 실제로 만지는 유일한 계층. 상위(orchestrator)는 이 계약만 알고,
SDK 타입은 여기서 벗겨진다 — backend 를 Kubernetes 로 바꿔도 계약은 그대로다.
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from aaestro.runtime.docker.errors import (
    OOM_EXIT_CODE,
    NetworkIsolationError,
    drain_timeout,
    image_unavailable,
    invalid_spec,
    network_mismatch,
    oom_killed,
    start_failed,
)
from aaestro.runtime.spec import LOOPBACK, ContainerSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

DEFAULT_NETWORK = "aaestro-net"
DEFAULT_STOP_GRACE_S = 30.0
DEFAULT_DRAIN_TIMEOUT_S = 30.0
CANCELLED_START_WAIT_S = 5.0
"""취소된 기동이 만든 컨테이너를 그 자리에서 기다려 지우는 상한 — 넘기면 정리를 넘겨받는다.

정지 · 재배포가 걸린 Docker 호출 (SDK 기본 timeout 은 호출당 60초) 에 묶이지 않게 한다 (#482)."""

log = structlog.get_logger(__name__)


AGENT_LABEL = "aaestro.agent"
CONTROL_PORT_NAME = "control"


def agent_of(spec: ContainerSpec) -> str:
    """Read the agent name from the spec's labels.

    스펙의 라벨에서 에이전트 이름을 읽습니다 — 라벨이 곧 소유자 표시이므로
    별도 필드를 만들지 않습니다.
    """
    return spec.labels.get(AGENT_LABEL, spec.name)


def control_port_of(spec: ContainerSpec) -> int:
    """Find the control port declared in the spec.

    스펙이 선언한 control 포트를 찾습니다.

    Raises:
        AaestroError: RUNTIME/``RT_001`` if no control port is declared —
            control 포트가 없으면 runtime 이 에이전트에 닿을 수 없습니다.
    """
    for binding in spec.ports:
        if binding.name == CONTROL_PORT_NAME:
            return binding.container_port
    raise invalid_spec(agent_of(spec), spec.image, reason="spec declares no control port")


@runtime_checkable
class DockerClient(Protocol):
    """The slice of the Docker SDK this layer uses.

    이 계층이 쓰는 Docker SDK 표면. 좁게 잡아 테스트 대역이 가벼워지고,
    SDK 변경의 영향 범위가 여기로 한정된다.
    """

    def ensure_image(self, image: str) -> None:
        """이미지를 확보한다 — 없으면 pull."""
        ...

    def ensure_network(self, name: str, *, internal: bool = False) -> None:
        """네트워크를 확보한다 — 없으면 생성, 있는데 격리가 다르면 예외."""
        ...

    def create(self, **kwargs: Any) -> str:
        """컨테이너를 만들고 id 를 돌려준다."""
        ...

    def start(self, container_id: str) -> None:
        """컨테이너를 기동한다."""
        ...

    def inspect(self, container_id: str) -> dict[str, Any]:
        """컨테이너 상태를 조회한다."""
        ...

    def port_of(self, container_id: str, container_port: int) -> int:
        """컨테이너 포트에 매핑된 호스트 포트."""
        ...

    def networks_of(self, container_id: str) -> tuple[str, ...]:
        """컨테이너가 붙어 있는 네트워크 이름들."""
        ...

    def address_of(self, container_id: str, network: str) -> str:
        """컨테이너가 이 네트워크에서 쓰는 주소 — 포트가 게시되지 않는 내부 네트워크에서 쓴다."""
        ...

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        """SIGTERM 후 유예 시간이 지나면 SIGKILL."""
        ...

    def remove(self, container_id: str) -> None:
        """컨테이너를 제거한다."""
        ...

    def build(self, context: str, tag: str, *, buildargs: Mapping[str, str] | None = None) -> str:
        """컨텍스트 디렉토리로 이미지를 굽고 빌드 로그를 돌려준다 (#265).

        로그는 실패 원인이 담기는 유일한 곳이다 — 예외 메시지만으로는 어느 단계에서
        무엇이 없었는지 알 수 없다.
        """
        ...

    def connect(self, network: str, container: str, *, aliases: tuple[str, ...] = ()) -> None:
        """컨테이너를 네트워크에 붙인다 — 이미 붙어 있으면 그대로 둔다 (#304 사이드카)."""
        ...

    def disconnect(self, network: str, container: str) -> None:
        """네트워크에서 뗀다 — 붙어 있지 않거나 네트워크가 없으면 그대로 둔다."""
        ...

    def remove_network(self, name: str) -> None:
        """네트워크를 지운다 — 없으면 그대로 둔다."""
        ...

    def labeled(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        """라벨이 모두 맞는 컨테이너의 id — 멈춘 것도 포함한다 (정리할 것을 놓치지 않게)."""
        ...

    def find(self, name: str) -> str | None:
        """이름으로 컨테이너 id 를 찾는다 — 없으면 None.

        컨테이너 이름은 결정적(`aaestro-{agent}-{replica}`)이라 재시작을 넘겨
        같은 자리를 가리킨다. id 는 재시작마다 바뀐다 (#243 reattach).
        """
        ...


@dataclass(frozen=True)
class ContainerHandle:
    """A started container.

    기동된 컨테이너. 로그·메트릭의 표준 필드를 그대로 담는다.
    """

    agent: str
    container_id: str
    image: str
    control_port: int
    control_host: str = LOOPBACK
    """control plane 이 이 컨테이너의 Control API 에 닿는 주소 — 게시 포트면 루프백, 내부
    네트워크면 그 네트워크에서의 컨테이너 주소."""

    @property
    def control_url(self) -> str:
        return f"http://{self.control_host}:{self.control_port}"

    @property
    def short_id(self) -> str:
        """로그에 싣는 짧은 id."""
        return self.container_id[:12]


@dataclass
class DockerEngine:
    """Starts and stops agent containers.

    에이전트 컨테이너를 기동/정지한다. 순수 로직(스펙 생성, 상태 머신)은
    이미 분리되어 있으므로 여기서는 SDK 호출과 에러 변환만 담당한다.
    """

    client: DockerClient
    network: str = DEFAULT_NETWORK
    internal: bool = False
    """에이전트 네트워크를 외부 경로 없는 내부 네트워크로 (#280, 02 Network). 켜면 포트를 게시하지
    않고 control plane 은 네트워크 안 주소로 닿는다 — 외부로 나가는 길은 이그레스 프록시뿐이다."""
    stop_grace_s: float = DEFAULT_STOP_GRACE_S
    started: dict[str, ContainerHandle] = field(default_factory=dict)
    cancel_wait_s: float = CANCELLED_START_WAIT_S
    """취소된 기동을 그 자리에서 기다리는 상한 — 넘기면 `_cleanups` 가 이어받는다."""
    _cleanups: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    """상한을 넘긴 정리 — 엔진이 쥐고, 종료가 `settle_cleanups` 로 끝까지 기다리거나 끊는다."""

    async def start(self, spec: ContainerSpec) -> ContainerHandle:
        """Create and start one agent container.

        컨테이너를 만들고 기동합니다.

        Args:
            spec: The container specification derived from the manifest.

        Returns:
            The started container handle.

        Raises:
            AaestroError: RUNTIME/``RT_004`` if the image is unavailable,
                ``RT_001`` if the container fails to create or start.
        """
        agent = agent_of(spec)
        container_control_port = control_port_of(spec)
        if self.internal and spec.extra_networks:
            # 격리된 에이전트가 다른 네트워크에 붙으면 그 길로 프록시를 건너뛴다 — 사이드카도
            # 프록시가 부른다 (#304)
            raise invalid_spec(
                agent, spec.image, reason="isolated agents join only the agent network",
                networks=list(spec.extra_networks),
            )  # fmt: skip

        try:
            await asyncio.to_thread(self.client.ensure_image, spec.image)
        except Exception as err:
            raise image_unavailable(agent, spec.image, reason=type(err).__name__) from err

        try:
            await asyncio.to_thread(
                self.client.ensure_network, self.network, internal=self.internal
            )
        except NetworkIsolationError as err:
            raise network_mismatch(agent, spec.image, err) from err
        except Exception as err:
            raise start_failed(
                agent, spec.image, reason=f"network unavailable: {type(err).__name__}"
            ) from err

        kwargs = spec.to_docker_kwargs()
        kwargs["network"] = self.network
        if self.internal:
            # 내부 네트워크에는 게시가 먹지 않는다 — 남겨 두면 닿지 않는 주소를 믿게 된다
            kwargs["ports"] = {}

        # create · start 는 스레드에서 끝까지 간다 — 기다리던 쪽이 취소돼도 스레드는 멈추지 않고
        # 컨테이너를 만든다. 그래서 취소는 그 끝을 기다린 뒤에 정리하고 다시 올린다 (#482)
        creating = asyncio.create_task(self._create_and_start(spec, kwargs, container_control_port))
        try:
            container_id, host, port = await asyncio.shield(creating)
        except asyncio.CancelledError:
            await self._discard_after_cancel(creating, agent=agent, image=spec.image)
            raise

        handle = ContainerHandle(
            agent=agent,
            container_id=container_id,
            image=spec.image,
            control_port=port,
            control_host=host,
        )
        self.started[agent] = handle
        log.info(
            "agent container started",
            agent=agent,
            container_id=handle.short_id,
            image=spec.image,
            port=port,
        )
        return handle

    async def _create_and_start(
        self, spec: ContainerSpec, kwargs: dict[str, Any], container_port: int
    ) -> tuple[str, str, int]:
        """컨테이너를 만들고 띄워 (id, host, port) 를 돌려준다 — 반쯤 만든 것은 지운다."""
        agent = agent_of(spec)
        try:
            container_id: str = await asyncio.to_thread(self.client.create, **kwargs)
        except Exception as err:
            raise start_failed(agent, spec.image, reason=type(err).__name__) from err

        try:
            for extra in spec.extra_networks:
                await asyncio.to_thread(self.client.connect, extra, container_id)
            await asyncio.to_thread(self.client.start, container_id)
            host, port = await self.control_address(container_id, container_port)
        except Exception as err:
            # 반쯤 만들어진 컨테이너를 남기면 유령 컨테이너가 쌓인다
            await self._discard(container_id, agent=agent, image=spec.image)
            raise start_failed(
                agent, spec.image, container_id=container_id[:12], reason=type(err).__name__
            ) from err
        return container_id, host, port

    async def _discard_after_cancel(
        self, creating: asyncio.Task[tuple[str, str, int]], *, agent: str, image: str
    ) -> None:
        """취소된 기동이 끝나길 잠깐 기다렸다가 만들어진 컨테이너를 지운다.

        Docker 호출은 스레드에서 끝까지 간다 — 걸리면 호출마다 SDK timeout (기본 60초) 까지다.
        그래서 그 자리에서는 `cancel_wait_s` 까지만 기다리고, 넘기거나 기다리는 쪽이 또 취소되면
        정리를 **엔진이 쥔** 태스크로 넘긴다. 손을 놓으면 그 컨테이너는 소유자가 없다.
        """
        try:
            done, _ = await asyncio.wait({creating}, timeout=self.cancel_wait_s)
        except asyncio.CancelledError:
            self._hand_off_cleanup(creating, agent=agent, image=image)
            raise
        if not done:
            self._hand_off_cleanup(creating, agent=agent, image=image)
            return
        await self._discard_started(creating, agent=agent, image=image)

    def _hand_off_cleanup(
        self, creating: asyncio.Task[tuple[str, str, int]], *, agent: str, image: str
    ) -> None:
        """정리를 엔진이 쥔 태스크로 넘긴다 — 아직 만들어지는 중이라 id 는 모른다."""
        log.warning(
            "cancelled agent container start is still running; cleanup continues in background",
            agent=agent,
            container_id=None,
            image=image,
            timeout_s=self.cancel_wait_s,
        )
        cleanup = asyncio.create_task(self._discard_when_done(creating, agent=agent, image=image))
        self._cleanups.add(cleanup)
        cleanup.add_done_callback(self._cleanups.discard)

    async def _discard_when_done(
        self, creating: asyncio.Task[tuple[str, str, int]], *, agent: str, image: str
    ) -> None:
        """걸린 기동이 끝나면 만들어진 컨테이너를 지운다."""
        try:
            await asyncio.wait({creating})
        except asyncio.CancelledError:
            log.warning(
                "cleanup of a cancelled agent container start abandoned; container may remain",
                agent=agent,
                container_id=None,
                image=image,
            )
            raise
        await self._discard_started(creating, agent=agent, image=image)

    async def _discard_started(
        self, creating: asyncio.Task[tuple[str, str, int]], *, agent: str, image: str
    ) -> None:
        """끝난 기동이 만든 컨테이너를 지운다 — 실패한 기동은 `_create_and_start` 가 이미 지웠다."""
        if creating.cancelled() or creating.exception() is not None:
            return
        container_id, _, _ = creating.result()
        log.warning(
            "agent container start cancelled; discarding the container",
            agent=agent,
            container_id=container_id[:12],
            image=image,
        )
        await self._discard(container_id, agent=agent, image=image)

    async def settle_cleanups(self, *, timeout_s: float | None = None) -> None:
        """Finish the cleanups handed off by cancelled starts, or abandon them loudly.

        넘겨받은 정리를 끝까지 기다린다 — 상한 (기본 `stop_grace_s`) 을 넘기면 끊고, 끊긴
        정리는 남았을 수 있는 컨테이너를 WARN 으로 알린다. 종료 경로가 부른다.
        """
        pending = set(self._cleanups)
        if not pending:
            return
        limit = self.stop_grace_s if timeout_s is None else timeout_s
        _, still = await asyncio.wait(pending, timeout=limit)
        for task in still:
            task.cancel()
        if still:
            await asyncio.gather(*still, return_exceptions=True)

    async def control_address(self, container_id: str, container_port: int) -> tuple[str, int]:
        """Where the control plane reaches this container's Control API — (host, port).

        게시 포트면 루프백의 호스트 포트, 내부 네트워크면 그 네트워크에서의 컨테이너 주소와
        컨테이너 포트다. 재부착도 같은 길로 주소를 다시 구한다 — 재시작이 주소를 바꾼다.
        """
        if self.internal:
            address = await asyncio.to_thread(self.client.address_of, container_id, self.network)
            return address, container_port
        port = await asyncio.to_thread(self.client.port_of, container_id, container_port)
        return LOOPBACK, port

    async def verify_isolation(self, agent: str, image: str, container_id: str) -> None:
        """Refuse a container that is not isolated the way this engine runs agents.

        재부착은 기동을 거치지 않는다 — 프록시를 켜기 전에 일반 네트워크로 뜬 컨테이너, 누가 외부
        네트워크를 더 붙인 컨테이너를 그대로 붙이면 외부 경로가 남은 채 "격리됨" 으로 다뤄진다.

        Raises:
            AaestroError: RUNTIME/``RT_001`` (재시도 불가) — 네트워크가 내부가 아니거나, 컨테이너가
                에이전트 네트워크 말고 다른 네트워크에도 붙어 있음.
        """
        if not self.internal:
            return
        try:
            await asyncio.to_thread(self.client.ensure_network, self.network, internal=True)
        except NetworkIsolationError as err:
            raise network_mismatch(agent, image, err) from err
        attached = await asyncio.to_thread(self.client.networks_of, container_id)
        if set(attached) != {self.network}:
            raise network_mismatch(
                agent,
                image,
                NetworkIsolationError(self.network, expected=True, actual=False),
                attached=sorted(attached),
            )

    async def inspect(self, handle: ContainerHandle) -> dict[str, Any]:
        """컨테이너 상태를 조회한다."""
        state: dict[str, Any] = await asyncio.to_thread(self.client.inspect, handle.container_id)
        return state

    async def check_exit(self, handle: ContainerHandle) -> None:
        """Raise if the container died in a way worth distinguishing.

        컨테이너가 죽은 방식을 구분해 올립니다 — OOM 은 리소스 조정이 필요하고,
        일반 종료는 재시작으로 풀릴 수 있어 정책이 다릅니다.

        Raises:
            AaestroError: RUNTIME/``RT_003`` when the container was OOM killed.
        """
        state = await self.inspect(handle)
        if state.get("OOMKilled") or state.get("ExitCode") == OOM_EXIT_CODE:
            raise oom_killed(
                handle.agent,
                handle.short_id,
                image=handle.image,
                exit_code=state.get("ExitCode"),
            )

    async def drain(
        self,
        handle: ContainerHandle,
        *,
        request_drain: Callable[[], Any],
        inflight: Callable[[], int],
        timeout_s: float = DEFAULT_DRAIN_TIMEOUT_S,
        poll_s: float = 0.5,
        sleep: Callable[[float], Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """Stop accepting tasks and wait for in-flight work.

        새 태스크 수락을 멈추고 진행 중 태스크를 기다립니다.
        시간 판정은 주입 가능한 clock/sleep 을 씁니다 — 테스트가 실제로 자지
        않도록 하기 위해서입니다.

        Args:
            handle: The container to drain.
            request_drain: Asks the agent to stop accepting tasks.
                Must be awaited if it returns an awaitable.
            inflight: Reports how many tasks are still running.
            timeout_s: How long to wait before giving up.
            poll_s: Delay between checks.
            sleep: Delay function; defaults to ``asyncio.sleep``.
            clock: Monotonic clock; defaults to the event loop clock.

        Raises:
            AaestroError: RUNTIME/``RT_005`` if work is still running at the
                deadline — 남은 태스크를 조용히 버리지 않습니다.
        """
        sleeper = sleep or asyncio.sleep
        now = clock or asyncio.get_running_loop().time

        result = request_drain()
        # coroutine 뿐 아니라 Future/Task 도 기다려야 한다 — 건너뛰면 에이전트가
        # 아직 태스크를 받는 중인데 drain 이 진행된다
        if inspect.isawaitable(result):
            await result

        deadline = now() + timeout_s
        while True:
            remaining = inflight()
            if remaining == 0:
                log.info(
                    "agent drained",
                    agent=handle.agent,
                    container_id=handle.short_id,
                    image=handle.image,
                )
                return
            if now() >= deadline:
                raise drain_timeout(
                    handle.agent,
                    handle.short_id,
                    image=handle.image,
                    inflight=remaining,
                    timeout_s=timeout_s,
                )
            await sleeper(poll_s)

    async def stop(self, handle: ContainerHandle, *, remove: bool = True) -> bool:
        """Stop a container, SIGTERM then SIGKILL.

        컨테이너를 정지합니다 — SIGTERM 후 유예 시간이 지나면 SIGKILL.
        정지 경로는 실패해도 진행합니다: 남은 컨테이너가 유령으로 쌓이는 편이
        더 나쁩니다.

        Returns:
            True if the container was removed. 제거 실패는 예외가 아니라 False 다 —
            호출자가 같은 handle 로 다시 지울지 정한다. ``remove=False`` 면 False.
        """
        try:
            await asyncio.to_thread(
                self.client.stop, handle.container_id, timeout_s=self.stop_grace_s
            )
        except Exception as err:  # noqa: BLE001 — 정지 실패가 정리를 막으면 안 된다
            log.warning(
                "container stop failed",
                agent=handle.agent,
                container_id=handle.short_id,
                image=handle.image,
                error=type(err).__name__,
            )

        removed = remove and await self._discard(
            handle.container_id, agent=handle.agent, image=handle.image
        )

        self.started.pop(handle.agent, None)
        log.info(
            "agent container stopped",
            agent=handle.agent,
            container_id=handle.short_id,
            image=handle.image,
        )
        return removed

    async def _discard(self, container_id: str, *, agent: str, image: str) -> bool:
        """컨테이너를 제거한다 — 실패는 로그만 남기고 False 를 돌려준다.

        제거 실패 로그에도 소유자와 이미지를 남긴다: 여러 이미지가 함께 도는
        환경에서 이 필드가 없으면 사고 추적이 끊긴다 (05 필수 필드).
        """
        try:
            await asyncio.to_thread(self.client.remove, container_id)
        except Exception as err:  # noqa: BLE001
            log.warning(
                "container removal failed",
                agent=agent,
                container_id=container_id[:12],
                image=image,
                error=type(err).__name__,
            )
            return False
        return True


__all__ = [
    "DEFAULT_DRAIN_TIMEOUT_S",
    "DEFAULT_NETWORK",
    "DEFAULT_STOP_GRACE_S",
    "AGENT_LABEL",
    "CONTROL_PORT_NAME",
    "ContainerHandle",
    "DockerClient",
    "DockerEngine",
    "agent_of",
    "control_port_of",
]
