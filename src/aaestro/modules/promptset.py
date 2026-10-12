"""Promptset schema and renderer.

프롬프트셋 선언 스키마와 Jinja2 렌더러. 템플릿 변수는 선언된 스키마로 검증하며,
미선언 변수 사용은 조용한 빈 렌더링 대신 ``MOD_004`` 로 실패시킨다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from jinja2 import Environment, StrictUndefined, TemplateError
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.manifest import SemVer
from aaestro.modules.registry import ModulePath, ModuleRegistry, validation_error

DEFAULT_TEMPLATE = "default"

_TYPE_CHECKS: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "array": (list, tuple),
    "object": dict,
}


class VariableSpec(BaseModel):
    """A declared template variable.

    선언된 템플릿 변수 — 렌더 시 이 스키마로 검증된다.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["string", "integer", "number", "boolean", "array", "object"]
    required: bool = False
    default: Any = None

    @model_validator(mode="after")
    def _check_default_matches_type(self) -> VariableSpec:
        """default 가 선언 타입과 맞는지 로드 시점에 확인한다.

        어긋난 default 는 렌더 시점에야 실패하거나, 조용히 잘못된 값으로
        렌더된다 — 모듈 로드에서 즉시 잡는다.
        """
        if self.default is not None and not self.check(self.default):
            raise ValueError(
                f"default value does not match declared type '{self.type}': {self.default!r}"
            )
        return self

    def check(self, value: Any) -> bool:
        """값이 선언된 타입에 맞는지 확인한다."""
        expected = _TYPE_CHECKS[self.type]
        if self.type == "integer" and isinstance(value, bool):
            return False
        if self.type == "number" and isinstance(value, bool):
            return False
        return isinstance(value, expected)


