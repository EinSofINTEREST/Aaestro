"""Unit tests for container spec derivation.

보안 기본값은 선언 여부와 무관하게 항상 적용돼야 한다 — 빠뜨려서 취약해지는
경로를 없애는 것이 이 계층의 목적이다.
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError
from aaestro.runtime.spec import (
    A2A_HOST_ENV,
    A2A_PORT_ENV,
    DEFAULT_CONTROL_PORT,
    DEFAULT_NETWORK,
    DEFAULT_PID_LIMIT,
    TMPFS_OPTIONS,
    build_container_spec,
    container_name,
)
from tests.fixtures.builders import make_manifest

GIB = 1024**3


def agent_manifest(**spec_extra):
    """runtime/a2a 만 바꿔 쓰는 manifest."""
    return make_manifest(
        spec={
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/test@0.1.0"},
            **spec_extra,
        }
    )


# --- 보안 기본값 (02 Container Standards) -----------------------------------


def test_container_runs_as_non_root():
    """base 이미지가 바뀌어도 root 로 뜨지 않도록 스펙에서 명시한다."""
    spec = build_container_spec(agent_manifest())

    assert spec.user == "1000:1000"


def test_all_capabilities_are_dropped():
    assert build_container_spec(agent_manifest()).cap_drop == ("ALL",)


def test_root_filesystem_is_read_only_with_writable_tmpfs():
    spec = build_container_spec(agent_manifest())

    assert spec.read_only_rootfs is True
    assert "/tmp" in spec.tmpfs  # noqa: S108 - 컨테이너 내부 경로


def test_the_workspace_is_declared_writable():
    """02 Security 3 은 /workspace 를 명시적 writable 경로로 규정한다."""
    assert "/workspace" in build_container_spec(agent_manifest()).tmpfs


@pytest.mark.parametrize("path", ["/tmp", "/workspace"])  # noqa: S108 - 컨테이너 내부 경로
def test_writable_paths_are_mounted_writable_by_the_container_user(path):
    """옵션을 비워 두면 root 755 로 생성되어 uid 1000 이 쓸 수 없다.

    목록에 있는지만 보면 이 결함을 놓친다 — 실제로 놓쳤고, Claude Code
    에이전트가 파일 생성 태스크에서 부딪혀 발견했다 (#136).
    """
    rendered = build_container_spec(agent_manifest()).to_docker_kwargs()

    assert rendered["tmpfs"][path] == TMPFS_OPTIONS
    assert "1777" in rendered["tmpfs"][path]


def test_the_assigned_a2a_port_reaches_the_container():
    """포트를 **열기만** 하면 컨테이너 안에서 아무도 듣지 않는다.

    agentd 는 이 값으로 서버를 띄운다 — 없으면 #166 이 만든 A2A 서버가
    실제 배포에서 영원히 안 뜬다.
    """
    spec = build_container_spec(agent_manifest(a2a={"enabled": True}), a2a_port=9137)

    assert spec.env[A2A_PORT_ENV] == "9137"
    assert any(port.name == "a2a" for port in spec.ports)


def test_the_advertised_host_matches_the_container_name():
    """peer 는 이 이름으로 접속한다 — 해석되지 않으면 카드가 무의미하다.

    컨테이너 이름은 ``aaestro-{agent}-{replica}`` 라 ``manifest.name`` 을
    광고하면 Docker DNS 가 풀지 못한다.
    """
    spec = build_container_spec(agent_manifest(a2a={"enabled": True}), a2a_port=9137)

    assert spec.env[A2A_HOST_ENV] == spec.name


def test_the_advertised_host_follows_the_replica():
    """레플리카마다 컨테이너가 다르다 — 0번만 맞으면 나머지가 닿지 않는다."""
    spec = build_container_spec(agent_manifest(a2a={"enabled": True}), a2a_port=9137, replica=2)

    assert spec.env[A2A_HOST_ENV] == spec.name
    assert spec.name.endswith("-2")


def test_no_a2a_port_is_injected_when_not_declared():
    """선언하지 않았는데 포트를 알려주면 열지도 않은 자리에 서버가 뜬다."""
    spec = build_container_spec(agent_manifest(), a2a_port=9137)

    assert A2A_PORT_ENV not in spec.env


def test_declared_secrets_survive_the_a2a_injection():
    """주입을 덧쓰다 기존 env 를 날리면 secrets 가 조용히 사라진다."""
    spec = build_container_spec(
        agent_manifest(a2a={"enabled": True}), env={"ANTHROPIC_API_KEY": "k"}, a2a_port=9137
    )

    assert spec.env["ANTHROPIC_API_KEY"] == "k"
    assert spec.env[A2A_PORT_ENV] == "9137"


def test_pid_limit_is_applied():
    """fork bomb 방지 — 선언하지 않아도 적용된다."""
    assert build_container_spec(agent_manifest()).pids_limit == DEFAULT_PID_LIMIT


def test_defaults_apply_without_any_runtime_declaration():
    """runtime 섹션을 생략해도 보안 기본값이 전부 적용된다."""
    spec = build_container_spec(agent_manifest())

    assert (spec.user, spec.read_only_rootfs, spec.cap_drop) == (
        "1000:1000",
        True,
        ("ALL",),
    )


# --- 네트워크와 포트 --------------------------------------------------------


def test_attaches_to_the_bridge_network_only():
    spec = build_container_spec(agent_manifest())

    assert spec.network == DEFAULT_NETWORK
    assert spec.to_docker_kwargs()["network_mode"] is None  # 호스트 네트워크 금지


def test_publish_all_ports_is_disabled():
    """임의 포트 publish 금지 (02 Network 규칙)."""
    assert build_container_spec(agent_manifest()).to_docker_kwargs()["publish_all_ports"] is False


def test_only_control_port_is_exposed_without_a2a():
    spec = build_container_spec(agent_manifest())

    assert [p.container_port for p in spec.ports] == [DEFAULT_CONTROL_PORT]


def test_a2a_port_is_exposed_when_enabled():
    spec = build_container_spec(agent_manifest(a2a={"enabled": True}), a2a_port=9100)

    assert {p.name for p in spec.ports} == {"control", "a2a"}
    assert [p.container_port for p in spec.ports if p.name == "a2a"] == [9100]


def test_a2a_port_is_omitted_when_runtime_assigns_none():
    """포트를 아직 할당받지 못했으면 노출하지 않는다."""
    spec = build_container_spec(agent_manifest(a2a={"enabled": True}))

    assert [p.name for p in spec.ports] == ["control"]


# --- 리소스와 이미지 --------------------------------------------------------


def test_resource_limits_come_from_the_manifest():
    spec = build_container_spec(
        agent_manifest(runtime={"resources": {"cpu": "2.0", "memory": "4Gi"}})
    )

    assert spec.cpu_cores == 2.0
    assert spec.memory_bytes == 4 * GIB
    assert spec.to_docker_kwargs()["nano_cpus"] == 2_000_000_000


def test_base_image_is_used_when_manifest_declares_none():
    spec = build_container_spec(agent_manifest(), base_image="aaestro/agent-base:0.2.0")

    assert spec.image == "aaestro/agent-base:0.2.0"


def test_manifest_image_wins():
    spec = build_container_spec(agent_manifest(runtime={"image": "aaestro/custom:1.0.0"}))

    assert spec.image == "aaestro/custom:1.0.0"


def test_a_baked_image_wins_over_the_declaration():
    """#266 — 재료가 있는 에이전트는 빌드 단계가 정한 태그로만 돈다."""
    spec = build_container_spec(
        agent_manifest(runtime={"image": "aaestro/custom:1.0.0"}),
        image="aaestro/agent-custom:0.1.0",
    )

    assert spec.image == "aaestro/agent-custom:0.1.0"


# --- env 주입 ---------------------------------------------------------------


def test_only_provided_env_is_injected():
    """스코프 해석은 앞 단계에서 끝나고, 여기서는 결과만 싣는다."""
    spec = build_container_spec(agent_manifest(), env={"ANTHROPIC_API_KEY": "v"})

    assert spec.env == {"ANTHROPIC_API_KEY": "v"}


def test_env_defaults_to_empty():
    assert build_container_spec(agent_manifest()).env == {}


# --- 볼륨과 라벨 ------------------------------------------------------------


def test_no_volumes_by_default():
    """기본은 볼륨 없음 — 필요한 경우에만 명시 선언 (02 Volumes 규칙)."""
    assert build_container_spec(agent_manifest()).volumes == ()


def test_declared_volumes_are_carried():
    spec = build_container_spec(
        agent_manifest(runtime={"volumes": [{"name": "work", "mount_path": "/workspace"}]})
    )

    assert spec.volumes[0]["mount_path"] == "/workspace"


def test_labels_identify_the_agent_and_group():
    spec = build_container_spec(
        make_manifest(metadata={"name": "researcher", "version": "0.1.0", "group": "research"})
    )

    assert spec.labels["aaestro.agent"] == "researcher"
    assert spec.labels["aaestro.group"] == "research"


def test_group_label_falls_back_to_global():
    assert build_container_spec(agent_manifest()).labels["aaestro.group"] == "global"


# --- 이름 ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("replica", "expected"),
    [(0, "aaestro-researcher-0"), (2, "aaestro-researcher-2")],
)
def test_container_names_are_unique_per_replica(replica, expected):
    assert container_name("researcher", replica) == expected


