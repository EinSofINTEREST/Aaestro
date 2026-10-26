"""Container specification from an agent manifest.

Manifest 를 Docker 컨테이너 스펙으로 변환한다. 02 의 보안 기본값
(non-root / cap-drop ALL / read-only rootfs / PID limit) 을 코드로 강제해,
선언에서 빠뜨려도 안전하지 않은 컨테이너가 만들어지지 않게 한다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

DEFAULT_NETWORK: Final = "aaestro-net"
DEFAULT_CONTROL_PORT: Final = 8080
HARDENED_USER = "1000:1000"
HARDENED_CAP_DROP: tuple[str, ...] = ("ALL",)
HARDENED_SECURITY_OPT: tuple[str, ...] = ()
"""에이전트 컨테이너가 싣는 security_opt — `build_container_spec` 이 스펙에 싣는다."""
DEFAULT_PID_LIMIT: Final = 256


DEFAULT_BASE_IMAGE: Final = "aaestro/agent-base:0.1.0"

A2A_PORT_ENV: Final = "AAESTRO_A2A_PORT"
"""할당된 A2A 포트를 컨테이너에 알리는 키 — agentd 가 이것으로 서버를 띄운다."""

A2A_HOST_ENV: Final = "AAESTRO_A2A_ADVERTISED_HOST"
# 아래 셋은 agentd.a2a_server 의 계약과 같은 값이어야 한다 — runtime 이 agentd 를
# import 하지 않도록 여기 다시 적고, 단위 테스트가 두 쪽의 드리프트를 막는다
A2A_EDGES_ENV: Final = "AAESTRO_A2A_EDGES"
A2A_SECRET_ENV: Final = "AAESTRO_A2A_SECRET"  # noqa: S105 — 키 이름이지 값이 아니다
A2A_PEERS_ENV: Final = "AAESTRO_A2A_PEERS"
"""카드에 실을 도달 주소 — peer 가 이 이름으로 접속한다."""

# 컨테이너가 쓸 수 있어야 하는 임시 경로 — read-only rootfs 위의 tmpfs.
#
# **작업대이지 출력함이 아니다**: tmpfs 라 재시작 시 소멸하고, 다음 노드는
# 다른 컨테이너라 여기 남긴 파일을 보지 못한다. 산출물은 graph state 또는
# artifact 저장소로 간다 (02 Statelessness / Output Discipline). 정당한 용도는
# 태스크 한 번 안의 scratch 뿐 — 02 가 허용하는 "재시작 시 소실 전제" 범위다
_WRITABLE_TMPFS: Final = ("/tmp", "/workspace")  # noqa: S108 - 컨테이너 내부 경로

TMPFS_OPTIONS: Final = "mode=1777"
"""tmpfs 마운트 옵션.

옵션을 비워 두면 root 소유 755 로 생성되어 **컨테이너 사용자(uid 1000)가 쓸 수
없다.** ``/tmp`` 만 우연히 동작하는데, Docker 가 그 경로에만 1777 을 주기
때문이다 — ``/workspace`` 는 그 예외에 해당하지 않아 조용히 막혔다.

