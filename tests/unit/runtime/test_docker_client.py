"""SdkDockerClient 의 순수 부분 — 이미지 참조 해석."""

from __future__ import annotations

import pytest

from aaestro.runtime.docker.client import SdkDockerClient, image_reference
from aaestro.runtime.docker.errors import NetworkIsolationError


@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("aaestro/agent-base:0.1.0", ("aaestro/agent-base", "0.1.0")),
        # registry 포트의 콜론은 태그가 아니다
        ("registry.local:5000/team/agent:1.2.3", ("registry.local:5000/team/agent", "1.2.3")),
        ("registry.local:5000/team/agent", ("registry.local:5000/team/agent", "latest")),
        # digest 는 태그 자리에 그대로 간다 — SDK 가 그렇게 당긴다
        ("agent@sha256:" + "a" * 64, ("agent", "sha256:" + "a" * 64)),
        ("agent", ("agent", "latest")),
    ],
)
def test_image_reference_splits_like_the_docker_cli(image, expected):
    assert image_reference(image) == expected


class _Network:
    def __init__(self, internal: bool) -> None:
        self.attrs = {"Internal": internal}


class _Networks:
    def __init__(self, existing: dict[str, bool]) -> None:
        self.existing = existing
        self.created: list[tuple[str, dict]] = []

    def get(self, name):
        from docker.errors import NotFound

        if name not in self.existing:
            raise NotFound(name)
        return _Network(self.existing[name])

    def create(self, name, **kwargs):
        self.created.append((name, kwargs))


class _Container:
    def __init__(self, networks: dict) -> None:
        self.attrs = {"NetworkSettings": {"Networks": networks}}

    def reload(self) -> None:
        return None


class _Containers:
    def __init__(self, container: _Container) -> None:
        self.container = container

    def get(self, _container_id):
        return self.container


class _Sdk:
    def __init__(self, existing=None, container=None) -> None:
        self.networks = _Networks(existing or {})
        self.containers = _Containers(container or _Container({}))


@pytest.mark.parametrize("internal", [True, False])
def test_a_missing_network_is_created_with_the_requested_isolation(internal):
    sdk = _Sdk()

    SdkDockerClient(handle=sdk).ensure_network("agents", internal=internal)

    assert sdk.networks.created == [("agents", {"driver": "bridge", "internal": internal})]


@pytest.mark.parametrize(("existing", "wanted"), [(False, True), (True, False)])
def test_an_existing_network_with_other_isolation_is_not_used(existing, wanted):
    """외부 경로가 있는 네트워크에 격리된 에이전트를 올리면 프록시를 우회할 수 있다 (#280)."""
    sdk = _Sdk(existing={"agents": existing})

    with pytest.raises(NetworkIsolationError) as exc_info:
        SdkDockerClient(handle=sdk).ensure_network("agents", internal=wanted)

    assert (exc_info.value.expected, exc_info.value.actual) == (wanted, existing)
    assert sdk.networks.created == []


def test_an_existing_network_with_matching_isolation_is_used_as_is():
    sdk = _Sdk(existing={"agents": True})

    SdkDockerClient(handle=sdk).ensure_network("agents", internal=True)

    assert sdk.networks.created == []


def test_the_address_is_read_from_the_named_network():
    container = _Container(
        {"other": {"IPAddress": "10.0.0.2"}, "agents": {"IPAddress": "172.30.0.7"}}
    )

    assert (
        SdkDockerClient(handle=_Sdk(container=container)).address_of("c", "agents") == "172.30.0.7"
    )
    with pytest.raises(LookupError):
        SdkDockerClient(handle=_Sdk(container=container)).address_of("c", "missing")