def test_spec_name_matches_the_replica():
    spec = build_container_spec(agent_manifest(), replica=1)

    assert spec.name.endswith("-1")
    assert spec.labels["aaestro.replica"] == "1"


# --- 격리 강제 --------------------------------------------------------------


@pytest.mark.parametrize("network", ["host", "bridge", "none", "container"])
def test_shared_networks_are_rejected(network):
    """호스트/공유 네트워크를 쓰면 컨테이너 격리가 무너진다."""
    with pytest.raises(AaestroError) as exc_info:
        build_container_spec(agent_manifest(), network=network)

    assert exc_info.value.code == "RT_001"


def test_dedicated_bridge_network_is_accepted():
    assert build_container_spec(agent_manifest(), network="aaestro-net").network == "aaestro-net"


def test_ports_bind_to_loopback_only():
    """호스트 IP 를 지정하지 않으면 0.0.0.0 에 바인딩되어 외부에 노출된다."""
    ports = build_container_spec(agent_manifest()).to_docker_kwargs()["ports"]

    assert all(binding[0] == "127.0.0.1" for binding in ports.values())


# --- Docker 인자 렌더링 -----------------------------------------------------


def test_declared_volumes_reach_docker_kwargs():
    """스펙이 볼륨을 들고 있어도 렌더링에서 빠지면 마운트가 무시된다."""
    kwargs = build_container_spec(
        agent_manifest(runtime={"volumes": [{"name": "work", "mount_path": "/workspace"}]})
    ).to_docker_kwargs()

    assert kwargs["volumes"] == {"aaestro-test-agent.work": {"bind": "/workspace", "mode": "rw"}}


