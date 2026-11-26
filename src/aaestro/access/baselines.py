"""What declarations allow — the registry's second decision step.

선언이 기본 권한이다 (01 Access Control 1). 종류마다 선언이 사는 곳이 다르므로 종류별로 둔다.
선언 판정은 그것을 쓰는 강제 지점과 함께 연결한다 — 소비자 없는 판정은 강제와 따로 논다.

선언은 **요청마다 파일에서** 읽는다: 그룹 이동이나 mode 강등이 재시작 없이 다음 판정에 반영되어야
한다. 강제 지점의 캐시는 레지스트리가 선언 변경을 감지해 올리는 버전으로 무효화된다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

from aaestro.access.model import Mode
from aaestro.core.agent import DIRECT_RUN_PREFIX
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.core.manifest import (
    MEMORY_ALIAS_CHARS,
    RESERVED_GLOBAL_GROUP,
    MemoryMode,
    agent_name,
)

if TYPE_CHECKING:
    from aaestro.access.store import AccessStore, Identity
    from aaestro.catalog import Catalog
    from aaestro.core.manifest import AgentManifest, GroupManifest, McpServerSpec

SPACE_ID = re.compile(
    rf"^(?P<scope>local|group|global):(?P<owner>[^:\s*]+):(?P<alias>{MEMORY_ALIAS_CHARS})$"
)
"""Memory Service 의 영구 space id — ``MemorySpace.space_id`` 와 같은 모양이다.

