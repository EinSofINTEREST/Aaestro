"""What the repository declares — agents, graphs, modules, groups.

저장소에 선언된 것을 읽는 **한 곳**. CLI `status` 와 Control Plane 카탈로그 API 가
같은 코드를 쓴다 (#240) — 조회가 CLI 에만 묶여 있으면 UI 는 "무엇으로 조립할 수
있는가" 를 알 길이 없고, CLI 만 고치고 API 를 빠뜨리는 일이 생긴다.

목록은 **읽을 수 있는 것을 돌려주고 깨진 것은 이름을 대며 따로 보고한다** —
선언 하나가 깨졌다고 카탈로그 전체가 500 이 되면 운영자는 어느 파일인지 모른다.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError

from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    field_problems,
    raise_detached,
)
from aaestro.core.manifest import NAME_PATTERN, AgentManifest, GroupManifest
from aaestro.core.yamlio import YamlReadError, load_yaml_file
from aaestro.modules.memoryset import MemorysetManifest
from aaestro.modules.promptset import PromptsetManifest
from aaestro.modules.registry import ModuleRegistry, RegistryRoots
from aaestro.modules.skillset import SkillsetManifest
from aaestro.orchestrator.topology import GraphTopology

MODULE_TYPES: tuple[str, ...] = ("skillsets", "promptsets", "memorysets")
"""API 로 읽을 수 있는 모듈 종류 — v0.1 은 전부 읽기 전용이다 (04 Registry 2)."""


@dataclass(frozen=True)
class Problem:
    """A declaration that could not be read.

    읽지 못한 선언 하나 — **어느 파일이 왜** 인지가 전부다.
    """

    path: str
    code: str
    message: str
    errors: tuple[dict[str, str], ...] = ()
    """``{field, problem}`` — 스키마 위반이면 **어느 키가 왜** 인지. 값은 싣지 않는다."""
    reason: str | None = None
    """읽기 실패의 위치와 종류 — 중복 키 (이름 · 줄), 파싱 실패 (줄 · 열). 입력 조각은 없다."""


def _problem(path: str, err: AaestroError) -> Problem:
    """에러를 Problem 으로 — 에러가 싣고 온 필드 목록을 잃지 않는다."""
    errors = err.details.get("errors")
    reason = err.details.get("reason")
    return Problem(
        path=path,
        code=err.code,
        message=err.message,
        errors=tuple(errors) if isinstance(errors, list) else (),
        reason=reason if isinstance(reason, str) else None,
    )


@dataclass(frozen=True)
class Listing[T: BaseModel]:
    """Parsed declarations plus the ones that failed to parse.

    파싱된 선언과 실패한 선언을 함께 담는다. 실패를 예외로 올리면 하나 때문에
    전체가 사라지고, 조용히 빼면 운영자가 빠진 줄 모른다.
    """

    items: dict[str, T] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()


def load_yaml(path: Path) -> dict[str, Any]:
    """Read a YAML mapping.

    YAML 문서를 읽습니다.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the file cannot be read or parsed.
    """
    from aaestro.config import config_error

    try:
        document = load_yaml_file(path)
    except YamlReadError as err:
        raise_detached(config_error("cannot read yaml document", path=str(path), reason=err.reason))

    if not isinstance(document, dict):
        raise config_error("yaml document must be a mapping", path=str(path))
    return document


def not_found(kind: str, name: str) -> AaestroError:
    """없는 대상 — 04 의 공용 매핑이 404 로 옮긴다."""
    return AaestroError(
        category=ErrorCategory.NOT_FOUND,
        code=ErrorCode.NF_001,
        message=f"unknown {kind}: {name}",
        details={"kind": kind, "name": name},
    )


def invalid_name(kind: str, name: str) -> AaestroError:
    """이름이 선언 이름 규칙을 어김 — 경로가 되기 전에 막는다 (#273)."""
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message=f"invalid {kind} name — lowercase letters, digits and single hyphens only",
        details={"kind": kind, "name": name},
    )


