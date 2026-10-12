"""Skillset schema and loader.

스킬셋 선언 스키마와 로더. 스킬셋 코드는 소유 에이전트의 컨테이너 안에서만
import/실행되며, tool 스키마는 함수 시그니처에서 자동 생성된다.
"""

from __future__ import annotations

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from types import ModuleType
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import SemVer
from aaestro.core.skill import SkillSpec, build_spec, get_spec
from aaestro.modules.registry import ModulePath, ModuleRegistry, reject_module

log = structlog.get_logger(__name__)

DEFAULT_SKILL_TIMEOUT_S = 60.0


class SkillDeclaration(BaseModel):
    """A single skill entry in ``skillset.yaml``.

    스킬셋의 개별 skill 선언.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    entrypoint: str
    description: str | None = None
    timeout_s: float = DEFAULT_SKILL_TIMEOUT_S

    @field_validator("entrypoint")
    @classmethod
    def _valid_entrypoint(cls, value: str) -> str:
        """entrypoint 는 ``module:function`` 형식이어야 한다."""
        if value.count(":") != 1:
            raise ValueError("entrypoint must be in 'module:function' format")
        module, function = value.split(":")
        if not module or not function:
            raise ValueError("entrypoint must be in 'module:function' format")
        return value

    @property
    def module_name(self) -> str:
        """entrypoint 의 모듈 부분."""
        return self.entrypoint.split(":")[0]

    @property
    def function_name(self) -> str:
        """entrypoint 의 함수 부분."""
        return self.entrypoint.split(":")[1]


class SkillsetRequires(BaseModel):
    """Skillset requirements checked against the agent manifest.

    스킬셋 요구사항 — 배포 검증에서 에이전트 manifest 와 대조된다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    env: tuple[str, ...] = ()
    python: str | None = None


class SkillsetSpec(BaseModel):
    """Skillset body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skills: tuple[SkillDeclaration, ...]
    requires: SkillsetRequires = Field(default_factory=SkillsetRequires)

    @field_validator("skills")
    @classmethod
    def _unique_names(cls, value: tuple[SkillDeclaration, ...]) -> tuple[SkillDeclaration, ...]:
        """스킬셋 내 tool 이름 중복 금지."""
        if not value:
            raise ValueError("skillset must declare at least one skill")
        names = [s.name for s in value]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError("duplicate skill name")
        return value


class SkillsetMetadata(BaseModel):
    """Skillset metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    version: SemVer
    description: str | None = None


class SkillsetManifest(BaseModel):
    """``skillset.yaml`` document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Skillset"]
    metadata: SkillsetMetadata
    spec: SkillsetSpec


class LoadedSkill(BaseModel):
    """A skill bound to its callable and derived tool schema.

    로드된 skill — 선언, 실제 함수, 그리고 시그니처에서 도출된 tool 스키마.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    declaration: SkillDeclaration
    spec: SkillSpec
    fn: Callable[..., Any]

    @property
    def name(self) -> str:
        """tool 이름 — 선언된 이름이 계약이다."""
        return self.declaration.name

    @property
    def timeout_s(self) -> float:
        """tool 실행 상한."""
        return self.declaration.timeout_s


class LoadedSkillset(BaseModel):
    """A fully loaded skillset.

    로드 완료된 스킬셋.
    """

    model_config = ConfigDict(frozen=True)

    ref: str
    manifest: SkillsetManifest
    skills: tuple[LoadedSkill, ...]

    def tools(self) -> tuple[SkillSpec, ...]:
        """Tool schemas exposed to the model.

        모델에게 노출되는 tool 스키마 목록입니다.
        """
        return tuple(s.spec for s in self.skills)

    def untyped_parameters(self) -> dict[str, tuple[str, ...]]:
        """Report tool parameters the model would see without a type.

        타입 없이 노출되는 파라미터를 보고합니다 — 모델은 그 자리에 어떤 값을
        넣어야 할지 알 수 없습니다.

        흔한 원인은 ``SkillContext`` 를 ``TYPE_CHECKING`` 뒤에 두어 런타임에
        이름이 해석되지 않는 경우입니다. 배포 검증이 이 리포트를 쓰면
        모델에게 도달하기 전에 드러납니다.

        Returns:
            Tool name to its untyped parameter names; empty when all are typed.
        """
        report: dict[str, tuple[str, ...]] = {}
        for spec in self.tools():
            loose = tuple(
                name
                for name, schema in spec.parameters.get("properties", {}).items()
                if not schema.get("type") and "anyOf" not in schema
            )
            if loose:
                report[spec.name] = loose
        return report

    def get(self, name: str) -> LoadedSkill:
        """Look up a loaded skill by tool name.

        tool 이름으로 skill 을 조회합니다.

        Raises:
            AaestroError: MODULE/``MOD_001`` if the skill is not present.
        """
        for item in self.skills:
            if item.name == name:
                return item
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_001,
            message=f"skill not found in skillset: {name}",
            details={"skillset": self.ref, "tool": name},
        )

    @property
    def required_env(self) -> tuple[str, ...]:
        """스킬셋이 요구하는 env 키."""
        return self.manifest.spec.requires.env


