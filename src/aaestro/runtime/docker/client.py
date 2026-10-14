"""The production DockerClient, over the Docker SDK.

`DockerClient` 프로토콜의 구현체가 **테스트에만** 둘 있었다 — `CliDockerClient`(subprocess)
와 `FakeDockerClient`. `pyproject.toml` 의 `docker` 의존성은 선언만 있고 import 하는 곳이
없었다 (#243). 01 은 runtime/ 을 "Docker SDK 를 만지는 유일한 층" 으로 규정하는데, 아무
층도 만지지 않고 있었다 — 그래서 컨테이너를 띄우는 것은 compose 뿐이었다.

예외는 SDK 것을 그대로 올린다. `DockerEngine` 이 단계별로 `except Exception` 으로 받아
RT_004(이미지) / RT_001(기동) 로 옮기므로, 여기서 또 감싸면 이중이다 (05 Layer Rules).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import docker
from docker.errors import BuildError, ImageNotFound, NotFound
from docker.utils import parse_repository_tag

from aaestro.runtime.docker.errors import NetworkIsolationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from docker import DockerClient as SdkHandle

BRIDGE = "bridge"

HARDENING_FIELDS = (
    "CapDrop",
    "CapAdd",
    "ReadonlyRootfs",
    "SecurityOpt",
    "Privileged",
    "PidMode",
    "IpcMode",
    "UsernsMode",
    "NetworkMode",
)
"""`HostConfig` 에서 읽는 경화 필드 — `engine.hardening_gaps` 가 같은 이름으로 대조한다."""

MAX_LOG_CHARS = 8000
"""빌드 로그의 상한 — 원인은 보통 끝에 있다. 전체를 실으면 응답과 기록이 로그 덤프가 된다."""


class ImageBuildError(Exception):
    """Image build failed — carries the log so the reason survives.

    빌드 실패의 원인은 로그에만 있다. 예외 메시지로는 어느 단계에서 무엇이 없었는지
    알 수 없어, 운영자가 결국 손으로 다시 굽게 된다.
    """

    def __init__(self, tag: str, log: str, cause: Exception) -> None:
        super().__init__(f"image build failed: {tag}")
        self.tag = tag
        self.log = log
        self.cause = cause


def _log_of(stream: Iterable[Any]) -> str:
    """SDK 의 빌드 스트림을 사람이 읽는 로그로 — 끝에서부터 상한만큼 남긴다."""
    lines = []
    for chunk in stream or ():
        if not isinstance(chunk, dict):
            continue
        text = chunk.get("stream") or chunk.get("error") or ""
        if isinstance(text, str) and text.strip():
            lines.append(text.rstrip())
    log = "\n".join(lines)
    return log[-MAX_LOG_CHARS:] if len(log) > MAX_LOG_CHARS else log


def image_reference(image: str) -> tuple[str, str]:
    """이미지 참조를 (repository, tag|digest) 로 — SDK 자신의 파서로 나눈다.

    `rpartition(":")` 은 `registry:5000/img` 를 태그로 오독하고, `img@sha256:…` 은
    digest 를 태그 자리에 잘못 넣는다. tag 가 없으면 docker CLI 처럼 `latest` 다 —
    None 으로 넘기면 SDK 가 **모든 태그**를 당긴다.
    """
    repository, tag = parse_repository_tag(image)
    return repository, tag or "latest"


class SdkDockerClient:
    """`DockerClient` over docker-py.

    docker-py 위의 `DockerClient` 구현. 메서드는 동기다 — `DockerEngine` 이
    `asyncio.to_thread` 로 감싼다 (07 Async 2).
    """

    def __init__(self, handle: SdkHandle | None = None) -> None:
        # 환경변수(DOCKER_HOST 등)로 데몬을 찾는다 — 소켓 경로를 하드코딩하지 않는다
        self._sdk = handle or docker.from_env()

    def ensure_image(self, image: str) -> None:
        """이미지를 확보한다 — 없으면 pull.

        에이전트 이미지는 배포 파이프라인이 굽는다 (02 Lifecycle 1) — 로컬 태그는
        pull 이 실패하고, 그 실패는 엔진이 RT_004 로 올린다.
        """
        try:
            self._sdk.images.get(image)
        except ImageNotFound:
            repository, tag = image_reference(image)
            self._sdk.images.pull(repository, tag=tag)

    def ensure_network(self, name: str, *, internal: bool = False) -> None:
        """네트워크를 확보한다 — 없으면 bridge 로 생성 (02 Network).

        이미 있는 네트워크가 요청한 격리와 다르면 쓰지 않는다: 외부 경로가 있는 네트워크에 격리된
        에이전트를 올리면 프록시를 우회할 수 있고, 반대로 내부 네트워크에는 포트가 게시되지 않는다.
        """
        try:
            network = self._sdk.networks.get(name)
        except NotFound:
            self._sdk.networks.create(name, driver=BRIDGE, internal=internal)
            return
        actual = bool(network.attrs.get("Internal"))
        if actual != internal:
            raise NetworkIsolationError(name, expected=internal, actual=actual)

    def create(self, **kwargs: Any) -> str:
        """컨테이너를 만들고 id 를 돌려준다.

        `ContainerSpec.to_docker_kwargs()` 를 그대로 받는다.
        """
        container_id: str = self._sdk.containers.create(**kwargs).id
        return container_id

    def start(self, container_id: str) -> None:
        self._sdk.containers.get(container_id).start()

    def inspect(self, container_id: str) -> dict[str, Any]:
        """`.State` 조각 — CLI 구현(`docker inspect --format '{{json .State}}'`)과 같다."""
        container = self._sdk.containers.get(container_id)
        container.reload()
        state: dict[str, Any] = container.attrs["State"]
        return state

    def port_of(self, container_id: str, container_port: int) -> int:
        container = self._sdk.containers.get(container_id)
        container.reload()
        bindings = container.ports.get(f"{container_port}/tcp") or []
        if not bindings:
            raise LookupError(f"container port {container_port} is not published")
        return int(bindings[0]["HostPort"])

    def networks_of(self, container_id: str) -> tuple[str, ...]:
        container = self._sdk.containers.get(container_id)
        container.reload()
        return tuple(container.attrs["NetworkSettings"]["Networks"])

    def hardening_of(self, container_id: str) -> dict[str, Any]:
        container = self._sdk.containers.get(container_id)
        container.reload()
        host = container.attrs["HostConfig"]
        return {
            **{field: host.get(field) for field in HARDENING_FIELDS},
            "User": container.attrs["Config"].get("User"),
        }

    def address_of(self, container_id: str, network: str) -> str:
        container = self._sdk.containers.get(container_id)
        container.reload()
        attached = container.attrs["NetworkSettings"]["Networks"].get(network) or {}
        address: str = attached.get("IPAddress") or ""
        if not address:
            raise LookupError(f"container has no address on network {network}")
        return address

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        """SIGTERM 후 유예가 지나면 SIGKILL (02 Lifecycle 5)."""
        self._sdk.containers.get(container_id).stop(timeout=int(timeout_s))

    def remove(self, container_id: str) -> None:
        try:
            self._sdk.containers.get(container_id).remove(force=True)
        except NotFound:
            return  # 이미 없다 — 정리의 목적은 달성됐다

    def build(self, context: str, tag: str, *, buildargs: Mapping[str, str] | None = None) -> str:
        """Build an image from a context directory, returning the build log.

        SDK 는 로그를 스트림으로 흘린다 — 성공해도 실패해도 그것을 모아 돌려준다.
        `BuildError` 에도 로그가 실려 있으므로 같은 형태로 꺼내 예외에 담는다.
        """
        try:
            _image, stream = self._sdk.images.build(
                path=context, tag=tag, rm=True, forcerm=True, buildargs=dict(buildargs or {})
            )
        except BuildError as err:
            raise ImageBuildError(tag, _log_of(err.build_log), err) from err
        return _log_of(stream)

    def connect(self, network: str, container: str, *, aliases: tuple[str, ...] = ()) -> None:
        if network in self.networks_of(container):
            return
        self._sdk.networks.get(network).connect(container, aliases=list(aliases) or None)

    def disconnect(self, network: str, container: str) -> None:
        try:
            if network not in self.networks_of(container):
                return
            self._sdk.networks.get(network).disconnect(container)
        except NotFound:
            return  # 컨테이너나 네트워크가 이미 없다 — 떼어 낼 것이 없다

    def remove_network(self, name: str) -> None:
        try:
            self._sdk.networks.get(name).remove()
        except NotFound:
            return

    def labeled(self, labels: Mapping[str, str]) -> tuple[str, ...]:
        filters = {"label": [f"{key}={value}" for key, value in labels.items()]}
        return tuple(c.id for c in self._sdk.containers.list(all=True, filters=filters))

    def find(self, name: str) -> str | None:
        try:
            container_id: str = self._sdk.containers.get(name).id
        except NotFound:
            return None
        return container_id


__all__ = ["SdkDockerClient"]
