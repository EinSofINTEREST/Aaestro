"""Writing graphs and agent manifests — validated before anything touches disk.

UI 가 조립한 결과를 **저장할 통로**와, 저장 전에 **검증할 통로** (#242).
`DeployValidator` 는 있었지만 CLI 전용이었다 — 여기서는 저장된 선언과 초안을 합쳐
같은 배포 검증 전체(`DeployValidator`)를 돌리고, 통과한 것만 쓴다 (01 Contract Validation).

쓰지 않는 것: 모듈(skillset / promptset / memoryset). v0.1 에서 게시된 모듈은
불변이다 (04 Registry 2) — 편집 가능한 것은 그래프와 에이전트 매니페스트뿐이다.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
import yaml
from pydantic import BaseModel

from aaestro.catalog import MODULE_TYPES, Catalog, Problem, not_found
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import AgentManifest, agent_name
from aaestro.core.topology import GraphTopology
from aaestro.deploy import Finding, ValidationReport, validate_deployment
from aaestro.materials import Materials, MaterialStore, check_files
from aaestro.modules.promptset import PromptsetManifest

log = structlog.get_logger(__name__)

InUse = Callable[[str, str], bool]
"""``(kind, name)`` 이 지금 배포 중인가 — 배포 lifecycle(#243) 이 채운다. 없으면 항상 False."""

DECLARATION_MODE = 0o644
"""새로 쓰는 선언 파일의 권한 — 다른 uid 로 도는 에이전트 컨테이너도 읽을 수 있어야 한다."""


def _problem_details(problem: Problem) -> dict[str, Any]:
    """카탈로그 문제를 finding details 로 — 경로 · 읽기 실패 사유 · ``{field, problem}`` 목록."""
    details: dict[str, Any] = {"path": problem.path}
    if problem.reason:
        details["reason"] = problem.reason
    if problem.errors:
        details["errors"] = list(problem.errors)
    return details


def _version_tuple(version: str) -> tuple[int, ...]:
    # SemVer 검증은 모델이 이미 했다 — 여기서는 비교만
    return tuple(int(part) for part in version.split("."))


def _serialize(model: BaseModel) -> str:
    """사람이 읽는 YAML — 키 순서 ``apiVersion → kind → metadata → spec`` (07 Manifest YAML)."""
    document = model.model_dump(mode="json", by_alias=True, exclude_none=True)
    return yaml.safe_dump(document, sort_keys=False, allow_unicode=True)


def _round_trips[T: BaseModel](model: T, kind: type[T]) -> None:
    """쓰기 **전에** 직렬화 → 파싱이 같은 모델을 내는지 본다 — 안 되면 파일이 생기지 않는다."""
    again = kind.model_validate(yaml.safe_load(_serialize(model)))
    if again != model or _override_keys(again) != _override_keys(model):
        raise AaestroError(
            category=ErrorCategory.INTERNAL,
            code=ErrorCode.INTERNAL_001,
            message="declaration does not survive a serialization round-trip",
            details={"kind": kind.__name__},
        )


def _override_keys(model: BaseModel) -> list[frozenset[str] | None]:
    """노드 model override 가 설정한 키 — 같은 값이라도 키의 유무가 다르면 다른 선언이다."""
    if not isinstance(model, GraphTopology):
        return []
    return [
        frozenset(node.model.model_fields_set) if node.model is not None else None
        for node in model.spec.nodes
    ]


def _rejected(message: str, **details: Any) -> AaestroError:
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=message,
        details=details,
    )


def _version_conflict(kind: str, name: str, *, existing: str, proposed: str) -> AaestroError:
    """내용이 바뀌면 버전이 올라야 한다 (02 Manifest Rules 2 / 04 Graph Rules)."""
    return AaestroError(
        category=ErrorCategory.MODULE,
        code=ErrorCode.MOD_002,
        message=f"{kind} changed without a version bump",
        details={"kind": kind, "name": name, "existing": existing, "proposed": proposed},
    )