class SkillsetLoader:
    """Loads skillsets through the registry.

    레지스트리를 통해 스킬셋을 로드한다. 스킬 모듈은 스킬셋 루트를 기준으로
    격리 import 되며, 스킬셋 간 import 는 허용하지 않는다.
    """

    def __init__(self, registry: ModuleRegistry, *, generation: int = 0) -> None:
        """Args:
        registry: Module resolution.
        generation: Import namespace generation. 같은 세대끼리는 import 한 모듈을 공유하고,
            세대가 바뀌면 스킬 코드를 **새로 실행**한다. 리로드가 세대를 올린다 (#274) —
            ``sys.modules`` 캐시를 그대로 쓰면 코드를 고치고 리로드해도 옛 함수가 남는다.
        """
        self._registry = registry
        self._generation = generation

    def load(self, ref: str) -> LoadedSkillset:
        """Load a skillset and bind its skill functions.

        스킬셋을 로드하고 skill 함수를 바인딩합니다.

        Args:
            ref: Skillset reference (``skillsets/{name}@{version}``).

        Returns:
            The loaded skillset with derived tool schemas.

        Raises:
            AaestroError: MODULE/``MOD_001`` if the ref cannot be resolved,
                ``MOD_003`` if the declaration or an entrypoint is invalid.
        """
        path, document = self._registry.load_document(ref)
        try:
            manifest = SkillsetManifest.model_validate(document)
        except ValidationError as err:
            reject_module(ref, err)

        skills = tuple(self._bind(declaration, path, ref) for declaration in manifest.spec.skills)
        loaded = LoadedSkillset(ref=ref, manifest=manifest, skills=skills)
        _warn_untyped(loaded)
        return loaded

    def _bind(self, declaration: SkillDeclaration, path: ModulePath, ref: str) -> LoadedSkill:
        """선언된 entrypoint 를 실제 함수로 해석하고 스키마를 도출한다."""
        module = _import_module(declaration.module_name, path, ref, generation=self._generation)
        fn = getattr(module, declaration.function_name, None)
        if fn is None:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_003,
                message=(f"skill entrypoint not found: {declaration.entrypoint}"),
                details={"skillset": ref, "skill": declaration.name},
            )

        spec = get_spec(fn)
        if spec is None:
            try:
                spec = build_spec(fn, name=declaration.name)
            except ValueError as err:
                raise AaestroError(
                    category=ErrorCategory.MODULE,
                    code=ErrorCode.MOD_003,
                    message=f"invalid skill '{declaration.name}': {err}",
                    details={"skillset": ref, "skill": declaration.name},
                ) from err

        # 선언된 이름이 계약이다 — 함수명이 달라도 선언 이름으로 노출한다
        if spec.name != declaration.name:
            spec = spec.model_copy(update={"name": declaration.name})
        if declaration.description:
            spec = spec.model_copy(update={"description": declaration.description})

        return LoadedSkill(declaration=declaration, spec=spec, fn=fn)


def _warn_untyped(loaded: LoadedSkillset) -> None:
    """타입 없이 노출되는 파라미터를 로드 시점에 경고한다.

    ``build_spec`` 의 경고는 ``@skill`` 데코레이터가 import 시점에 내므로 어느
    skillset 에서 온 것인지 알 수 없다 — 그 맥락을 아는 것은 로더뿐이다.
    리포트를 반환만 하면 호출자가 부르지 않는 한 아무도 모른다.
    """
    report = loaded.untyped_parameters()
    if not report:
        return
    for tool, parameters in report.items():
        log.warning(
            "skill parameters have no type",
            skillset=loaded.ref,
            tool=tool,
            parameters=list(parameters),
        )


def _register_packages(prefix: str, module_name: str, path: ModulePath) -> None:
    """스킬셋 루트와 그 하위 패키지를 sys.modules 에 등록한다.

    격리 import 로 로드된 모듈이 상대 import (``from .util import ...``) 와
    패키지 내부 절대 import 를 쓸 수 있으려면, 중간 패키지가 ``__path__`` 를 갖고
    등록돼 있어야 한다.
    """
    package = prefix
    directory = path.root
    if package not in sys.modules:
        sys.modules[package] = _make_package(package, directory)

    for part in module_name.split(".")[:-1]:
        directory = directory / part
        parent, package = package, f"{package}.{part}"
        if package not in sys.modules:
            sys.modules[package] = _make_package(package, directory)
        setattr(sys.modules[parent], part, sys.modules[package])


