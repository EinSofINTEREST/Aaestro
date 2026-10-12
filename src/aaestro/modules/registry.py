"""Module reference resolution.

모듈 참조(``{type}/{name}@{version}``)를 실제 경로로 해석하는 유일한 경로.
경로 하드코딩을 금지하기 위해 모든 모듈 로드는 registry 를 거친다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import ParsedModuleRef

MODULE_FILENAMES: dict[str, str] = {
    "skillsets": "skillset.yaml",
    "promptsets": "promptset.yaml",
    "memorysets": "memoryset.yaml",
}
"""모듈 타입별 선언 파일 이름. agents/graphs 는 별도 규칙을 따른다."""

MODULE_KINDS: dict[str, str] = {
    "skillsets": "Skillset",
    "promptsets": "Promptset",
    "memorysets": "Memoryset",
    "agents": "Agent",
    "graphs": "Graph",
}
"""ref 타입 ↔ yaml ``kind`` 대응 — integrity 검증에 사용."""


class ModulePath(BaseModel):
    """A resolved module location.

    해석된 모듈 위치 — 선언 파일과 모듈 루트 디렉토리를 함께 제공한다.
    """

    model_config = ConfigDict(frozen=True)

    ref: ParsedModuleRef
    root: Path
    manifest_file: Path

    @property
    def type(self) -> str:
        """모듈 타입 (``skillsets`` 등)."""
        return self.ref.type

    @property
    def name(self) -> str:
        """모듈 이름."""
        return self.ref.name

    @property
    def version(self) -> str:
        """모듈 버전."""
        return self.ref.version


@dataclass(frozen=True)
class RegistryRoots:
    """Per-type resolution roots.

    ref 타입별 해석 루트. config 의 ``registry.roots`` 와 1:1 대응한다.
    """

    skillsets: Path
    promptsets: Path
    memorysets: Path
    agents: Path
    graphs: Path
    groups: Path

    @classmethod
    def under(cls, base: Path) -> RegistryRoots:
        """Build the default layout under a repository root.

        레포 루트 기준 기본 배치를 만듭니다.
        """
        return cls(
            skillsets=base / "modules" / "skillsets",
            promptsets=base / "modules" / "promptsets",
            memorysets=base / "modules" / "memorysets",
            agents=base / "agents",
            graphs=base / "graphs",
            groups=base / "groups",
        )

    def for_type(self, module_type: str) -> Path:
        """타입에 대응하는 해석 루트를 반환한다."""
        root = getattr(self, module_type, None)
        if root is None:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_001,
                message=f"no registry root configured for module type: {module_type}",
            )
        return Path(root)


class ModuleRegistry:
    """Filesystem-backed module registry.

    파일시스템 기반 모듈 레지스트리 (v0.1). ``resolve`` 가 유일한 해석 경로이며,
    게시된 버전 디렉토리는 불변으로 취급한다.
    """

    def __init__(self, roots: RegistryRoots) -> None:
        self._roots = roots

    @classmethod
    def under(cls, base: Path) -> ModuleRegistry:
        """Create a registry rooted at a repository directory.

        레포 루트 기준 레지스트리를 만듭니다.
        """
        return cls(RegistryRoots.under(base))

    @property
    def roots(self) -> RegistryRoots:
        """해석 루트 설정."""
        return self._roots

    def parse(self, ref: str) -> ParsedModuleRef:
        """Parse a module reference.

        모듈 참조를 파싱합니다.

        Args:
            ref: Module reference in ``{type}/{name}@{version}`` format.

        Returns:
            The parsed reference.

        Raises:
            AaestroError: MODULE/``MOD_001`` if the reference is malformed.
        """
        try:
            return ParsedModuleRef.parse(ref)
        except ValueError as err:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_001,
                message=f"invalid module ref: {ref}",
                details={"module_ref": ref},
            ) from err

    def resolve(self, ref: str) -> ModulePath:
        """Resolve a module reference to a filesystem path.

        모듈 참조 문자열을 실제 경로로 해석합니다.
        게시된 버전 디렉토리가 없으면 ``MOD_001`` 을 발생시킵니다.

        Args:
            ref: Module reference in ``{type}/{name}@{version}`` format.

        Returns:
            Resolved module path with a verified declaration file.

        Raises:
            AaestroError: MODULE/``MOD_001`` if the reference cannot be resolved.
        """
        parsed = self.parse(ref)
        root = self._roots.for_type(parsed.type)

        if parsed.type == "agents":
            module_root = root / parsed.name
            manifest_file = module_root / "manifest.yaml"
        elif parsed.type == "graphs":
            module_root = root
            manifest_file = root / f"{parsed.name}.yaml"
        else:
            module_root = root / parsed.name / parsed.version
            manifest_file = module_root / MODULE_FILENAMES[parsed.type]

        if not manifest_file.is_file():
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_001,
                message=f"module not found: {ref}",
                details={"module_ref": ref, "expected_path": str(manifest_file)},
            )
        if not _within(root, manifest_file):
            # 모듈 루트 안의 링크가 밖을 가리키면 루트 밖 코드와 선언을 모듈로 로드한다 (#273)
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_001,
                message=f"module resolves outside the registry root: {ref}",
                details={"module_ref": ref},
            )

        return ModulePath(ref=parsed, root=module_root, manifest_file=manifest_file)

    def load_document(self, ref: str) -> tuple[ModulePath, dict[str, Any]]:
        """Resolve a reference and load its declaration document.

        참조를 해석하고 선언 문서를 로드합니다. ``kind``/``name``/``version`` 이
        ref 와 일치하는지 검증합니다 (integrity check).

        Args:
            ref: Module reference.

        Returns:
            The resolved path and the parsed YAML mapping.

        Raises:
            AaestroError: MODULE/``MOD_003`` if the document is unreadable or
                inconsistent with the reference.
        """
        path = self.resolve(ref)
        document = _read_yaml(path.manifest_file, ref)
        _check_integrity(document, path, ref)
        return path, document


def _within(root: Path, path: Path) -> bool:
    """정규 경로가 루트 안인가 — 링크 순환처럼 해석이 실패하면 밖으로 본다."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, RuntimeError):
        return False


