"""Unit tests for MCP sidecar lifecycle (#304)."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.runtime.docker.errors import NetworkIsolationError
from aaestro.runtime.sidecars import (
    McpSidecars,
    ProxyAttachment,
    sidecar_env,
    sidecar_network,
)
from aaestro.runtime.spec import DEFAULT_PID_LIMIT
from tests.fixtures.builders import make_manifest
from tests.fixtures.fake_docker import FakeDockerClient

PROXY = ProxyAttachment(container="aaestro-egress-1", alias="aaestro-egress")


def with_servers(*servers, env_allowlist=()):
    return make_manifest(
        metadata={"name": "researcher", "version": "0.1.0"},
        spec={
            "mcp": {"servers": list(servers)},
            "runtime": {"env_allowlist": list(env_allowlist)},
        },
    )


BROWSER = {
    "name": "browser",
    "transport": "streamable-http",
    "sidecar": {
        "image": "mcp/playwright:1.2.0",
        "resources": {"cpu": "0.5", "memory": "512Mi"},
        "port": 3000,
    },
    "env_allowlist": ["BROWSER_KEY"],
}
SEARCH = {
    "name": "search",
    "transport": "streamable-http",
    "sidecar": {"image": "mcp/search:0.3.0"},
}
AGENT_ENV = {
    "BROWSER_KEY": "k-browser",
    "OTHER_SECRET": "k-other",
    "ANTHROPIC_API_KEY": "identity",
    "AAESTRO_ACCESS_CREDENTIAL": "identity",
    "HTTPS_PROXY": "http://researcher:identity@aaestro-egress:8080",
    "https_proxy": "http://researcher:identity@aaestro-egress:8080",
}


def sidecar_ids(docker: FakeDockerClient) -> list[str]:
    return [
        f"container-{index:04d}" + "0" * 20
        for index, kwargs in enumerate(docker.created, start=1)
        if kwargs["labels"].get("aaestro.role") == "mcp-sidecar"
    ]


async def test_start_runs_each_sidecar_hardened_on_its_own_internal_network():
    docker = FakeDockerClient()
    manifest = with_servers(BROWSER, SEARCH, env_allowlist=["BROWSER_KEY"])

    await McpSidecars(docker).start(manifest, AGENT_ENV)

    network = "aaestro-researcher--mcp"
    assert sidecar_network("researcher") == network
    assert (network, True) in zip(docker.networks, docker.internal_requests, strict=True)
    browser, search = docker.created
    assert browser["name"] == "aaestro-researcher--mcp-browser"
    assert search["name"] == "aaestro-researcher--mcp-search"
    assert browser["network"] == network
    assert browser["ports"] == {}
    assert browser["user"] == "1000:1000"
    assert browser["read_only"] is True
    assert browser["cap_drop"] == ["ALL"]
    assert browser["security_opt"] == ["no-new-privileges:true"]
    assert browser["restart_policy"] == {"Name": "on-failure", "MaximumRetryCount": 5}
    assert browser["nano_cpus"] == 500_000_000
    assert browser["mem_limit"] == 512 * 1024 * 1024
    assert search["nano_cpus"] == 1_000_000_000  # 미선언은 프레임워크 기본값 (02 Resources)
    assert docker.started == [f"container-{i:04d}" + "0" * 20 for i in (1, 2)]


def test_a_sidecar_gets_only_its_allowlist_and_the_egress_wiring():
    manifest = with_servers(BROWSER, env_allowlist=["BROWSER_KEY"])

    env = sidecar_env(manifest.spec.mcp.servers[0], AGENT_ENV)

    # 신원은 프록시 배선에만 실린다 — 모델 키 자리·레지스트리 자격 이름으로는 넘기지 않는다
    assert env == {
        "BROWSER_KEY": "k-browser",
        "HTTPS_PROXY": AGENT_ENV["HTTPS_PROXY"],
        "https_proxy": AGENT_ENV["https_proxy"],
        "HOME": "/tmp",  # noqa: S108 — 컨테이너 안 tmpfs
    }


async def test_with_the_proxy_only_the_proxy_joins_the_sidecar_network():
    docker = FakeDockerClient()
    sidecars = McpSidecars(docker, proxy=PROXY)
    manifest = with_servers(SEARCH)

    await sidecars.start(manifest, {})

    assert docker.connected == [("aaestro-researcher--mcp", PROXY.container, (PROXY.alias,))]
    assert sidecars.agent_networks(manifest) == ()


async def test_without_the_proxy_the_agent_joins_its_own_sidecar_network():
    docker = FakeDockerClient()
    sidecars = McpSidecars(docker)
    manifest = with_servers(SEARCH)

    await sidecars.start(manifest, {})

    assert docker.connected == []
    assert sidecars.agent_networks(manifest) == ("aaestro-researcher--mcp",)


async def test_an_agent_without_sidecars_touches_nothing():
    docker = FakeDockerClient()
    sidecars = McpSidecars(docker, proxy=PROXY)
    manifest = with_servers(
        {"name": "corp", "transport": "streamable-http", "url": "https://mcp.example.com/mcp"}
    )

    await sidecars.start(manifest, AGENT_ENV)

    assert docker.created == [] and docker.networks == [] and docker.connected == []
    assert sidecars.agent_networks(manifest) == ()


async def test_stop_removes_the_sidecars_but_not_the_agent_replicas():
    docker = FakeDockerClient()
    sidecars = McpSidecars(docker, proxy=PROXY)
    # 같은 에이전트 라벨을 단 레플리카 컨테이너 — 사이드카 정리가 건드리면 안 된다
    replica = docker.create(name="aaestro-researcher-0", labels={"aaestro.agent": "researcher"})
    await sidecars.start(with_servers(BROWSER, SEARCH, env_allowlist=["BROWSER_KEY"]), AGENT_ENV)
    started = sidecar_ids(docker)

    await sidecars.stop("researcher")

    assert len(started) == 2
    assert replica not in docker.removed
    assert sorted(docker.removed) == sorted(started)
    assert docker.disconnected == [("aaestro-researcher--mcp", PROXY.container)]
    assert docker.removed_networks == ["aaestro-researcher--mcp"]


async def test_stop_keeps_cleaning_when_a_step_fails():
    class Flaky(FakeDockerClient):
        def disconnect(self, network, container):
            raise RuntimeError("daemon hiccup")

    docker = Flaky()
    sidecars = McpSidecars(docker, proxy=PROXY)
    await sidecars.start(with_servers(SEARCH), {})

    await sidecars.stop("researcher")

    assert docker.removed and docker.removed_networks == ["aaestro-researcher--mcp"]


async def test_a_deploy_replaces_a_leftover_sidecar_but_reattach_reuses_a_sound_one():
    docker = FakeDockerClient()
    sidecars = McpSidecars(docker)
    manifest = with_servers(SEARCH)
    await sidecars.start(manifest, {})
    first = docker.find("aaestro-researcher--mcp-search")

    await sidecars.start(manifest, {}, reuse=True)
    assert docker.find("aaestro-researcher--mcp-search") == first

    await sidecars.start(manifest, {})
    assert first in docker.removed
    assert docker.find("aaestro-researcher--mcp-search") not in (None, first)


async def test_reattach_replaces_a_sidecar_that_is_not_running():
    docker = FakeDockerClient(state={"Running": False, "ExitCode": 1})
    sidecars = McpSidecars(docker)
    manifest = with_servers(SEARCH)
    await sidecars.start(manifest, {})
    first = docker.find("aaestro-researcher--mcp-search")

    await sidecars.start(manifest, {}, reuse=True)

    assert first in docker.removed


async def test_reattach_replaces_a_sidecar_attached_to_another_network():
    docker = FakeDockerClient(attached=("aaestro-researcher--mcp", "bridge"))
    sidecars = McpSidecars(docker)
    manifest = with_servers(SEARCH)
    await sidecars.start(manifest, {})
    first = docker.find("aaestro-researcher--mcp-search")

    await sidecars.start(manifest, {}, reuse=True)

    assert first in docker.removed


SOUND_SIDECAR_HARDENING = {
    "CapDrop": ["ALL"],
    "ReadonlyRootfs": True,
    "User": "1000:1000",
    "SecurityOpt": ["no-new-privileges:true"],
    "PidsLimit": DEFAULT_PID_LIMIT,
}


@pytest.mark.parametrize(
    "hardening",
    [
        {"CapDrop": None, "ReadonlyRootfs": True, "User": "1000:1000"},
        {"CapDrop": ["ALL"], "ReadonlyRootfs": True, "User": "1000:1000", "Privileged": True},
        {**SOUND_SIDECAR_HARDENING, "PidsLimit": 0},
        {
            **SOUND_SIDECAR_HARDENING,
            "SecurityOpt": ["no-new-privileges:true", "seccomp=unconfined"],
        },
    ],
)
async def test_reattach_replaces_a_sidecar_that_lost_its_hardening(hardening):
    docker = FakeDockerClient()
    sidecars = McpSidecars(docker)
    manifest = with_servers(SEARCH)
    await sidecars.start(manifest, {})
    first = docker.find("aaestro-researcher--mcp-search")
    docker._hardening = hardening

    await sidecars.start(manifest, {}, reuse=True)

    assert first in docker.removed


async def test_a_sidecar_that_fails_to_start_is_removed_and_reported():
    docker = FakeDockerClient(start_error=RuntimeError("boom"))

    with pytest.raises(AaestroError) as caught:
        await McpSidecars(docker).start(with_servers(SEARCH), {})

    assert caught.value.code == ErrorCode.RT_001
    assert caught.value.details["mcp_server"] == "search"
    assert docker.removed == ["container-0001" + "0" * 20]


async def test_a_missing_sidecar_image_is_rt_004():
    docker = FakeDockerClient(image_error=RuntimeError("pull denied"))

    with pytest.raises(AaestroError) as caught:
        await McpSidecars(docker).start(with_servers(SEARCH), {})

    assert caught.value.code == ErrorCode.RT_004
    assert docker.created == []


async def test_a_sidecar_network_that_is_not_internal_is_refused():
    docker = FakeDockerClient(
        network_error=NetworkIsolationError("aaestro-researcher--mcp", expected=True, actual=False)
    )

    with pytest.raises(AaestroError) as caught:
        await McpSidecars(docker, proxy=PROXY).start(with_servers(SEARCH), {})

    assert caught.value.code == ErrorCode.RT_001
    assert docker.created == [] and docker.connected == []