def _make_package(name: str, directory: Path) -> ModuleType:
    """``__path__`` 를 가진 빈 패키지 모듈을 만든다."""
    spec = importlib.machinery.ModuleSpec(name, loader=None, is_package=True)
    spec.submodule_search_locations = [str(directory)]
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(directory)]
    return module


class _SourceLoader(importlib.machinery.SourceFileLoader):
    """Always compile skill code from source.

    바이트코드 캐시는 mtime(초 단위)과 크기로 신선도를 본다. 같은 초 안에 같은 크기로 고친
    파일은 옛 ``.pyc`` 가 그대로 쓰여, 리로드가 세대를 올려도 옛 코드가 실행된다 (#274).
    스킬 코드는 작아서 매번 컴파일하는 비용이 무시할 만하다.
    """

    def get_code(self, fullname: str) -> Any:
        source = self.get_data(self.path)
        return compile(source, self.path, "exec", dont_inherit=True)


_NAMESPACE = "_aaestro_skillset_"


class _SkillsetFinder(importlib.abc.MetaPathFinder):
    """Route every module inside a skillset namespace through `_SourceLoader`.

    진입 모듈만 소스로 컴파일하면, 스킬 코드가 ``from .helper import ...`` 로 끌어오는 하위 모듈은
    기본 finder 가 바이트코드 캐시로 읽어 옛 코드가 섞인다 (#274).
    """

    def find_spec(
        self, fullname: str, path: Sequence[str] | None, target: ModuleType | None = None
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(_NAMESPACE) or path is None:
            return None
        leaf = fullname.rsplit(".", 1)[-1]
        for directory in path:
            package = Path(directory) / leaf / "__init__.py"
            if package.is_file():
                return importlib.util.spec_from_file_location(
                    fullname,
                    package,
                    loader=_SourceLoader(fullname, str(package)),
                    submodule_search_locations=[str(package.parent)],
                )
            module = Path(directory) / f"{leaf}.py"
            if module.is_file():
                return importlib.util.spec_from_file_location(
                    fullname, module, loader=_SourceLoader(fullname, str(module))
                )
        return None


def _install_finder() -> None:
    """한 번만, 맨 앞에 — 기본 path finder 보다 먼저 봐야 캐시를 우회한다."""
    if not any(isinstance(finder, _SkillsetFinder) for finder in sys.meta_path):
        sys.meta_path.insert(0, _SkillsetFinder())


def _import_module(module_name: str, path: ModulePath, ref: str, *, generation: int = 0) -> Any:
    """스킬셋 루트를 기준으로 모듈을 격리 import 한다."""
    file = path.root / Path(*module_name.split(".")).with_suffix(".py")
    if not file.is_file():
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"skill module not found: {module_name}",
            details={"skillset": ref, "expected_path": str(file)},
        )

    # 스킬셋 위치별 고유 이름으로 등록해 모듈 네임스페이스가 겹치지 않게 한다.
    # 같은 name@version 이라도 해석 루트가 다르면 다른 모듈이므로 경로를 키에 포함한다
    location = hashlib.sha256(str(path.root.resolve()).encode()).hexdigest()[:12]
    # 세대를 이름에 넣는다 — 패키지 하위 모듈까지 새 이름이 되어 통째로 다시 실행된다.
    # 옛 세대 모듈은 지우지 않는다: 진행 중 태스크의 옛 함수가 그 네임스페이스를 계속 참조한다
    prefix = f"{_NAMESPACE}{path.name}_{location}_g{generation}"
    qualified = f"{prefix}.{module_name}"
    if qualified in sys.modules:
        return sys.modules[qualified]

    # 중간 패키지를 먼저 등록한다 — 없으면 스킬 코드의 `from .util import ...` 같은
    # 정상적인 스킬셋 내부 import 가 전부 실패한다
    _register_packages(prefix, module_name, path)
    _install_finder()

    spec = importlib.util.spec_from_file_location(
        qualified, file, loader=_SourceLoader(qualified, str(file))
    )
    if spec is None or spec.loader is None:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"failed to load skill module: {module_name}",
            details={"skillset": ref, "path": str(file)},
        )

    module = importlib.util.module_from_spec(spec)
    sys.modules[qualified] = module
    try:
        spec.loader.exec_module(module)
    except Exception as err:
        del sys.modules[qualified]
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"skill module import failed: {module_name}",
            details={"skillset": ref, "path": str(file)},
        ) from err
    return module
