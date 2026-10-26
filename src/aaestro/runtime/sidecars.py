"""MCP sidecars — an agent's HTTP MCP servers in containers of their own (#304).

03 MCP 패턴 2. 사이드카는 **소유 에이전트 전용**이고 lifecycle 을 그 에이전트와 함께한다:

- 에이전트마다 외부 경로 없는 사이드카 네트워크(``aaestro-{agent}--mcp``)를 따로 두고 사이드카는
  거기에만 붙는다. 에이전트 네트워크에 두면 다른 에이전트도 닿고, 이그레스 프록시를 켠 배포에서는
  에이전트가 프록시를 건너뛰어 도구 단위 판정(`mcp_tool`)을 피한다 — 컨테이너 안의 검사는 강제가
  아니므로 막는 것은 네트워크다
- 이그레스 프록시가 켜져 있으면 그 네트워크에 붙는 것은 **프록시뿐**이다. 에이전트는 프록시의
  ``/mcp/{server}`` 로 부르고, 프록시가 도구마다 판정해 사이드카로 보낸다. 사이드카의 바깥 호출도
  같은 프록시를 거친다 (소유 에이전트의 신원으로, 목적지 단위 판정)
- 프록시가 없으면 에이전트 레플리카가 그 네트워크에 직접 붙는다 — 판정 지점이 없는 배포이므로
  지킬 것은 1:1 배치(03 Placement 1)뿐이다

주소는 선언이 아니라 이름 규칙에서 나온다 (`sidecar_url`) — runtime 이 그 이름으로 띄운다.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

import structlog

from aaestro.core.manifest import ResourceSpec, sidecar_host
from aaestro.runtime.docker.engine import AGENT_LABEL, hardening_gaps, inspect_hardening
from aaestro.runtime.docker.errors import (
    NetworkIsolationError,
    image_unavailable,
    network_mismatch,
    start_failed,
)
from aaestro.runtime.spec import (
    DEFAULT_PID_LIMIT,
    HARDENED_CAP_DROP,
    HARDENED_SECURITY_OPT,
    HARDENED_USER,
    TMPFS_OPTIONS,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from aaestro.core.manifest import AgentManifest, McpServerSpec
    from aaestro.runtime.docker.engine import DockerClient

log = structlog.get_logger(__name__)

SIDECAR_LABEL: Final = "aaestro.mcp_sidecar"
"""값은 서버 이름."""
ROLE_LABEL: Final = "aaestro.role"
SIDECAR_ROLE: Final = "mcp-sidecar"
"""해체는 선언이 아니라 (에이전트, 역할) 라벨로 찾는다 — 선언이 바뀌어도 남김없이, 그리고 같은
에이전트 라벨을 단 레플리카 컨테이너는 건드리지 않게."""

SIDECAR_RESTART: Final = {"Name": "on-failure", "MaximumRetryCount": 5}
"""죽은 사이드카는 Docker 가 다시 세운다 — 상한을 두어 crash loop 가 끝없이 돌지 않게."""

SIDECAR_HOME: Final = "/tmp"  # noqa: S108 — 컨테이너 안 경로, read-only rootfs 위의 tmpfs
"""이미지가 기대하는 홈이 읽기 전용이면 서버가 뜨다 넘어진다 — 쓸 수 있는 tmpfs 를 준다."""

PROXY_ENV_KEYS: Final = ("HTTPS_PROXY", "https_proxy")
"""사이드카가 소유 에이전트에게서 물려받는 이그레스 배선 — 이 밖의 에이전트 env 는 주지 않는다."""


def sidecar_network(agent: str) -> str:
    """The agent's sidecar network — 이 에이전트의 사이드카와 (있으면) 이그레스 프록시만 붙는다."""
    return f"aaestro-{agent}--mcp"


def sidecars_of(manifest: AgentManifest) -> tuple[McpServerSpec, ...]:
    """이 에이전트가 선언한 사이드카 서버."""
    return tuple(s for s in manifest.spec.mcp.servers if s.sidecar is not None)


def sidecar_env(server: McpServerSpec, agent_env: Mapping[str, str]) -> dict[str, str]:
    """What the sidecar process gets — the server's own allowlist and the egress wiring.

    에이전트 env 전부가 아니라 서버 선언의 ``env_allowlist`` 에 든 키만 넘긴다 (03 Security 5).
    이그레스 프록시 배선은 물려준다: 사이드카는 소유 에이전트의 일부이므로 같은 신원으로 같은 판정을
    받는다. 프록시가 없는 배포면 넘길 배선도 없고 사이드카에는 바깥 길이 없다.
    """
    env = {key: agent_env[key] for key in server.env_allowlist if key in agent_env}
    env.update({key: agent_env[key] for key in PROXY_ENV_KEYS if key in agent_env})
    env["HOME"] = SIDECAR_HOME
    return env