def contained(root: Path, kind: str, name: str, *parts: str) -> Path:
    """선언 파일의 경로 — 이름을 검증하고, 해석한 경로가 루트 안에 머무는지 확인한다.

    이름이 URL 에서 곧장 파일 경로가 된다. 검증 없이 이으면 ``..`` 가 루트 밖을 가리켜,
    읽기는 루트 밖 파일을 파싱해 오류 세부로 흘리고 삭제는 루트 밖 파일을 지웠다 (#273).
    규칙 검사가 1차 방어이고, 해석한 경로의 포함 여부는 규칙이 느슨해져도 남는 2차 방어다.

    Raises:
        AaestroError: VALIDATION/``VAL_002`` if the name breaks the rule or escapes the root.
    """
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise invalid_name(kind, str(name))
    path = root.joinpath(*parts) if parts else root / name
    resolved = inside(root, path)
    if resolved is None:
        raise invalid_name(kind, name)
    # 검사한 **정규 경로**를 돌려준다 — 원래 경로를 돌려주면 검사 뒤에 링크가 바뀌었을 때
    # 쓰기·삭제·마운트가 새 대상을 따라간다
    return resolved


def inside(root: Path, path: Path) -> Path | None:
    """``path`` 의 정규 경로가 ``root`` 안이면 그 경로, 아니면 None.

    링크 순환이나 읽을 수 없는 구간은 해석 자체가 실패한다 — 그것도 밖으로 본다.
    """
    try:
        resolved = path.resolve()
        base = root.resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(base) else None


def _invalid(path: Path, err: ValidationError) -> NoReturn:
    """스키마 위반을 파일 이름과 함께 — 어느 필드가 왜 인지까지."""
    problems = field_problems(err)
    # 이 프레임은 던지는 에러의 traceback 에 남는다 — 입력 값을 실은 원인을 지역 변수로 두지 않는다
    del err
    raise_detached(
        AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message="declaration failed schema validation",
            details={"path": str(path), "errors": problems},
        )
    )


_MODULE_MANIFESTS: dict[str, type[BaseModel]] = {
    "skillsets": SkillsetManifest,
    "promptsets": PromptsetManifest,
    "memorysets": MemorysetManifest,
}


def _validate_module(module_type: str, ref: str, document: dict[str, Any]) -> None:
    """모듈 선언을 종류별 스키마로 — 위반은 ``MOD_003`` 과 ``{field, problem}`` 목록."""
    try:
        _MODULE_MANIFESTS[module_type].model_validate(document)
    except ValidationError as err:
        raise_detached(
            AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_003,
                message=f"module declaration failed schema validation: {ref}",
                details={"module_ref": ref, "errors": field_problems(err)},
            )
        )


def parse_declaration[T: BaseModel](path: Path, model: type[T]) -> T:
    """Read one declaration file as ``model``.

    선언 파일 하나를 모델로 — 스키마 위반은 ``VAL_002`` 과 ``{field, problem}`` 목록으로,
    값은 싣지 않는다. CLI 도 같은 경로를 쓴다.
    """
    return _parse(path, model)


def _parse[T: BaseModel](path: Path, model: type[T]) -> T:
    try:
        return model.model_validate(load_yaml(path))
    except ValidationError as err:
        _invalid(path, err)


def _mismatch(path: Path, *, located: str) -> AaestroError:
    """선언된 이름과 파일 위치가 다르다 — 04 Registry 3 의 integrity 위반.

    기대 값 (위치에서 온 이름) 만 싣는다. 선언에 적힌 이름은 싣지 않는다 — 이름 규칙을 통과한
    문자열이라도 선언의 값이고, 운영자는 경로와 기대 이름만으로 고칠 수 있다.
    """
    return AaestroError(
        category=ErrorCategory.VALIDATION,
        code=ErrorCode.VAL_002,
        message="declared name does not match its location",
        details={"path": str(path), "located": located},
    )


def _checked[T: BaseModel](
    path: Path, model: type[T], key: Callable[[T], str], *, located: str
) -> T:
    parsed = _parse(path, model)
    if key(parsed) != located:
        raise _mismatch(path, located=located)
    return parsed