sticky bit(1) 는 유지한다: 컨테이너 안에 사용자는 하나뿐이지만, 표준
``/tmp`` 의미와 어긋나게 둘 이유가 없다.
"""

# 컨테이너가 호스트 네트워크/PID/IPC 를 공유하면 격리가 무너진다
_FORBIDDEN_NETWORKS: Final = frozenset({"host", "none", "bridge", "container"})

# 노출 포트는 루프백에만 바인딩한다 — 0.0.0.0 이면 컨테이너 밖 누구나 접근한다
LOOPBACK: Final = "127.0.0.1"


@dataclass(frozen=True)
class PortBinding:
    """A container port exposed to the runtime.

    노출 포트. Control 포트는 runtime 만, A2A 포트는 allowlist peer 만 접근한다.
    """

    name: str
    container_port: int


@dataclass(frozen=True)
class ContainerSpec:
    """Everything needed to create one agent container.

    에이전트 컨테이너 생성에 필요한 전부. Docker SDK 호출 인자로 그대로 옮겨진다.
    """

    name: str
    image: str
    env: Mapping[str, str]
    network: str
    ports: tuple[PortBinding, ...]
    cpu_cores: float
    memory_bytes: int
    pids_limit: int
    user: str
    read_only_rootfs: bool
    cap_drop: tuple[str, ...]
    tmpfs: tuple[str, ...]
    volumes: tuple[Mapping[str, Any], ...] = ()
    labels: Mapping[str, str] = field(default_factory=dict)
    extra_networks: tuple[str, ...] = ()
    """기동 전에 더 붙는 네트워크 — 이그레스 프록시 없는 배포의 자기 사이드카 네트워크 (#304).
    SDK 의 생성 인자는 네트워크를 하나만 받으므로 엔진이 따로 붙인다."""
    security_opt: tuple[str, ...] = ()

    def hardening_kwargs(self) -> dict[str, Any]:
        """이 컨테이너의 경화 생성 인자 — 기동 kwargs 와 재부착 대조의 단일 출처.

        `to_docker_kwargs` 가 이것을 그대로 싣고, 재부착은 같은 매니페스트로 스펙을 다시 만들어
        이것을 기대값으로 쓴다 (`engine.hardening_gaps`). 값은 한 방향으로만 흐른다 — 검사 쪽에
        사본을 두면 스펙이 바뀔 때 정상 컨테이너가 전부 거절된다.
        """
        return {
            "pids_limit": self.pids_limit,
            "user": self.user,
            "read_only": self.read_only_rootfs,
            "cap_drop": list(self.cap_drop),
            "security_opt": list(self.security_opt),
        }

    def to_docker_kwargs(self) -> dict[str, Any]:
        """Render arguments for the Docker SDK.

        Docker SDK 의 컨테이너 생성 인자로 변환합니다.
        """
        return {
            "name": self.name,
            "image": self.image,
            "environment": dict(self.env),
            "network": self.network,
            # (host_ip, None) 로 루프백 바인딩 — 포트를 외부에 노출하지 않는다
            "ports": {f"{p.container_port}/tcp": (LOOPBACK, None) for p in self.ports},
            "nano_cpus": round(self.cpu_cores * 1_000_000_000),
            "mem_limit": self.memory_bytes,
            **self.hardening_kwargs(),
            "tmpfs": dict.fromkeys(self.tmpfs, TMPFS_OPTIONS),
            "volumes": {
                volume["name"]: {
                    "bind": volume["mount_path"],
                    "mode": "ro" if volume["read_only"] else "rw",
                }
                for volume in self.volumes
            },
            "labels": dict(self.labels),
            # 호스트 네트워크 금지, 임의 포트 publish 금지 (02 Network 규칙)
            "network_mode": None,
            "publish_all_ports": False,
        }


def build_container_spec(
    manifest: AgentManifest,
    *,
    env: Mapping[str, str] | None = None,
    replica: int = 0,
    network: str = DEFAULT_NETWORK,
    a2a_port: int | None = None,
    base_image: str = DEFAULT_BASE_IMAGE,
    mounts: Sequence[Mapping[str, Any]] = (),
    image: str | None = None,
    extra_networks: Sequence[str] = (),
) -> ContainerSpec:
    """Derive a container spec from an agent manifest.

    Manifest 로부터 컨테이너 스펙을 도출합니다. 보안 기본값은 선언 여부와
    무관하게 항상 적용됩니다 — 빠뜨려서 취약해지는 경로를 없애기 위함입니다.

    Args:
        manifest: The validated agent manifest.
        env: Resolved secret values to inject (scope resolution happens earlier).
        replica: Replica index, used to make container names unique.
        network: Dedicated bridge network to attach; shared/host networks are rejected.
        a2a_port: A2A port assigned by the runtime, when A2A is enabled.
        base_image: Image used when the manifest declares none.
        mounts: Read-only binds the runtime adds on top of the manifest's
            volumes — 선언(manifest, modules)을 base 이미지에 들여보내는 통로다.
            02 Rule 2 의 declarative agent 는 이미지를 굽지 않으므로 runtime 이
            선언을 실어 줘야 한다 (#243).
        image: The image the framework baked for this agent (#266). 재료가 있는
            커스텀 에이전트는 빌드 단계가 정한 태그로만 돈다 — 매니페스트 선언보다
            우선한다. 둘이 다르면 배포 검증이 먼저 거절한다.
        extra_networks: Networks joined before start — 이그레스 프록시 없는 배포에서 자기
            사이드카에 닿는 길 (#304). 공유·호스트 네트워크는 여기서도 거절한다.

    Returns:
        The container specification.

    Raises:
        AaestroError: RUNTIME/``RT_001`` if a shared or host network is requested.
    """
    if network in _FORBIDDEN_NETWORKS:
        raise AaestroError(
            category=ErrorCategory.RUNTIME,
            code=ErrorCode.RT_001,
            message=f"agent containers must not use the '{network}' network",
            agent=manifest.name,
            details={"network": network},
        )

    for extra in extra_networks:
        if extra in _FORBIDDEN_NETWORKS:
            raise AaestroError(
                category=ErrorCategory.RUNTIME,
                code=ErrorCode.RT_001,
                message=f"agent containers must not use the '{extra}' network",
                agent=manifest.name,
                details={"network": extra},
            )

    runtime = manifest.spec.runtime

    ports = [PortBinding("control", DEFAULT_CONTROL_PORT)]
    resolved_env = dict(env or {})
    if manifest.spec.a2a.enabled and a2a_port is not None:
        ports.append(PortBinding("a2a", a2a_port))
        # 포트를 **열기만** 하면 컨테이너 안에서 아무도 듣지 않는다 — 번호를
        # 알려줘야 agentd 가 그 자리에 서버를 띄운다 (#166 / 03 Rule 2)
        resolved_env[A2A_PORT_ENV] = str(a2a_port)
        # 광고 주소는 **컨테이너가 실제로 응답하는 이름**이어야 한다.
        # 컨테이너 이름은 aaestro-{agent}-{replica} 라 manifest.name 으로는
        # 해석되지 않는다 — 별칭을 달아 두 이름 모두 닿게 한다
        resolved_env[A2A_HOST_ENV] = container_name(manifest.name, replica)

    volumes = tuple(
        {
            "name": volume.name,
            "mount_path": volume.mount_path,
            "read_only": volume.read_only,
        }
        for volume in runtime.volumes
    ) + tuple(dict(mount) for mount in mounts)

    return ContainerSpec(
        name=container_name(manifest.name, replica),
        image=image or runtime.image or base_image,
        env=resolved_env,
        network=network,
        ports=tuple(ports),
        cpu_cores=runtime.resources.cpu_cores,
        memory_bytes=runtime.resources.memory_bytes,
        pids_limit=DEFAULT_PID_LIMIT,
        # base 이미지가 non-root 로 실행되지만, 스펙에서도 명시해 이미지 변경에
        # 관계없이 root 로 뜨지 않게 한다
        user=HARDENED_USER,
        read_only_rootfs=True,
        cap_drop=HARDENED_CAP_DROP,
        security_opt=HARDENED_SECURITY_OPT,
        tmpfs=_WRITABLE_TMPFS,
        volumes=volumes,
        labels={
            "aaestro.agent": manifest.name,
            "aaestro.agent_version": manifest.metadata.version,
            "aaestro.group": manifest.group,
            "aaestro.replica": str(replica),
        },
        extra_networks=tuple(extra_networks),
    )


def container_name(agent: str, replica: int = 0) -> str:
    """Build the container name for an agent replica.

    에이전트 레플리카의 컨테이너 이름을 만듭니다.
    """
    return f"aaestro-{agent}-{replica}"