def sidecar_kwargs(
    agent: str, server: McpServerSpec, env: Mapping[str, str], network: str
) -> dict[str, Any]:
    """Docker create arguments for one sidecar — 에이전트 컨테이너와 같은 보안 기본값.

    포트는 게시하지 않는다: 닿는 쪽은 같은 사이드카 네트워크 안의 프록시(또는 에이전트)뿐이다.
    """
    if server.sidecar is None:
        raise ValueError(f"mcp server is not a sidecar: {server.name}")
    resources = server.sidecar.resources or ResourceSpec()
    return {
        "name": sidecar_host(agent, server.name),
        "image": server.sidecar.image,
        "environment": dict(env),
        "network": network,
        "ports": {},
        "nano_cpus": round(resources.cpu_cores * 1_000_000_000),
        "mem_limit": resources.memory_bytes,
        "pids_limit": DEFAULT_PID_LIMIT,
        # 02 Security — 이미지가 root 로 뜨도록 만들어졌어도 non-root 로 돌린다
        "user": HARDENED_USER,
        "read_only": True,
        "cap_drop": list(HARDENED_CAP_DROP),
        "security_opt": list(HARDENED_SECURITY_OPT),
        "tmpfs": {SIDECAR_HOME: TMPFS_OPTIONS},
        "volumes": {},
        "labels": {AGENT_LABEL: agent, ROLE_LABEL: SIDECAR_ROLE, SIDECAR_LABEL: server.name},
        "restart_policy": dict(SIDECAR_RESTART),
        "network_mode": None,
        "publish_all_ports": False,
    }


@dataclass(frozen=True)
class ProxyAttachment:
    """The egress proxy container and the name agents reach it by.

    사이드카 네트워크에 프록시를 붙일 때 같은 이름(별칭)을 준다 — 사이드카가 물려받은
    ``HTTPS_PROXY`` 가 그 이름을 가리킨다.
    """

    container: str
    alias: str