def _collect[T: BaseModel](
    paths: Iterable[Path],
    model: type[T],
    key: Callable[[T], str],
    *,
    located: Callable[[Path], str],
    root: Path | None = None,
) -> Listing[T]:
    """파싱해 이름으로 묶는다 — **위치가 곧 정체성**이다.

    목록의 키는 파일 위치(디렉토리/파일명)에서 오고, 선언 안의 이름이 그것과
    다르면 깨진 선언으로 보고한다. 그래야 목록의 키로 단건 조회가 되고,
    같은 이름을 선언한 두 파일이 서로를 조용히 덮어쓰지 못한다.
    """
    items: dict[str, T] = {}
    problems: list[Problem] = []
    for path in sorted(paths):
        if root is not None and inside(root, path) is None:
            # 조용히 건너뛰면 운영자가 왜 목록에 없는지 모른다 — 깨진 선언처럼 보고한다
            problems.append(
                Problem(
                    path=str(path),
                    code=ErrorCode.VAL_002,
                    message="declaration resolves outside its root",
                )
            )
            continue
        try:
            parsed = _parse(path, model)
            if key(parsed) != located(path):
                raise _mismatch(path, located=located(path))
        except AaestroError as err:
            problems.append(_problem(str(path), err))
            continue
        items[located(path)] = parsed
    return Listing(items=items, problems=tuple(problems))


@dataclass(frozen=True)
class ModuleListing:
    """Published module versions per name, plus the ones that failed integrity."""

    items: dict[str, tuple[str, ...]] = field(default_factory=dict)
    problems: tuple[Problem, ...] = ()