def _read_yaml(file: Path, ref: str) -> dict[str, Any]:
    """선언 yaml 을 읽어 매핑으로 반환한다."""
    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as err:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"failed to read module declaration: {ref}",
            details={"module_ref": ref, "path": str(file)},
        ) from err

    if not isinstance(raw, dict):
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"module declaration must be a mapping: {ref}",
            details={"module_ref": ref, "path": str(file)},
        )
    return raw


def _check_integrity(document: dict[str, Any], path: ModulePath, ref: str) -> None:
    """문서의 kind/name/version 이 ref 와 일치하는지 확인한다."""
    expected_kind = MODULE_KINDS[path.type]
    actual_kind = document.get("kind")
    if actual_kind != expected_kind:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"module kind mismatch: expected {expected_kind}, got {actual_kind}",
            details={"module_ref": ref},
        )

    metadata = document.get("metadata")
    if not isinstance(metadata, dict):
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=f"module declaration is missing metadata: {ref}",
            details={"module_ref": ref},
        )

    if metadata.get("name") != path.name:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=(
                f"module name mismatch: ref says {path.name}, "
                f"declaration says {metadata.get('name')}"
            ),
            details={"module_ref": ref},
        )

    if metadata.get("version") != path.version:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_003,
            message=(
                f"module version mismatch: ref says {path.version}, "
                f"declaration says {metadata.get('version')}"
            ),
            details={"module_ref": ref},
        )


def validation_error(ref: str, err: ValidationError) -> AaestroError:
    """Convert a pydantic validation error at the module boundary.

    모듈 경계에서 pydantic 검증 실패를 ``MOD_003`` 으로 변환합니다.
    """
    return AaestroError(
        category=ErrorCategory.MODULE,
        code=ErrorCode.MOD_003,
        message=f"module schema validation failed: {ref}",
        details={"module_ref": ref, "errors": err.errors(include_url=False)},
    )