@dataclass
class McpSidecars:
    """Starts and removes one agent's sidecars on its own network.

    Attributes:
        client: Docker 호출 계층.
        proxy: 이그레스 프록시 — 있으면 사이드카 네트워크에 붙는 것은 프록시뿐이다. None 이면
            에이전트 레플리카가 직접 붙는다 (`agent_networks`).
    """

    client: DockerClient
    proxy: ProxyAttachment | None = None

    def agent_networks(self, manifest: AgentManifest) -> tuple[str, ...]:
        """에이전트 컨테이너가 더 붙을 네트워크 — 프록시가 부르는 배포면 없다."""
        if self.proxy is not None or not sidecars_of(manifest):
            return ()
        return (sidecar_network(manifest.name),)

    async def start(
        self, manifest: AgentManifest, agent_env: Mapping[str, str], *, reuse: bool = False
    ) -> None:
        """Bring up every sidecar the agent declares, before the agent.

        에이전트보다 먼저 세운다 — agentd 가 기동하며 세션을 연다 (03 Startup Sequence 3).

        Args:
            manifest: The agent's declaration.
            agent_env: The env the agent container gets — 사이드카는 여기서 제 몫만 받는다.
            reuse: 떠 있는 사이드카를 그대로 쓴다 (재부착). 배포는 새로 세운다 — 이전 기동이 남긴
                컨테이너가 다른 이미지·env 로 돌고 있을 수 있다.

        Raises:
            AaestroError: RUNTIME/``RT_004`` 이미지 확보 실패, ``RT_001`` 네트워크·기동 실패.
        """
        servers = sidecars_of(manifest)
        if not servers:
            return
        agent = manifest.name
        network = sidecar_network(agent)
        await self._network(agent, servers[0], network)
        for server in servers:
            await self._ensure(agent, server, sidecar_env(server, agent_env), network, reuse=reuse)

    async def stop(self, agent: str) -> None:
        """Remove the agent's sidecars and their network — 정리는 실패해도 끝까지 간다.

        라벨로 찾는다: 선언이 바뀌었거나 읽을 수 없어도 이 에이전트의 사이드카를 남기지 않는다.
        """
        network = sidecar_network(agent)
        try:
            labeled = await asyncio.to_thread(
                self.client.labeled, {AGENT_LABEL: agent, ROLE_LABEL: SIDECAR_ROLE}
            )
        except Exception as err:  # noqa: BLE001 — 찾지 못해도 네트워크 정리는 한다; 로그로 드러낸다
            log.warning("mcp sidecars could not be listed", agent=agent, error=type(err).__name__)
            labeled = ()
        for container_id in labeled:
            await self._discard(agent, container_id)
        if self.proxy is not None:
            # 프록시를 먼저 떼야 네트워크가 지워진다 — 붙은 컨테이너가 있으면 Docker 가 거절한다
            await self._quietly(agent, self.client.disconnect, network, self.proxy.container)
        await self._quietly(agent, self.client.remove_network, network)

    async def _quietly(self, agent: str, call: Callable[..., None], *args: str) -> None:
        """네트워크 정리 한 단계 — 실패는 남은 정리를 막지 않고 로그로 드러낸다."""
        try:
            await asyncio.to_thread(call, *args)
        except Exception as err:  # noqa: BLE001
            log.warning("mcp sidecar network cleanup failed", agent=agent, network=args[0],
                        step=call.__name__, error=type(err).__name__)  # fmt: skip

    async def _network(self, agent: str, server: McpServerSpec, network: str) -> None:
        image = server.sidecar.image if server.sidecar else ""
        try:
            await asyncio.to_thread(self.client.ensure_network, network, internal=True)
            if self.proxy is not None:
                await asyncio.to_thread(
                    self.client.connect, network, self.proxy.container, aliases=(self.proxy.alias,)
                )
        except NetworkIsolationError as err:
            raise network_mismatch(agent, image, err) from err
        except Exception as err:
            raise start_failed(
                agent, image, network=network, reason=f"sidecar network: {type(err).__name__}"
            ) from err

    async def _ensure(
        self,
        agent: str,
        server: McpServerSpec,
        env: Mapping[str, str],
        network: str,
        *,
        reuse: bool,
    ) -> None:
        kwargs = sidecar_kwargs(agent, server, env, network)
        image = kwargs["image"]
        existing = await asyncio.to_thread(self.client.find, kwargs["name"])
        if existing is not None:
            if reuse and await self._sound(agent, existing, network, kwargs):
                return
            await self._discard(agent, existing)
        try:
            await asyncio.to_thread(self.client.ensure_image, image)
        except Exception as err:
            raise image_unavailable(
                agent, image, mcp_server=server.name, reason=type(err).__name__
            ) from err
        try:
            container_id = await asyncio.to_thread(self.client.create, **kwargs)
        except Exception as err:
            raise start_failed(
                agent, image, mcp_server=server.name, reason=type(err).__name__
            ) from err
        try:
            await asyncio.to_thread(self.client.start, container_id)
        except Exception as err:
            await self._discard(agent, container_id)
            raise start_failed(
                agent,
                image,
                mcp_server=server.name,
                container_id=container_id[:12],
                reason=type(err).__name__,
            ) from err
        log.info("mcp sidecar started", agent=agent, mcp_server=server.name,
                 container_id=container_id[:12], image=image)  # fmt: skip

    async def _sound(
        self, agent: str, container_id: str, network: str, expected: Mapping[str, Any]
    ) -> bool:
        """재부착해도 되는 사이드카인가 — 떠 있고, 그 네트워크에만 붙어 있고, 경화가 그대로다."""
        try:
            state = await asyncio.to_thread(self.client.inspect, container_id)
            attached = await asyncio.to_thread(self.client.networks_of, container_id)
            found = await asyncio.to_thread(
                inspect_hardening, self.client, container_id, agent=agent
            )
        except Exception:  # noqa: BLE001 — 확인하지 못하면 새로 세운다
            return False
        return (
            bool(state.get("Running"))
            and set(attached) == {network}
            and not hardening_gaps(found, expected)
        )

    async def _discard(self, agent: str, container_id: str) -> None:
        try:
            await asyncio.to_thread(self.client.remove, container_id)
        except Exception as err:  # noqa: BLE001 — 남은 정리를 막지 않는다; 로그로 드러낸다
            log.warning("mcp sidecar removal failed", agent=agent,
                        container_id=container_id[:12], error=type(err).__name__)  # fmt: skip
            return
        log.info("mcp sidecar removed", agent=agent, container_id=container_id[:12])


__all__ = [
    "ROLE_LABEL",
    "SIDECAR_LABEL",
    "SIDECAR_ROLE",
    "McpSidecars",
    "ProxyAttachment",
    "sidecar_env",
    "sidecar_kwargs",
    "sidecar_network",
    "sidecars_of",
]