def test_read_only_volume_mode_is_carried():
    kwargs = build_container_spec(
        agent_manifest(
            runtime={"volumes": [{"name": "ref", "mount_path": "/ref", "read_only": True}]}
        )
    ).to_docker_kwargs()

    assert kwargs["volumes"]["aaestro-test-agent.ref"]["mode"] == "ro"


def _volume_sources(agent: str, volume: str, replica: int = 0) -> set[str]:
    manifest = make_manifest(
        metadata={"name": agent},
        spec={"runtime": {"volumes": [{"name": volume, "mount_path": "/data"}]}},
    )
    return set(build_container_spec(manifest, replica=replica).to_docker_kwargs()["volumes"])


def test_same_volume_name_in_two_agents_is_two_docker_volumes():
    """두 에이전트가 같은 이름을 선언해도 볼륨을 공유하지 않는다 — 02 Volumes (#513)."""
    assert _volume_sources("a", "work") == {"aaestro-a.work"}
    assert _volume_sources("b", "work") == {"aaestro-b.work"}


def test_replicas_of_one_agent_share_its_volume():
    assert _volume_sources("a", "work", replica=0) == _volume_sources("a", "work", replica=3)


def test_agent_and_volume_boundary_cannot_collide():
    """`-` 로 이으면 a + b-work 와 a-b + work 가 겹친다 — 구분자는 에이전트 이름에 없는 `.`."""
    assert _volume_sources("a", "b-work") != _volume_sources("a-b", "work")


def test_reattach_expects_the_same_volume_name_it_creates_with():
    spec = build_container_spec(
        agent_manifest(runtime={"volumes": [{"name": "work", "mount_path": "/workspace"}]})
    )

    assert spec.hardening_kwargs()["volumes"] == spec.to_docker_kwargs()["volumes"]


def test_no_volumes_renders_an_empty_mapping():
    assert build_container_spec(agent_manifest()).to_docker_kwargs()["volumes"] == {}


@pytest.mark.parametrize(
    ("cpu", "expected"),
    [("0.1", 100_000_000), ("0.5", 500_000_000), ("1.0", 1_000_000_000), ("2.5", 2_500_000_000)],
)
def test_nano_cpus_rounds_without_loss(cpu, expected):
    """int() 절삭은 부동소수 표현에 따라 CPU 를 덜 할당할 수 있다."""
    kwargs = build_container_spec(
        agent_manifest(runtime={"resources": {"cpu": cpu}})
    ).to_docker_kwargs()

    assert kwargs["nano_cpus"] == expected


def test_to_docker_kwargs_sets_no_new_privileges():
    """setuid 바이너리로 uid 1000 에서 권한이 올라가는 길을 막는다 (#475).

    cap-drop ALL 만으로는 setuid 실행 시 얻는 권한을 막지 못한다.
    """
    rendered = build_container_spec(agent_manifest()).to_docker_kwargs()

    assert "no-new-privileges:true" in rendered["security_opt"]