@dataclass(frozen=True)
class Catalog:
    """Read access to everything the repository declares.

    저장소 선언에 대한 읽기 접근. 경로 레이아웃은 `RegistryRoots` 가 정한다 —
    여기서 경로를 하드코딩하지 않는다 (04 Registry 1).
    """

    roots: RegistryRoots

    @classmethod
    def under(cls, base: Path) -> Catalog:
        """레포 루트 기준 기본 배치."""
        return cls(roots=RegistryRoots.under(base))

    @classmethod
    def from_config(cls, roots: Mapping[str, str] | Any, *, base: Path) -> Catalog:
        """설정의 상대 경로 루트를 `base` 기준으로 해석한다.

        설정(`RegistryConfig.roots`)은 문자열이고 상대 경로다 — control plane 은
        그것을 자기 작업 디렉토리가 아니라 **레포 루트** 기준으로 봐야 한다.
        """
        values = roots if isinstance(roots, Mapping) else roots.model_dump()
        return cls(
            roots=RegistryRoots(
                **{
                    name: (base / Path(values[name])).resolve()
                    for name in RegistryRoots.__dataclass_fields__
                }
            )
        )

    # --- agents ----------------------------------------------------------------

    def agents(self) -> Listing[AgentManifest]:
        return _collect(
            self.roots.agents.glob("*/manifest.yaml"),
            AgentManifest,
            lambda m: m.name,
            located=lambda path: path.parent.name,
            root=self.roots.agents,
        )

    def agent_path(self, name: str) -> Path:
        """에이전트 매니페스트 경로 — 이름 검증과 루트 포함 확인을 거친다."""
        return contained(self.roots.agents, "agent", name, name, "manifest.yaml")

    def agent(self, name: str) -> AgentManifest:
        path = self.agent_path(name)
        if not path.is_file():
            raise not_found("agent", name)
        return _checked(path, AgentManifest, lambda m: m.name, located=name)

    # --- graphs ----------------------------------------------------------------

    def graphs(self) -> Listing[GraphTopology]:
        return _collect(
            self.roots.graphs.glob("*.yaml"),
            GraphTopology,
            lambda g: g.metadata.name,
            located=lambda path: path.stem,
            root=self.roots.graphs,
        )

    def graph_path(self, name: str) -> Path:
        """그래프 선언 경로 — 이름 검증과 루트 포함 확인을 거친다."""
        return contained(self.roots.graphs, "graph", name, f"{name}.yaml")

    def graph(self, name: str) -> GraphTopology:
        path = self.graph_path(name)
        if not path.is_file():
            raise not_found("graph", name)
        return _checked(path, GraphTopology, lambda g: g.metadata.name, located=name)

    # --- groups ----------------------------------------------------------------

    def groups(self) -> Listing[GroupManifest]:
        return _collect(
            self.roots.groups.glob("*.yaml"),
            GroupManifest,
            lambda g: g.metadata.name,
            located=lambda path: path.stem,
            root=self.roots.groups,
        )

    def group_path(self, name: str) -> Path:
        """그룹 선언 경로 — 이름 검증과 루트 포함 확인을 거친다."""
        return contained(self.roots.groups, "group", name, f"{name}.yaml")

    def group(self, name: str) -> GroupManifest:
        path = self.group_path(name)
        if not path.is_file():
            raise not_found("group", name)
        return _checked(path, GroupManifest, lambda g: g.metadata.name, located=name)

    # --- modules ---------------------------------------------------------------

    def module_refs(self) -> frozenset[str]:
        """게시된 모든 모듈 ref — 배포 검증의 `resolvable_refs` 입력.

        **무결성을 통과한 것만** 게시한다. 디렉토리만 있고 문서가 깨진 ref 를
        해석 가능으로 넘기면 배포 검증은 통과하고 로드에서야 실패한다.
        """
        refs: set[str] = set()
        for module_type in MODULE_TYPES:
            for name, versions in self.modules(module_type).items.items():
                refs.update(f"{module_type}/{name}@{version}" for version in versions)
        return frozenset(refs)

    def modules(self, module_type: str) -> ModuleListing:
        """타입별 모듈 이름 → 게시된 버전들 (+ 깨진 것).

        디렉토리 구조가 ``{type}/{name}/{version}/`` 이므로 경로에서 후보를 찾고,
        각 문서를 실제로 읽어 ``kind``/``name``/``version`` 이 맞는지 본다 —
        빈 디렉토리나 깨진 문서는 게시된 것이 아니다.
        """
        if module_type not in MODULE_TYPES:
            raise not_found("module type", module_type)
        type_root = self.roots.for_type(module_type)
        if not type_root.is_dir():
            return ModuleListing()
        registry = ModuleRegistry(self.roots)
        found: dict[str, list[str]] = {}
        problems: list[Problem] = []
        for version_dir in sorted(type_root.glob("*/*")):
            if not version_dir.is_dir():
                continue
            ref = f"{module_type}/{version_dir.parent.name}@{version_dir.name}"
            try:
                # 선언 문서를 스키마로 읽는다 — 오타 난 키가 배포를 통과해 컨테이너나 Memory Service
                # 기동에서야 터지지 않게. yaml 스키마 검증만이다: 스킬 코드는 control plane 에서
                # 실행하지 않고 소유 에이전트 컨테이너 안에서만 로드된다 (04 Loading Isolation)
                _path, document = registry.load_document(ref)
                _validate_module(module_type, ref, document)
            except AaestroError as err:
                problems.append(_problem(str(version_dir), err))
                continue
            found.setdefault(version_dir.parent.name, []).append(version_dir.name)
        return ModuleListing(
            items={name: tuple(versions) for name, versions in found.items()},
            problems=tuple(problems),
        )

    def promptset(self, ref: str) -> PromptsetManifest:
        """Parse a promptset declaration by reference.

        promptset 선언을 ref 로 읽어 파싱한다 — 배포 검증이 노드 id 에 대응하는
        템플릿과 그 필수 변수를 보려면 선언이 필요하다 (#260).

        **선언만 읽는다** — 템플릿 파일의 실재는 `PromptsetLoader` 의 몫이고, 그것은
        렌더 시점의 관심사다. 여기서 파일까지 요구하면 그래프 배선과 무관한 이유로
        저장이 막힌다.

        Raises:
            AaestroError: MODULE/``MOD_001`` 해석 실패, ``MOD_003`` 스키마 위반.
        """
        _path, document = ModuleRegistry(self.roots).load_document(ref)
        try:
            return PromptsetManifest.model_validate(document)
        except ValidationError as err:
            raise_detached(
                AaestroError(
                    category=ErrorCategory.MODULE,
                    code=ErrorCode.MOD_003,
                    message=f"promptset failed schema validation: {ref}",
                    details={"module_ref": ref, "errors": field_problems(err)},
                )
            )

    def module(self, module_type: str, name: str, version: str) -> dict[str, Any]:
        """모듈 선언 문서 — 무결성 검사(`kind`/`name`/`version` 일치)를 거친다."""
        if module_type not in MODULE_TYPES:
            raise not_found("module type", module_type)
        ref = f"{module_type}/{name}@{version}"
        try:
            _path, document = ModuleRegistry(self.roots).load_document(ref)
        except AaestroError as err:
            if err.code == ErrorCode.MOD_001:
                raise not_found("module", ref) from err
            raise
        return document


__all__ = [
    "MODULE_TYPES",
    "Catalog",
    "Listing",
    "ModuleListing",
    "Problem",
    "load_yaml",
    "parse_declaration",
    "not_found",
]