run scope 는 여기 없다: run space 는 run 이 경계이고 선언이 아니라 run 기록으로 판정한다
(``RUN_SPACE_ID``).
"""

RUN_SPACE_ID = re.compile(rf"^run:(?P<run>.+):(?P<alias>{MEMORY_ALIAS_CHARS})$")
"""run space id — ``run:<run_id>:<alias>``. 별칭은 마지막 조각이다: 재개한 service run 의 id 는
``:`` 를 품는다 (``x:resumed``)."""


class RunParticipation(Protocol):
    """What a run record says about who may reach its run spaces (#608)."""

    @property
    def graph(self) -> str: ...

    @property
    def live(self) -> bool: ...

    @property
    def agents(self) -> tuple[str, ...]: ...

    @property
    def memory_spaces(self) -> Mapping[str, str]: ...


class RunRecords(Protocol):
    """Where run records are read — ``orchestrator.runstore`` 의 저장소가 이 모양이다.

    access 는 orchestrator 에 의존하지 않는다 — 모양만 받는다.
    """

    def get(self, run_id: str) -> RunParticipation | None: ...


@dataclass(frozen=True)
class MemoryBaseline:
    """Declared memory permissions — manifest (local), group.yaml (group), global.yaml (global).

    run scope space (``run:<run_id>:<alias>``) 는 선언 파일이 아니라 run 기록으로 판정한다 (#608):
    그 run 의 시작 시점 그래프에서 이 에이전트가 노드이고 그 별칭을 선언했으면, 진행 중에는 rw,
    끝난 뒤에는 ro (09 Scope Rule 1). 모르는 run · ``direct-`` run · 다른 그래프에 배포된 신원은
    거부한다.

    Attributes:
        catalog: 선언을 읽는 곳 — 매 판정마다 읽는다.
        runs: run 기록 — 없으면 run space 는 아무에게도 열리지 않는다.
    """

    catalog: Catalog
    runs: RunRecords | None = None

    def allows(
        self, agent: str, target: str, mode: Mode | None, identity: Identity | None = None
    ) -> bool:
        run_space = RUN_SPACE_ID.fullmatch(target)
        if run_space is not None:
            return self._run_allows(agent, run_space["run"], run_space["alias"], mode, identity)
        matched = SPACE_ID.fullmatch(target)
        if matched is None:
            return False
        try:
            manifest = self.catalog.agent(agent)
        except AaestroError as err:
            if err.code == ErrorCode.NF_001:
                return False
            raise
        scope, owner, alias = matched["scope"], matched["owner"], matched["alias"]
        writing = mode is Mode.RW

        if scope == "local":
            # 자기 이름의 space 만 — 남의 local space 는 선언으로 열리지 않는다
            return owner == agent and any(
                s.alias == alias and (not writing or s.mode is MemoryMode.RW)
                for s in manifest.spec.memory.spaces
            )

        if scope == "group":
            if manifest.metadata.group != owner:
                return False  # 소속이 곧 경계다 — 비멤버는 읽기도 못 한다
            group = self._group(owner)
            return group is not None and any(
                s.alias == alias and (not writing or s.mode is MemoryMode.RW)
                for s in group.spec.memory.spaces
            )

        if owner != RESERVED_GLOBAL_GROUP:
            return False
        declared = self._group(RESERVED_GLOBAL_GROUP)
        return declared is not None and any(
            s.alias == alias and (not writing or agent in s.writers)
            for s in declared.spec.memory.spaces
        )

    def _run_allows(
        self, agent: str, run_id: str, alias: str, mode: Mode | None, identity: Identity | None
    ) -> bool:
        record = self.participation(agent, run_id, identity)
        if record is None or alias not in record.memory_spaces:
            return False
        return mode is not Mode.RW or record.live

    def participation(
        self, agent: str, run_id: str, identity: Identity | None = None
    ) -> RunParticipation | None:
        """이 에이전트가 노드로 참여한 run 의 기록 — 참여자가 아니면 None (#609).

        모르는 run · ``direct-`` run · 노드가 아닌 에이전트 · 다른 그래프로 배포된 신원은 참여자가
        아니다. run space 판정과 Memory Service 의 run 조회가 이 한 규칙을 쓴다.
        """
        if self.runs is None or run_id.startswith(DIRECT_RUN_PREFIX):
            return None
        record = self.runs.get(run_id)
        if record is None or agent not in record.agents:
            return None
        if identity is not None and identity.graph != record.graph:
            # 같은 에이전트가 다른 그래프로도 배포돼 있으면 그 배포의 신원은 이 run 의 참여자가
            # 아니다
            return None
        return record

    def declared_for(self, agent: str) -> list[tuple[str, Mode | None]]:
        """이 에이전트에게 선언이 주는 space 와 mode — 운영자 화면이 보여 주는 기본 권한.

        run space 는 싣지 않는다 — 선언이 아니라 run 마다 생기고 사라지는 참여다.
        """
        manifest = _manifest(self.catalog, agent)
        if manifest is None:
            return []
        rw, ro = Mode.RW, Mode.RO
        found: list[tuple[str, Mode | None]] = [
            (f"local:{agent}:{s.alias}", rw if s.mode is MemoryMode.RW else ro)
            for s in manifest.spec.memory.spaces
        ]
        group_name = manifest.metadata.group
        group = self._group(group_name) if group_name else None
        if group is not None:
            found += [
                (f"group:{group_name}:{s.alias}", rw if s.mode is MemoryMode.RW else ro)
                for s in group.spec.memory.spaces
            ]
        declared = self._group(RESERVED_GLOBAL_GROUP)
        if declared is not None:
            found += [
                (f"global:{RESERVED_GLOBAL_GROUP}:{s.alias}", rw if agent in s.writers else ro)
                for s in declared.spec.memory.spaces
            ]
        return found

    def _group(self, name: str) -> GroupManifest | None:
        try:
            return self.catalog.group(name)
        except AaestroError as err:
            if err.code == ErrorCode.NF_001:
                return None
            raise


@dataclass(frozen=True)
class A2ABaseline:
    """Declared A2A connections — the graph the caller is deployed in.

    그래프의 ``connections`` 가 기본 권한이다 (03 Enforcement). 호출자가 **지금 배포된** 그래프만
    본다 — 같은 두 에이전트를 잇는 그래프가 저장소에 있어도 배포되지 않았으면 연결이 아니다.

    권한 에이전트(``stewards``)는 예외다: 운영자가 설정으로 지정한 권한 에이전트에게는 모든
    에이전트가 확장을 **요청**할 수 있어야 한다 (01 Access Control 3). 요청일 뿐 — 무엇을 줄지는
    권한 에이전트의 규칙과 확장 상한이 정한다. 권한 에이전트가 자기 자신을 부르는 것은 아니다.

    Attributes:
        catalog: 그래프 선언.
        store: 호출자의 살아 있는 신원이 어느 그래프에 배포됐는지.
        stewards: 운영자가 지정한 권한 에이전트 이름.
    """

    catalog: Catalog
    store: AccessStore
    stewards: frozenset[str] = frozenset()

    def allows(
        self, agent: str, target: str, mode: Mode | None, identity: Identity | None = None
    ) -> bool:
        if target in self.stewards and target != agent:
            return True
        if identity is not None:
            # 요청한 신원의 배포 그래프만 본다 — 같은 이름으로 다른 그래프에 배포된 신원이 있어도
            # 그 그래프의 연결을 빌려 쓰지 못한다
            graphs = {identity.graph} if identity.graph else set()
        else:
            graphs = set(self.store.live_graphs(agent))
        return any(self._connected(graph, agent, target) for graph in sorted(graphs))

    def declared_for(self, agent: str) -> list[tuple[str, Mode | None]]:
        """지금 배포된 그래프의 연결과 권한 에이전트 — 이 에이전트가 부를 수 있는 피호출자."""
        callees = {s for s in self.stewards if s != agent}
        for graph in self.store.live_graphs(agent):
            try:
                topology = self.catalog.graph(graph)
            except AaestroError as err:
                if err.code == ErrorCode.NF_001:
                    continue
                raise
            agent_of = {n.id: agent_name(n.agent) for n in topology.spec.nodes if n.agent}
            callees |= {
                agent_of[c.callee]
                for c in topology.spec.connections
                if agent_of.get(c.caller) == agent and c.callee in agent_of
            }
        return [(callee, None) for callee in sorted(callees)]

    def _connected(self, graph: str, caller: str, callee: str) -> bool:
        try:
            topology = self.catalog.graph(graph)
        except AaestroError as err:
            if err.code == ErrorCode.NF_001:
                return False
            raise
        # connections 는 **노드 id** 를 잇는다 — 노드의 에이전트로 풀어서 비교한다
        agent_of = {n.id: agent_name(n.agent) for n in topology.spec.nodes if n.agent}
        return any(
            agent_of.get(c.caller) == caller and agent_of.get(c.callee) == callee
            for c in topology.spec.connections
        )


def _manifest(catalog: Catalog, agent: str) -> AgentManifest | None:
    try:
        return catalog.agent(agent)
    except AaestroError as err:
        if err.code == ErrorCode.NF_001:
            return None
        raise


ALL_TOOLS = "*"
"""화면에 보이는 "서버의 모든 도구" — 판정 대상 이름이 아니다 (도구 판정은 이름 하나씩)."""

HTTPS_PORT = 443
HTTP_PORT = 80

PROVIDER_HOSTS = {
    "anthropic": "api.anthropic.com",
    "openai-compat": "openai-compat.invalid",
}
"""모델 provider 의 논리 호스트 — 이그레스 프록시가 종단하는 호출은 이 이름으로 판정한다.

실제 upstream(대역 provider 등)이 달라도 판정 대상은 이 이름이다: 운영자가 회수하는 것은 "모델 API"
이지 테스트 배치의 주소가 아니다.

``openai-compat`` 은 upstream 이 설정에서 와서 자연스러운 호스트가 없다 — 합성 이름을 쓴다.
``.invalid`` 는 예약 TLD(RFC 2606)라 풀리지 않는다: 이 이름이 선언 목적지에 들어가도 같은 이름의
CONNECT 터널이 열리지 않는다. 판정 문자열일 뿐 어디서도 연결 대상으로 쓰지 않는다."""


def egress_target(host: str, port: int = HTTPS_PORT) -> str:
    """판정 대상 이름 — 443 은 생략한다. 선언과 CONNECT 가 같은 이름을 쓰게 한 곳에서 정한다."""
    host = host.lower().rstrip(".")
    return host if port == HTTPS_PORT else f"{host}:{port}"


def _declared_target(value: str) -> str:
    host, _, port = value.rpartition(":") if ":" in value else (value, "", "")
    return egress_target(host, int(port)) if port else egress_target(value)


@dataclass(frozen=True)
class EgressBaseline:
    """Declared egress destinations.

    - 매니페스트 ``runtime.egress``
    - 모델 provider 의 논리 호스트 (프록시가 종단해 키를 주입하는 호출) — fallback 포함
    - external MCP 서버 ``url`` 의 호스트

    선언을 요청마다 읽는다 — 목적지 추가·삭제가 재시작 없이 다음 판정에 반영된다.
    """

    catalog: Catalog

    def allows(
        self, agent: str, target: str, mode: Mode | None, identity: Identity | None = None
    ) -> bool:
        try:
            manifest = self.catalog.agent(agent)
        except AaestroError as err:
            if err.code == ErrorCode.NF_001:
                return False
            raise
        return target in self.declared(manifest)

    def declared_for(self, agent: str) -> list[tuple[str, Mode | None]]:
        manifest = _manifest(self.catalog, agent)
        return [] if manifest is None else [(t, None) for t in sorted(self.declared(manifest))]

    @staticmethod
    def declared(manifest: AgentManifest) -> frozenset[str]:
        targets = {_declared_target(entry) for entry in manifest.spec.runtime.egress}
        # fallback provider 도 선언이다 — 빠지면 전환한 호출을 프록시가 거부한다
        for provider in manifest.spec.model.providers():
            host = PROVIDER_HOSTS.get(provider)
            if host is not None:
                targets.add(host)
        for server in manifest.spec.mcp.servers:
            target = url_target(server.url) if server.url is not None else None
            if target is not None:
                targets.add(target)
        return frozenset(targets)


def url_target(url: str) -> str | None:
    """An http(s) URL's egress target — the name a remote MCP server is decided under.

    선언 판정과 프록시의 원격 MCP 종단이 같은 이름을 쓰게 한 곳에서 정한다. 풀 수 없는 URL 은
    None — 선언 검증이 막지만, 한 에이전트의 잘못된 선언으로 판정이 터지면 안 된다.
    """
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname:
        return None
    default = HTTPS_PORT if parts.scheme == "https" else HTTP_PORT
    return egress_target(parts.hostname, port or default)


@dataclass(frozen=True)
class McpToolBaseline:
    """Declared MCP tools the proxy terminates — ``server/tool`` (#282, #304).

    프록시가 종단하는 서버(external, sidecar)만 대상이다 — stdio 서버의 도구는 컨테이너 안에서 돌아
    프록시가 보지 못한다. ``allowed_tools`` 를 선언했으면 그 목록만, 아니면 그 서버의 모든 도구가
    기본 권한이다.
    """

    catalog: Catalog

    def allows(
        self, agent: str, target: str, mode: Mode | None, identity: Identity | None = None
    ) -> bool:
        server, _, tool = target.partition("/")
        if not (server and tool):
            return False
        try:
            manifest = self.catalog.agent(agent)
        except AaestroError as err:
            if err.code == ErrorCode.NF_001:
                return False
            raise
        for declared in manifest.spec.mcp.servers:
            if declared.name == server and _terminated(declared):
                return not declared.allowed_tools or tool in declared.allowed_tools
        return False

    def declared_for(self, agent: str) -> list[tuple[str, Mode | None]]:
        """프록시가 종단하는 서버의 선언 도구 — ``allowed_tools`` 가 없으면 ``server/*``."""
        manifest = _manifest(self.catalog, agent)
        if manifest is None:
            return []
        found: list[tuple[str, Mode | None]] = []
        for server in manifest.spec.mcp.servers:
            if not _terminated(server):
                continue
            tools = server.allowed_tools or (ALL_TOOLS,)
            found += [(f"{server.name}/{tool}", None) for tool in tools]
        return found


def _terminated(server: McpServerSpec) -> bool:
    """프록시가 종단하는 서버인가 — external 이거나 사이드카. stdio 는 컨테이너 안이다."""
    return server.url is not None or server.sidecar is not None


__all__ = [
    "DIRECT_RUN_PREFIX",
    "PROVIDER_HOSTS",
    "RUN_SPACE_ID",
    "SPACE_ID",
    "A2ABaseline",
    "EgressBaseline",
    "McpToolBaseline",
    "MemoryBaseline",
    "RunParticipation",
    "RunRecords",
    "egress_target",
    "url_target",
]