@dataclass(frozen=True)
class Author:
    """Validates and writes graphs and agent manifests.

    그래프와 매니페스트를 검증하고 씁니다. 읽기는 `Catalog` 가, 쓰기는 여기가 —
    한 파일이 두 곳에서 쓰이면 한쪽이 다른 쪽의 규칙을 모른다.

    Attributes:
        catalog: What is already declared — 검증의 바탕이자 쓰기의 대상.
        a2a_port_range: The runtime's allocatable range, for the port check.
        in_use: Whether a declaration is currently deployed (#243). 배포 중인
            것을 지우거나 덮어쓰면 실행 중 run 의 계약이 발밑에서 바뀐다.
        materials: Where build materials live (#264). 없으면 재료 표면을 열지 않는다 —
            선언만 다루는 조립에서는 커스텀 이미지를 굽지 않기 때문이다.
        providers: Egress proxy upstream names (#358). None 이면 프록시가 없다 —
            모델 provider 는 알려진 논리 호스트인지만 본다.
        state_artifacts: Whether a state-compaction artifact store is configured (#371).
            없으면 `summarize` 압축을 선언한 그래프가 검증에서 거절된다.
    """

    catalog: Catalog
    a2a_port_range: tuple[int, int] | None = None
    in_use: InUse | None = None
    materials: MaterialStore | None = None
    providers: frozenset[str] | None = None
    state_artifacts: bool = False

    # --- 검증 ----------------------------------------------------------------

    def validate(
        self,
        *,
        graphs: Sequence[GraphTopology] = (),
        agents: Sequence[AgentManifest] = (),
        replacing: Path | Sequence[Path] | None = None,
        with_saved_graphs: bool = True,
    ) -> ValidationReport:
        """Validate drafts together with everything already saved.

        초안을 **저장된 것과 함께** 검증합니다 — 초안 그래프가 기존 에이전트를
        참조하는 것이 일반적이고, 초안 에이전트는 기존 그래프를 깨뜨릴 수 있습니다.
        같은 이름의 초안은 저장된 것을 덮습니다.

        ``with_saved_graphs=False`` 면 **주어진 그래프만** 검증한다 — CLI 의 `deploy` /
        `validate` 는 지목한 그래프만 판정해 왔다. 다만 에이전트/그룹/모듈 검사는
        저장소 전체를 본다 (그 검사들은 애초에 그래프 단위가 아니다).

        읽지 못한 선언은 검증에서 빠진 것이지 통과한 것이 아니다 — finding 으로 합친다.
        단 ``replacing`` 경로의 문제는 뺀다: 그 파일은 이번 저장이 덮어쓰는 것이라,
        깨진 파일을 고치는 저장이 그 파일 때문에 거절되면 고칠 길이 없다.
        """
        saved_agents, saved_groups = self.catalog.agents(), self.catalog.groups()
        manifests = {**saved_agents.items, **{m.name: m for m in agents}}
        promptsets, unloadable = self._promptsets(manifests.values())
        saved_graphs = self.catalog.graphs()
        drafted = {g.metadata.name for g in graphs}
        others = saved_graphs.items.items() if with_saved_graphs else ()
        topologies = [*graphs, *(g for n, g in others if n not in drafted)]

        report = validate_deployment(
            topologies,
            manifests=manifests,
            groups=saved_groups.items,
            resolvable_refs=self.catalog.module_refs(),
            promptsets=promptsets,
            global_secrets=(
                frozenset(saved_groups.items["global"].spec.secrets)
                if "global" in saved_groups.items
                else ()
            ),
            a2a_port_range=self.a2a_port_range,
            providers=self.providers,
            state_artifacts=self.state_artifacts,
        )
        # 저장된 그래프의 문제는 저장된 그래프를 검증할 때만 — 그래프 하나를 지목한
        # CLI 검증이 무관한 깨진 그래프 때문에 실패하면 docstring 과 어긋난다.
        # 에이전트/그룹/모듈은 그 검사들이 애초에 저장소 전체를 보므로 늘 포함한다
        problems = [
            *saved_agents.problems,
            *saved_groups.problems,
            *(saved_graphs.problems if with_saved_graphs else ()),
            *(p for t in MODULE_TYPES for p in self.catalog.modules(t).problems),
        ]
        targets = (
            set()
            if replacing is None
            else {str(p) for p in ([replacing] if isinstance(replacing, Path) else replacing)}
        )
        broken = [
            Finding(
                check="catalog",
                code=ErrorCode(p.code),
                message=p.message,
                details=_problem_details(p),
            )
            for p in problems
            if p.path not in targets
        ]
        return ValidationReport(
            findings=(*broken, *unloadable, *report.findings), warnings=report.warnings
        )

    # --- 그래프 ---------------------------------------------------------------

    def _promptsets(
        self, manifests: Iterable[AgentManifest]
    ) -> tuple[dict[str, PromptsetManifest], list[Finding]]:
        """검증 대상 에이전트들이 선언한 promptset 을 ref 로 모은다 (#260).

        **읽지 못한 것은 finding 으로 남긴다.** 조용히 건너뛰면 노드↔템플릿 검사가
        그 에이전트에 대해 아무 것도 보지 못한 채로 통과한다 — 검사가 있다는 사실만
        남고 실제로는 꺼져 있는 상태가 된다.

        해석 자체가 안 되는 ref(`MOD_001`)만 예외다 — 모듈 ref 검사가 이미 보고한다.
        """
        found: dict[str, PromptsetManifest] = {}
        problems: list[Finding] = []
        for manifest in manifests:
            ref = manifest.spec.promptset.ref
            if ref in found:
                continue
            try:
                found[ref] = self.catalog.promptset(ref)
            except AaestroError as err:
                if err.code == ErrorCode.MOD_001:
                    continue
                problems.append(
                    Finding(
                        check="node_templates",
                        code=ErrorCode(err.code),
                        message=f"promptset could not be loaded: {err.message}",
                        details={"agent": manifest.name, "module_ref": ref},
                    )
                )
        return found, problems

    def save_graph(self, name: str, topology: GraphTopology) -> Path:
        """Validate and write one graph.

        검증을 통과한 그래프만 씁니다. 경로의 이름과 선언의 이름이 같아야 합니다 —
        카탈로그에서 **위치가 정체성**이기 때문입니다.
        """
        if topology.metadata.name != name:
            raise _rejected(
                "graph name in the path does not match the declaration",
                path_name=name,
                declared=topology.metadata.name,
            )
        existing = self._existing(name, self.catalog.graph)
        if existing == topology:
            return self._graph_path(name)  # 멱등 — 같은 것을 다시 쓰지 않는다
        if existing is not None:
            self._require_bump("graph", name, existing.metadata.version, topology.metadata.version)
        self._refuse_if_in_use("graph", name)
        self._require_ok(self.validate(graphs=[topology], replacing=self._graph_path(name)))
        _round_trips(topology, GraphTopology)
        return self._write(self._graph_path(name), topology)

    def save_all(
        self,
        *,
        graphs: Mapping[str, GraphTopology] | None = None,
        agents: Mapping[str, AgentManifest] | None = None,
    ) -> list[Path]:
        """Validate a set of declarations together and commit them together.

        여러 선언을 **함께** 검증하고 **함께** 씁니다. 에이전트를 0.2.0 으로 올리면서
        그것을 참조하는 그래프도 함께 올려야 할 때, 둘을 따로 저장하면 어느 순서로도
        통과하지 못한다 — 그래프가 먼저면 없는 버전을 가리키고, 에이전트가 먼저면
        저장된 그래프의 ref 가 깨진다. 함께 검증하면 둘 다 통과한다.

        커밋은 전부 아니면 전무다: 쓰기 전 원본을 스냅샷하고, 하나라도 실패하면
        되돌린다. 원자적 파일 교체는 개별 `_write` 가, 묶음의 원자성은 스냅샷이 맡는다.
        """
        graphs = graphs or {}
        agents = agents or {}
        for name, topology in graphs.items():
            if topology.metadata.name != name:
                raise _rejected(
                    "graph name in the path does not match the declaration",
                    path_name=name,
                    declared=topology.metadata.name,
                )
        for name, manifest in agents.items():
            if manifest.name != name:
                raise _rejected(
                    "agent name in the path does not match the declaration",
                    path_name=name,
                    declared=manifest.name,
                )

        targets: dict[Path, BaseModel] = {}
        for name, topology in graphs.items():
            existing = self._existing(name, self.catalog.graph)
            if existing == topology:
                continue
            if existing is not None:
                self._require_bump(
                    "graph", name, existing.metadata.version, topology.metadata.version
                )
            self._refuse_if_in_use("graph", name)
            _round_trips(topology, GraphTopology)
            targets[self._graph_path(name)] = topology
        for name, manifest in agents.items():
            existing_agent = self._existing(name, self.catalog.agent)
            if existing_agent == manifest:
                continue
            if existing_agent is not None:
                self._require_bump(
                    "agent", name, existing_agent.metadata.version, manifest.metadata.version
                )
            self._refuse_if_in_use("agent", name)
            _round_trips(manifest, AgentManifest)
            targets[self._agent_path(name)] = manifest
        if not targets:
            return []

        self._require_ok(
            self.validate(
                graphs=list(graphs.values()), agents=list(agents.values()), replacing=list(targets)
            )
        )

        snapshots = {path: (path.read_bytes() if path.is_file() else None) for path in targets}
        written: list[Path] = []
        try:
            for path, model in targets.items():
                written.append(self._write(path, model))
        except BaseException:
            for path, before in snapshots.items():
                if before is None:
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                else:
                    path.write_bytes(before)
            raise
        return written

    def delete_graph(self, name: str) -> None:
        path = self._graph_path(name)
        if not path.is_file():
            raise not_found("graph", name)
        self._refuse_if_in_use("graph", name)
        path.unlink()

    # --- 에이전트 -----------------------------------------------------------

    def save_agent(self, name: str, manifest: AgentManifest) -> Path:
        """Validate and write one agent manifest.

        에이전트 자체의 계약(모듈 ref / 그룹 / env_allowlist / quota)과, 이 에이전트를
        참조하는 **모든 저장된 그래프**가 여전히 통과하는지를 함께 봅니다.
        """
        if manifest.name != name:
            raise _rejected(
                "agent name in the path does not match the declaration",
                path_name=name,
                declared=manifest.name,
            )
        existing = self._existing(name, self.catalog.agent)
        if existing == manifest:
            return self._agent_path(name)
        if existing is not None:
            self._require_bump("agent", name, existing.metadata.version, manifest.metadata.version)
        self._refuse_if_in_use("agent", name)
        self._require_ok(self.validate(agents=[manifest], replacing=self._agent_path(name)))
        _round_trips(manifest, AgentManifest)
        return self._write(self._agent_path(name), manifest)

    def delete_agent(self, name: str) -> list[str]:
        path = self._agent_path(name)
        if not path.is_file():
            raise not_found("agent", name)
        referenced_by = sorted(
            graph_name
            for graph_name, graph in self.catalog.graphs().items.items()
            if any(
                node.agent is not None and agent_name(node.agent) == name
                for node in graph.spec.nodes
            )
        )
        if referenced_by:
            # 참조를 남긴 채 지우면 그 그래프는 다음 검증에서야 깨진다 — 지금 막는다
            raise _rejected(
                "agent is referenced by graphs and cannot be deleted",
                agent=name,
                referenced_by=referenced_by,
            )
        self._refuse_if_in_use("agent", name)
        path.unlink()
        return self._clear_agent_directory(path.parent, name)

    def _clear_agent_directory(self, directory: Path, agent: str) -> list[str]:
        """선언을 지운 뒤 남은 것 — 빈 디렉토리는 지우고, 사람이 둔 것은 남겨 알린다 (#258).

        빌드 재료는 재료 스토어에 산다 (#264) — 프레임워크가 이 디렉토리에 쓰는 것은 매니페스트
        하나뿐이다. 그래서 비었으면 지우는 것이 "삭제됐다" 는 응답과 맞고, 남은 것은 프레임워크가
        만들지 않았으므로 지우지 않는다. 조용히 두지도 않는다: 같은 이름으로 다시 만들면 되살아난다.

        지우는 것은 **진짜 빈 디렉토리**뿐이다 — 심볼릭 링크는 따라가지도, 지우지도 않고 남은 것으로
        보고한다 (디렉토리를 가리키는 링크에 ``rmdir`` 을 걸면 매니페스트를 지운 뒤에 터진다).
        """
        retained: list[str] = []
        for parent, directories, files in os.walk(directory, topdown=False):
            here = Path(parent)
            retained += [str((here / name).relative_to(directory)) for name in files]
            for name in directories:
                child = here / name
                if child.is_symlink():
                    retained.append(str(child.relative_to(directory)))
                elif not any(child.iterdir()):
                    child.rmdir()  # 아래에서부터 — 비게 된 하위 디렉토리도 남기지 않는다
        if retained:
            log.warning("agent directory kept — it holds files aaestro did not write",
                        agent=agent, retained=sorted(retained))  # fmt: skip
            return sorted(retained)
        directory.rmdir()
        return []

    # --- 내부 ---------------------------------------------------------------

    def _graph_path(self, name: str) -> Path:
        # 경로 규칙은 카탈로그 한 곳에 있다 — 읽기와 쓰기·삭제가 같은 검사를 지난다 (#273)
        return self.catalog.graph_path(name)

    def _agent_path(self, name: str) -> Path:
        return self.catalog.agent_path(name)

    def _existing[T: BaseModel](self, name: str, read: Callable[[str], T]) -> T | None:
        """저장된 같은 이름 — 없으면 None.

        읽을 수는 있는데 깨져 있으면 **덮어쓰기를 허용**한다: 버전을 비교할 대상이
        없고, 깨진 파일을 고치는 길이 API 에 있어야 한다. 깨진 사실은 카탈로그가
        이미 `problems` 로 보고하고 있다.
        """
        try:
            return read(name)
        except AaestroError:
            return None

    @staticmethod
    def _require_bump(kind: str, name: str, existing: str, proposed: str) -> None:
        if _version_tuple(proposed) <= _version_tuple(existing):
            raise _version_conflict(kind, name, existing=existing, proposed=proposed)

    # --- 빌드 재료 (#264) ------------------------------------------------------

    def _store(self) -> MaterialStore:
        if self.materials is None:
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_001,
                message="this control plane has no material store configured",
            )
        return self.materials

    def read_materials(self, name: str) -> Materials:
        """Read the materials declared for an agent's **current** version.

        저장된 매니페스트의 버전을 키로 읽는다 — 재료는 선언과 한 몸이라 버전이 다르면
        다른 재료다. 없으면 빈 집합을 돌려준다: "아직 넣지 않았다" 는 오류가 아니다.
        """
        manifest = self.catalog.agent(name)
        version = manifest.metadata.version
        found = self._store().get(name, version)
        return found or Materials(agent=name, version=version, files={})

    def save_materials(self, name: str, files: Mapping[str, str]) -> Materials:
        """Validate and store one agent version's build materials.

        선언과 같은 두 규칙을 따른다: **버전이 같으면 내용도 같아야** 하고 (04 Registry 2
        Immutability), 배포 중인 에이전트의 재료는 바꾸지 못한다. 재료가 바뀌면 그 버전으로
        구운 이미지와 어긋나므로, 무엇이 도는지 알 수 없게 된다.

        Raises:
            AaestroError: VALIDATION/``VAL_002`` 경로·크기 규칙 위반,
                MODULE/``MOD_002`` 같은 버전에 다른 내용,
                NOT_FOUND/``NF_001`` 선언되지 않은 에이전트.
        """
        manifest = self.catalog.agent(name)
        version = manifest.metadata.version
        checked = check_files(files)
        existing = self._store().get(name, version)
        if existing is not None:
            if dict(existing.files) == checked:
                # 바뀌는 것이 없는 저장은 배포 중이어도 막을 이유가 없다 — 같은 PUT 을
                # 다시 보내는 것이 거절되면 재시도가 실패로 보인다
                return existing
            raise _version_conflict("agent materials", name, existing=version, proposed=version)
        self._refuse_if_in_use("agent", name)
        self._store().put(Materials(agent=name, version=version, files=checked))
        stored = self._store().get(name, version)
        assert stored is not None  # noqa: S101 — 방금 적재했다
        # 적재 시점이 찍힌 **저장된** 기록을 돌려준다. 넣은 것을 그대로 돌려주면 같은 쓰기가
        # PUT 응답과 이후 GET 에서 다르게 보인다
        return stored

    def delete_materials(self, name: str) -> bool:
        """Clear the materials for an agent's current version — the declaration stays.

        **행을 지우지 않고 빈 집합으로 덮는다.** 지워 버리면 불변성 검사의 유일한 근거가
        사라져서, 삭제한 뒤 같은 버전에 다른 내용을 넣을 수 있다 — 그 버전으로 구운 이미지가
        무엇으로 만들어졌는지 알 수 없게 된다. 내용을 바꾸려면 여전히 버전을 올려야 한다.

        Returns:
            Whether anything was there to clear.
        """
        manifest = self.catalog.agent(name)
        version = manifest.metadata.version
        existing = self._store().get(name, version)
        if existing is None or not existing.files:
            return False
        self._refuse_if_in_use("agent", name)
        self._store().put(Materials(agent=name, version=version, files={}))
        return True

    def _refuse_if_in_use(self, kind: str, name: str) -> None:
        if self.in_use is not None and self.in_use(kind, name):
            raise _rejected(
                f"{kind} is currently deployed — tear the deployment down first",
                kind=kind,
                name=name,
            )

    @staticmethod
    def _require_ok(report: ValidationReport) -> None:
        if report.ok:
            return
        raise AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_001,
            message="declaration failed deployment validation",
            details={
                "findings": [
                    {"check": f.check, "code": str(f.code), "message": f.message, **f.details}
                    for f in report.findings
                ]
            },
        )

    @staticmethod
    def _write(path: Path, model: BaseModel) -> Path:
        """같은 디렉토리의 임시 파일에 쓰고 ``os.replace`` 로 바꿔 넣는다.

        ``write_text`` 는 먼저 비우고 쓴다 — 중간에 죽으면 잘린 파일이 남고 마지막
        정상 버전은 사라진다. 이 API 가 선언을 바꾸는 주 경로이므로 원자적이어야 한다.

        ``mkstemp`` 는 0600 으로 만든다 — 그대로 바꿔 넣으면 다른 uid 로 도는 에이전트
        컨테이너가 읽기 전용 마운트로 보던 선언을 더는 읽지 못한다. 교체되는 파일의 권한을
        잇고, 새 파일은 ``DECLARATION_MODE`` 로 둔다. 선언에는 secret 이 없다 (env 로 주입).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except FileNotFoundError:
            mode = DECLARATION_MODE
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        try:
            os.fchmod(fd, mode)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(_serialize(model))
            Path(tmp).replace(path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                Path(tmp).unlink()
            raise
        return path


__all__ = ["Author", "InUse"]
