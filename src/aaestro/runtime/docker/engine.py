"""Docker SDK call layer.

Docker 를 실제로 만지는 유일한 계층. 상위(orchestrator)는 이 계약만 알고,
SDK 타입은 여기서 벗겨진다 — backend 를 Kubernetes 로 바꿔도 계약은 그대로다.
"""

from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from aaestro.runtime.docker.errors import (
    OOM_EXIT_CODE,
    NetworkIsolationError,
    drain_timeout,
    hardening_mismatch,
    image_unavailable,
    invalid_spec,
    network_mismatch,
    oom_killed,
    start_failed,
)
from aaestro.runtime.spec import LOOPBACK, ContainerSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping

DEFAULT_NETWORK = "aaestro-net"
DEFAULT_STOP_GRACE_S = 30.0
DEFAULT_DRAIN_TIMEOUT_S = 30.0

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


SHARED_NAMESPACE_FIELDS = ("PidMode", "IpcMode", "UsernsMode", "NetworkMode")
HOST_NAMESPACE = "host"
CONTAINER_NAMESPACE_PREFIX = "container:"
SECURITY_OPT_SEPARATOR = re.compile(r"[=:]")
UNMASKED_PATH_FIELDS = ("MaskedPaths", "ReadonlyPaths")


def security_opt_map(options: Iterable[str] | None) -> dict[str, str | None]:
    """`security_opt` 항목을 키 → 유효값으로 — 생성 인자와 `docker inspect` 를 같은 모양으로 읽는다.

    Docker 는 같은 키가 여럿이면 **마지막 값** 을 쓴다 (`no-new-privileges:true` 뒤의
    `no-new-privileges:false` 는 `NoNewPrivs: 0` — #489 QA 실측). 그래서 집합이 아니라 맵으로
    비교한다: 항목을 더하거나 뒤에서 덮는 것은 같은 맵이 되지 않는다. 구분자는 `=` 와 옛 `:` 중
    먼저 나오는 것이고 (seccomp 프로필 JSON 안의 `:` 는 값에 남는다), 구분자 없는 항목은 키만이다.
    """
    parsed: dict[str, str | None] = {}
    for option in options or ():
        key, *value = SECURITY_OPT_SEPARATOR.split(option, maxsplit=1)
        parsed[key] = value[0] if value else None
    return parsed


