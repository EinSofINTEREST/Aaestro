"""Every DockerClient implementation must behave the same.

프로덕션 구현(`SdkDockerClient`)과 테스트 참조 구현(`CliDockerClient`)이 갈리면
E2E 는 통과하고 실제 배포는 다르게 동작한다 (#243). 같은 계약 테스트를 둘 다 통과한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from aaestro.runtime.docker.client import SdkDockerClient
from aaestro.runtime.docker.engine import DockerClient
from tests.integration.runtime.test_docker_lifecycle import (
    CliDockerClient,
    docker,
    echo_image,  # noqa: F401 — fixture
    requires_docker,
)

pytestmark = [pytest.mark.integration, requires_docker]
NETWORK = "aaestro-contract-net"


def spec_kwargs(image: str, name: str) -> dict[str, Any]:
    return {
        "name": name,
        "image": image,
        "environment": {"AAESTRO_AGENT_TOKEN": "contract"},
        "network": NETWORK,
        "ports": {"8080/tcp": ("127.0.0.1", None)},
        "nano_cpus": 500_000_000,
        "mem_limit": 268_435_456,
        "pids_limit": 256,
        "user": "1000:1000",
        "read_only": True,
        "cap_drop": ["ALL"],
        "tmpfs": {"/tmp": "size=64m", "/workspace": "size=64m"},  # noqa: S108 — 컨테이너 안 경로다
        "volumes": {},
        "labels": {"aaestro.contract": "1"},
        "network_mode": None,
        "publish_all_ports": False,
    }


@pytest.fixture(params=[CliDockerClient, SdkDockerClient], ids=["cli", "sdk"])
def client(request) -> DockerClient:
    return request.param()


@pytest.fixture
def container(client: DockerClient, echo_image: str):  # noqa: F811
    """만들고 반드시 지운다 — 유령 컨테이너는 05 Consistency 3 위반이다."""
    client.ensure_image(echo_image)
    client.ensure_network(NETWORK)
    name = f"contract-{type(client).__name__.lower()}"
    docker("rm", "-f", name, check=False)
    container_id = client.create(**spec_kwargs(echo_image, name))
    try:
        yield container_id
    finally:
        client.remove(container_id)


def test_the_protocol_is_satisfied(client):
    assert isinstance(client, DockerClient)


def test_a_missing_image_is_an_error_not_a_silent_pass(client):
    with pytest.raises(Exception):  # noqa: B017 — 종류는 엔진이 RT_004 로 옮긴다
        client.ensure_image("aaestro/does-not-exist:0.0.0")


def test_create_start_inspect_port_stop_round_trip(client, container):
    client.start(container)

    state = client.inspect(container)
    assert state["Running"] is True
    assert client.port_of(container, 8080) > 0

    client.stop(container, timeout_s=5)

    assert client.inspect(container)["Running"] is False


def test_the_security_settings_reach_the_container(client, container):
    """02 Docker Isolation — 표기가 아니라 컨테이너에 걸린 값을 본다."""
    raw = docker(
        "inspect",
        "--format",
        " ".join(
            f"{{{{.HostConfig.{field}}}}}"
            for field in ("ReadonlyRootfs", "PidsLimit", "NanoCpus", "Memory", "CapDrop")
        ),
        container,
    )

    assert raw.split() == ["true", "256", "500000000", "268435456", "[ALL]"]


def test_find_resolves_a_name_to_the_live_id_only(client, container):
    """재부착은 이름으로 찾는다 — 재시작마다 id 가 바뀌기 때문이다."""
    container_id = container
    name = f"contract-{type(client).__name__.lower()}"
    assert client.find(name) == container_id
    assert client.find("aaestro-no-such-container") is None
    client.remove(container_id)
    assert client.find(name) is None


def test_remove_is_idempotent(client, container):
    client.remove(container)

    client.remove(container)  # 두 번째도 조용히 성공해야 정리 경로가 안전하다


INTERNAL = "aaestro-contract-internal"


def test_network_isolation_is_created_and_a_mismatch_is_refused(client):
    """두 구현이 같아야 통합 테스트가 격리 불일치를 실제로 검증한다 (#280)."""
    from aaestro.runtime.docker.errors import NetworkIsolationError

    docker("network", "rm", INTERNAL, check=False)
    try:
        client.ensure_network(INTERNAL, internal=True)
        assert docker("network", "inspect", "-f", "{{.Internal}}", INTERNAL) == "true"
        client.ensure_network(INTERNAL, internal=True)  # 같은 격리면 그대로 쓴다
        with pytest.raises(NetworkIsolationError):
            client.ensure_network(INTERNAL, internal=False)
        client.ensure_network(NETWORK)
        with pytest.raises(NetworkIsolationError):
            client.ensure_network(NETWORK, internal=True)
    finally:
        docker("network", "rm", INTERNAL, check=False)


def test_networks_and_address_are_read_from_the_container(client, container):
    client.start(container)

    assert client.networks_of(container) == (NETWORK,)
    assert client.address_of(container, NETWORK).count(".") == 3
    with pytest.raises(LookupError):
        client.address_of(container, "aaestro-not-attached")


SIDE = "aaestro-contract-side"


def test_a_container_joins_and_leaves_a_second_network_idempotently(client, container):
    """사이드카 네트워크 (#304) — 프록시를 붙였다 떼고, 네트워크를 지운다. 두 번 해도 조용하다."""
    docker("network", "rm", SIDE, check=False)
    try:
        client.ensure_network(SIDE, internal=True)
        client.connect(SIDE, container, aliases=("contract-alias",))
        client.connect(SIDE, container, aliases=("contract-alias",))
        assert set(client.networks_of(container)) == {NETWORK, SIDE}
        aliases = docker(
            "inspect", "-f", f'{{{{json (index .NetworkSettings.Networks "{SIDE}").Aliases}}}}',
            container,
        )  # fmt: skip
        assert "contract-alias" in aliases

        client.disconnect(SIDE, container)
        client.disconnect(SIDE, container)
        assert client.networks_of(container) == (NETWORK,)
        client.remove_network(SIDE)
        client.remove_network(SIDE)
        assert not docker("network", "inspect", "-f", "{{.Id}}", SIDE, check=False)
    finally:
        docker("network", "rm", SIDE, check=False)


def test_labeled_finds_stopped_containers_and_nothing_else(client, container):
    assert container in client.labeled({"aaestro.contract": "1"})
    assert client.labeled({"aaestro.contract": "1", "aaestro.nothing": "x"}) == ()
    client.remove(container)
    assert container not in client.labeled({"aaestro.contract": "1"})


def test_sidecar_hardening_reaches_the_container(client, echo_image):  # noqa: F811
    name = f"contract-hardened-{type(client).__name__.lower()}"
    docker("rm", "-f", name, check=False)
    kwargs = {
        **spec_kwargs(echo_image, name),
        "security_opt": ["no-new-privileges:true"],
        "restart_policy": {"Name": "on-failure", "MaximumRetryCount": 5},
    }
    client.ensure_network(NETWORK)
    container_id = client.create(**kwargs)
    try:
        raw = docker(
            "inspect", "-f",
            "{{json .HostConfig.SecurityOpt}} {{.HostConfig.RestartPolicy.Name}} "
            "{{.HostConfig.RestartPolicy.MaximumRetryCount}}",
            container_id,
        )  # fmt: skip
        assert raw.split() == ['["no-new-privileges:true"]', "on-failure", "5"]
    finally:
        client.remove(container_id)


def test_hardening_is_read_under_the_inspect_field_names(client, container):
    """재부착 대조가 읽는 필드 이름이 `docker inspect` 와 맞는지 — 틀리면 전부 LOST (#483)."""
    found = client.hardening_of(container)

    assert found["CapDrop"] == ["ALL"]
    assert found["ReadonlyRootfs"] is True
    assert found["User"] == "1000:1000"
    assert not found["SecurityOpt"]  # 이 스펙은 security_opt 를 싣지 않는다
    assert found["PidsLimit"] == 256  # 재부착 대조가 기대하는 DEFAULT_PID_LIMIT
    # systempaths=unconfined 는 SecurityOpt 가 아니라 이 둘을 비운다 — 기본은 비어 있지 않다
    assert found["MaskedPaths"]
    assert found["ReadonlyPaths"]
    assert not found["Privileged"]
    assert not found["CapAdd"]


async def test_a_container_built_from_the_spec_passes_the_engine_check(client, echo_image):  # noqa: F811
    """기동 스펙(`build_container_spec`)이 만든 컨테이너를 대조가 거절하지 않는다 (#483).

    손으로 쓴 kwargs 가 아니라 실제 스펙이어야 스펙과 대조의 어긋남(표기 · 빈 값)이 드러난다.
    """
    from aaestro.runtime.docker.engine import DockerEngine
    from tests.integration.runtime.test_docker_lifecycle import echo_spec

    client.ensure_image(echo_image)
    client.ensure_network(NETWORK)
    spec = echo_spec(image=echo_image, network=NETWORK)
    docker("rm", "-f", spec.name, check=False)
    container_id = client.create(**spec.to_docker_kwargs())
    try:
        await DockerEngine(client=client, network=NETWORK).verify_isolation(
            "echo", echo_image, container_id, spec.hardening_kwargs()
        )
    finally:
        client.remove(container_id)