class TemplateSpec(BaseModel):
    """A template declaration."""

    model_config = ConfigDict(frozen=True)

    file: str
    variables: dict[str, VariableSpec] = Field(default_factory=dict)
    output_keys: tuple[str, ...] = ()
    """이 템플릿이 요구하는 응답 키.

    **요구하는 곳과 선언하는 곳이 같아야** 드리프트가 구조적으로 불가능하다.
    에이전트 단위로 두면 같은 에이전트가 노드마다 다른 키를 낼 수 없고,
    에이전트가 자기 배선을 알아야 하게 된다 (02 Rule 6 위반).
    """

    @field_validator("output_keys")
    @classmethod
    def _named_keys(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """키는 state schema 필드명과 맞물리므로 식별자 모양이어야 한다."""
        for key in value:
            if not key.isidentifier():
                raise ValueError(f"output key must be an identifier: {key!r}")
        if len(set(value)) != len(value):
            raise ValueError("output keys must be unique")
        return value


class PromptsetSpec(BaseModel):
    """Promptset body."""

    model_config = ConfigDict(frozen=True)

    engine: Literal["jinja2"] = "jinja2"
    default_locale: str = "en"
    templates: dict[str, TemplateSpec]

    @field_validator("templates")
    @classmethod
    def _non_empty(cls, value: dict[str, TemplateSpec]) -> dict[str, TemplateSpec]:
        """템플릿이 하나도 없는 프롬프트셋은 무의미하다."""
        if not value:
            raise ValueError("promptset must declare at least one template")
        return value


class PromptsetMetadata(BaseModel):
    """Promptset metadata."""

    model_config = ConfigDict(frozen=True)

    name: str
    version: SemVer
    description: str | None = None


class PromptsetManifest(BaseModel):
    """``promptset.yaml`` document."""

    model_config = ConfigDict(frozen=True)

    api_version: Literal["aaestro/v1"] = Field(alias="apiVersion")
    kind: Literal["Promptset"]
    metadata: PromptsetMetadata
    spec: PromptsetSpec


class LoadedPromptset(BaseModel):
    """A promptset ready to render.

    렌더 가능한 상태로 로드된 프롬프트셋.
    """

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    ref: str
    manifest: PromptsetManifest
    root: Any

    @property
    def template_names(self) -> frozenset[str]:
        """선언된 템플릿 이름 집합 — 그래프 node_id 호환성 검증에 사용."""
        return frozenset(self.manifest.spec.templates)

    @property
    def has_default(self) -> bool:
        """direct 요청 처리에 필요한 ``default`` 템플릿 보유 여부."""
        return DEFAULT_TEMPLATE in self.manifest.spec.templates

    def render(self, template: str, *, locale: str | None = None, **variables: Any) -> str:
        """Render a template with validated variables.

        선언된 변수 스키마로 검증한 뒤 템플릿을 렌더링합니다.

        Args:
            template: Template name (graph ``node_id``, or ``default``).
            locale: Optional locale override; falls back to ``default_locale``.
            **variables: Template variables.

        Returns:
            The rendered prompt text.

        Raises:
            AaestroError: MODULE/``MOD_004`` if a variable fails validation or
                the template is missing/unrenderable.
        """
        spec = self.manifest.spec.templates.get(template)
        if spec is None:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_004,
                message=f"template not declared: {template}",
                details={"promptset": self.ref, "template": template},
            )

        resolved = self._validate(spec, template, variables)
        source = self._read_template(spec, template, locale)

        # 프롬프트는 HTML 이 아니다 — autoescape 는 프롬프트를 훼손한다 (04 Promptset Rules 2)
        environment = Environment(
            autoescape=False,  # noqa: S701
            undefined=StrictUndefined,
            keep_trailing_newline=True,
        )
        try:
            return environment.from_string(source).render(**resolved)
        except TemplateError as err:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_004,
                message=f"template render failed: {template}",
                details={"promptset": self.ref, "template": template},
            ) from err

    def _validate(
        self, spec: TemplateSpec, template: str, variables: dict[str, Any]
    ) -> dict[str, Any]:
        """변수 스키마 검증 — 미선언/누락/타입 불일치를 모두 차단한다."""
        undeclared = set(variables) - set(spec.variables)
        if undeclared:
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_004,
                message=f"undeclared template variables: {sorted(undeclared)}",
                details={"promptset": self.ref, "template": template},
            )

        resolved: dict[str, Any] = {}
        for name, variable in spec.variables.items():
            if name in variables:
                value = variables[name]
                if not variable.check(value):
                    raise AaestroError(
                        category=ErrorCategory.MODULE,
                        code=ErrorCode.MOD_004,
                        message=(
                            f"variable '{name}' expected {variable.type}, "
                            f"got {type(value).__name__}"
                        ),
                        details={"promptset": self.ref, "template": template},
                    )
                resolved[name] = value
            elif variable.required:
                raise AaestroError(
                    category=ErrorCategory.MODULE,
                    code=ErrorCode.MOD_004,
                    message=f"missing required template variable: {name}",
                    details={"promptset": self.ref, "template": template},
                )
            else:
                resolved[name] = variable.default
        return resolved

    def _read_template(self, spec: TemplateSpec, template: str, locale: str | None) -> str:
        """Locale 오버라이드를 우선 적용해 템플릿 파일을 읽는다."""
        chosen = locale or self.manifest.spec.default_locale
        candidates = []
        if chosen != self.manifest.spec.default_locale:
            candidates.append(self.root / "locales" / chosen / Path(spec.file).name)
        candidates.append(self.root / spec.file)

        for candidate in candidates:
            if candidate.is_file():
                try:
                    return str(candidate.read_text(encoding="utf-8"))
                except OSError as err:
                    raise AaestroError(
                        category=ErrorCategory.MODULE,
                        code=ErrorCode.MOD_004,
                        message=f"failed to read template file: {spec.file}",
                        details={"promptset": self.ref, "template": template},
                    ) from err

        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_004,
            message=f"template file not found: {spec.file}",
            details={"promptset": self.ref, "template": template},
        )


class PromptsetLoader:
    """Loads promptsets through the registry."""

    def __init__(self, registry: ModuleRegistry) -> None:
        self._registry = registry

    def load(self, ref: str) -> LoadedPromptset:
        """Load a promptset declaration.

        프롬프트셋을 로드합니다.

        Args:
            ref: Promptset reference (``promptsets/{name}@{version}``).

        Returns:
            The loaded promptset.

        Raises:
            AaestroError: MODULE/``MOD_001`` if unresolved, ``MOD_003`` if the
                declaration fails schema validation.
        """
        path, document = self._registry.load_document(ref)
        try:
            manifest = PromptsetManifest.model_validate(document)
        except ValidationError as err:
            raise validation_error(ref, err) from err

        _check_template_files(manifest, path, ref)
        return LoadedPromptset(ref=ref, manifest=manifest, root=path.root)


def _check_template_files(manifest: PromptsetManifest, path: ModulePath, ref: str) -> None:
    """선언된 템플릿 파일이 실재하는지 로드 시점에 확인한다."""
    for name, spec in manifest.spec.templates.items():
        if not (path.root / spec.file).is_file():
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_003,
                message=f"declared template file is missing: {spec.file}",
                details={"promptset": ref, "template": name},
            )
