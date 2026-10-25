"""Fake Docker client.

실제 daemon 없이 SDK 호출부의 에러 변환과 정리 동작을 검증한다.
"""

from __future__ import annotations

import pathlib
from typing import Any


class FakeDockerClient:
    """스크립트된 Docker 동작을 돌려주는 대역."""

    def __init__(
        self,
        *,
        image_error: Exception | None = None,
        network_error: Exception | None = None,
        create_error: Exception | None = None,
        start_error: Exception | None = None,
        stop_error: Exception | None = None,
        remove_error: Exception | None = None,
        build_error: Exception | None = None,
        state: dict[str, Any] | None = None,
        host_port: int = 49152,
        address: str = "172.30.0.7",
        attached: tuple[str, ...] | None = None,
        hardening: dict[str, Any] | None = None,
    ) -> None:
        self._image_error = image_error
        self._network_error = network_error
        self._create_error = create_error
        self._start_error = start_error
        self._stop_error = stop_error
        self._remove_error = remove_error
        self._build_error = build_error
        self._state = state or {"Running": True, "ExitCode": 0, "OOMKilled": False}
        self._host_port = host_port
        self._address = address
        self._attached = attached
        self._hardening = hardening

        self.images: list[str] = []
        self.networks: list[str] = []
        self.internal_requests: list[bool] = []
        self.addressed: list[tuple[str, str]] = []
        self.created: list[dict[str, Any]] = []
        self.started: list[str] = []
        self.stopped: list[tuple[str, float]] = []
        self.removed: list[str] = []
        self.built: list[tuple[str, str]] = []
        self.contexts: list[dict[str, str]] = []
        self.connected: list[tuple[str, str, tuple[str, ...]]] = []
        self.disconnected: list[tuple[str, str]] = []
        self.removed_networks: list[str] = []

    def ensure_image(self, image: str) -> None:
        if self._image_error is not None:
            raise self._image_error
        self.images.append(image)

    def ensure_network(self, name: str, *, internal: bool = False) -> None:
        if self._network_error is not None:
            raise self._network_error
        self.networks.append(name)
        self.internal_requests.append(internal)

    def networks_of(self, container_id: str) -> tuple[str, ...]:
        return self._attached if self._attached is not None else tuple(self.networks[-1:])

    def hardening_of(self, container_id: str) -> dict[str, Any]:
        """지정한 값이 있으면 그것, 없으면 만들 때 받은 스펙대로 걸린 것으로 본다."""
        if self._hardening is not None:
            return dict(self._hardening)
        for index, kwargs in enumerate(self.created, start=1):
            if container_id == f"container-{index:04d}" + "0" * 20:
                return {
                    "CapDrop": kwargs.get("cap_drop"),
                    "ReadonlyRootfs": kwargs.get("read_only"),
                    "SecurityOpt": kwargs.get("security_opt"),
                    "User": kwargs.get("user"),
                    "NetworkMode": kwargs.get("network_mode"),
                    "PidsLimit": kwargs.get("pids_limit"),
                }
        raise LookupError(container_id)

    def address_of(self, container_id: str, network: str) -> str:
        self.addressed.append((container_id, network))
        return self._address

    def create(self, **kwargs: Any) -> str:
        if self._create_error is not None:
            raise self._create_error
        self.created.append(kwargs)
        return f"container-{len(self.created):04d}" + "0" * 20

    def start(self, container_id: str) -> None:
        if self._start_error is not None:
            raise self._start_error
        self.started.append(container_id)

    def inspect(self, container_id: str) -> dict[str, Any]:
        return dict(self._state)

    def port_of(self, container_id: str, container_port: int) -> int:
        return self._host_port

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        if self._stop_error is not None:
            raise self._stop_error
        self.stopped.append((container_id, timeout_s))

    def remove(self, container_id: str) -> None:
        if self._remove_error is not None:
            raise self._remove_error
        self.removed.append(container_id)

    def build(self, context: str, tag: str, *, buildargs: dict[str, str] | None = None) -> str:
        """조립된 컨텍스트를 기록만 한다 — 실제 빌드는 통합 테스트 소관."""
        if self._build_error is not None:
            raise self._build_error
        self.built.append((context, tag))
        # 조립이 맞는지 보려면 컨텍스트의 내용이 필요하다 — 지워지기 전에 담아 둔다
        root = pathlib.Path(context)
        self.contexts.append(
            {
                str(f.relative_to(root)): f.read_text(encoding="utf-8")
                for f in sorted(root.rglob("*"))
                if f.is_file()
            }
        )
        return f"built {tag}"

    def connect(self, network: str, container: str, *, aliases: tuple[str, ...] = ()) -> None:
        self.connected.append((network, container, tuple(aliases)))

    def disconnect(self, network: str, container: str) -> None:
        self.disconnected.append((network, container))

    def remove_network(self, name: str) -> None:
        self.removed_networks.append(name)

    def labeled(self, labels: dict[str, str]) -> tuple[str, ...]:
        """라벨이 맞는 **살아 있는** 컨테이너 — 지워진 것은 없는 것이다."""
        found = []
        for index, kwargs in enumerate(self.created, start=1):
            container_id = f"container-{index:04d}" + "0" * 20
            carried = kwargs.get("labels") or {}
            if container_id not in self.removed and all(
                carried.get(k) == v for k, v in labels.items()
            ):
                found.append(container_id)
        return tuple(found)

    def find(self, name: str) -> str | None:
        """이름이 같은 **살아 있는** 컨테이너의 id — 지워진 것은 없는 것이다."""
        for index, kwargs in enumerate(self.created, start=1):
            container_id = f"container-{index:04d}" + "0" * 20
            if kwargs.get("name") == name and container_id not in self.removed:
                return container_id
        return None


__all__ = ["FakeDockerClient"]