def hardening_gaps(found: Mapping[str, Any], expected: Mapping[str, Any]) -> list[str]:
    """`docker inspect` 값에서 경화가 어긋난 필드 이름 — 없으면 빈 목록.

    ``expected`` 는 그 컨테이너를 만든 생성 인자(user · cap_drop · read_only · security_opt ·
    pids_limit)다 — 에이전트는 `ContainerSpec.hardening_kwargs()`, 사이드카는 `sidecar_kwargs`.
    필드 목록이 아니라 부류를 본다: 생성 인자가 건 설정이 빠진 것, 그리고 걸려 있어도 뜻을 잃게
    만드는 설정(privileged 는 CapDrop 과 무관하게 모든 capability 를 주고, 호스트 · 다른 컨테이너와
    공유하는 네임스페이스도 같다). `security_opt` 는 키별 유효값이 생성 인자와 같아야 한다 —
    더한 항목(`seccomp=unconfined`, 허용 정책 프로필)과 뒤에서 덮은 항목이 모두 걸린다.
    `MaskedPaths` · `ReadonlyPaths` 가 빈 목록이면 `/proc` · `/sys` 가림이 꺼진 것이다
    (`systempaths=unconfined` 는 `SecurityOpt` 에 남지 않고 여기에만 드러난다). `None` 은 그 필드를
    보고하지 않는 옛 daemon 이라 불일치로 보지 않는다 — 비었다는 증거가 아니다.
    """
    wrong: list[str] = []
    if not set(expected.get("cap_drop") or ()) <= set(found.get("CapDrop") or ()):
        wrong.append("CapDrop")
    if expected.get("read_only") and found.get("ReadonlyRootfs") is not True:
        wrong.append("ReadonlyRootfs")
    if found.get("User") != expected.get("user"):
        wrong.append("User")
    if security_opt_map(found.get("SecurityOpt")) != security_opt_map(expected.get("security_opt")):
        wrong.append("SecurityOpt")
    wrong.extend(field for field in UNMASKED_PATH_FIELDS if found.get(field) == [])
    # 0 · None · 음수는 무제한이다 — 기대값과 같아야 fork bomb 방어가 남아 있다
    if "pids_limit" in expected and found.get("PidsLimit") != expected["pids_limit"]:
        wrong.append("PidsLimit")
    if found.get("Privileged"):
        wrong.append("Privileged")
    if found.get("CapAdd"):
        wrong.append("CapAdd")
    wrong.extend(
        field
        for field in SHARED_NAMESPACE_FIELDS
        if (mode := str(found.get(field) or ""))
        and (mode == HOST_NAMESPACE or mode.startswith(CONTAINER_NAMESPACE_PREFIX))
    )
    return wrong


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

    def hardening_of(self, container_id: str) -> dict[str, Any]:
        """`docker inspect` 의 경화 필드 — `hardening_gaps` 가 읽는 이름 그대로."""
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

        try:
            container_id = await asyncio.to_thread(self.client.create, **kwargs)
        except Exception as err:
            raise start_failed(agent, spec.image, reason=type(err).__name__) from err

        try:
            for extra in spec.extra_networks:
                await asyncio.to_thread(self.client.connect, extra, container_id)
            await asyncio.to_thread(self.client.start, container_id)
            host, port = await self.control_address(container_id, container_control_port)
        except Exception as err:
            # 반쯤 만들어진 컨테이너를 남기면 유령 컨테이너가 쌓인다
            await self._discard(container_id, agent=agent, image=spec.image)
            raise start_failed(
                agent, spec.image, container_id=container_id[:12], reason=type(err).__name__
            ) from err

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

    async def verify_isolation(
        self, agent: str, image: str, container_id: str, expected: Mapping[str, Any]
    ) -> None:
        """Refuse a container that is not isolated the way this engine runs agents.

        재부착은 기동을 거치지 않는다 — 프록시를 켜기 전에 일반 네트워크로 뜬 컨테이너, 누가 외부
        네트워크를 더 붙인 컨테이너를 그대로 붙이면 외부 경로가 남은 채 "격리됨" 으로 다뤄진다.
        ``expected`` 는 그 컨테이너를 만든 스펙의 `ContainerSpec.hardening_kwargs()` 다.

        Raises:
            AaestroError: RUNTIME/``RT_001`` (재시도 불가) — 네트워크가 내부가 아니거나, 컨테이너가
                에이전트 네트워크 말고 다른 네트워크에도 붙어 있거나, 경화 설정이 어긋남.
        """
        await self._verify_hardening(agent, image, container_id, expected)
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

    async def _verify_hardening(
        self, agent: str, image: str, container_id: str, expected: Mapping[str, Any]
    ) -> None:
        """기동 스펙의 경화 설정이 실제로 걸려 있는지 대조한다 (02 Docker Isolation 3)."""
        found = await asyncio.to_thread(self.client.hardening_of, container_id)
        wrong = hardening_gaps(found, expected)
        if wrong:
            raise hardening_mismatch(agent, image, wrong)

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

    async def stop(self, handle: ContainerHandle, *, remove: bool = True) -> None:
        """Stop a container, SIGTERM then SIGKILL.

        컨테이너를 정지합니다 — SIGTERM 후 유예 시간이 지나면 SIGKILL.
        정지 경로는 실패해도 진행합니다: 남은 컨테이너가 유령으로 쌓이는 편이
        더 나쁩니다.
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

        if remove:
            await self._discard(handle.container_id, agent=handle.agent, image=handle.image)

        self.started.pop(handle.agent, None)
        log.info(
            "agent container stopped",
            agent=handle.agent,
            container_id=handle.short_id,
            image=handle.image,
        )

    async def _discard(self, container_id: str, *, agent: str, image: str) -> None:
        """컨테이너를 제거한다 — 실패는 로그만 남긴다.

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
