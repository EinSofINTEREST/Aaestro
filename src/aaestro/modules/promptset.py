"""Promptset schema and renderer.

프롬프트셋 선언 스키마와 Jinja2 렌더러. 템플릿 변수는 선언된 스키마로 검증하며,
미선언 변수 사용은 조용한 빈 렌더링 대신 ``MOD_004`` 로 실패시킨다.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, Literal

import structlog
from jinja2 import StrictUndefined, TemplateSyntaxError
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
from aaestro.modules.boundary import (
    DEFAULT_RENDER_LIMIT,
    FENCE_COST,
    OMISSION_NOTICE,
    TRUNCATION_NOTICE,
    Boundary,
    GuardedEnvironment,
    OperationRefusedError,
    RenderLimitError,
    UnsupportedSyntaxError,
    check_syntax,
)
from aaestro.modules.budget import (
    DEFAULT_VARIABLE_LIMIT,
    INPUT_BUDGET_SHARE,
    NestingTooDeepError,
    UnprintableValueError,
    clip,
    weigh,
)
from aaestro.modules.registry import ModulePath, ModuleRegistry, validation_error

DEFAULT_TEMPLATE = "default"

log = structlog.get_logger(__name__)

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
    variable_limit: int = Field(default=DEFAULT_VARIABLE_LIMIT, gt=0)
    """변수 하나의 크기 상한 — 넘으면 자르고 프롬프트와 로그에 알린다."""
    render_limit: int = Field(default=DEFAULT_RENDER_LIMIT, gt=0)
    """렌더 결과 전체의 문자 수 상한 — 넘으면 자르지 않고 실패시킨다.

    값이 쓸 수 있는 예산 (입력 무게의 합, 값 출력의 누적 길이) 도 여기서 유도한다
    (``INPUT_BUDGET_SHARE``). 값 출력은 그 예산에서 생략되므로, 이 상한에 닿는 것은 템플릿의
    정적인 글이 반복문으로 부풀려진 경우다.
    """

    @property
    def value_budget(self) -> int:
        """렌더 한 번에서 값이 쓸 수 있는 문자 수 — 입력 예산이자 출력 예산."""
        numerator, denominator = INPUT_BUDGET_SHARE
        return self.render_limit * numerator // denominator

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

        선언된 변수 스키마로 검증한 뒤 템플릿을 렌더링합니다. 변수는 전부 태스크 입력이므로
        그 값은 경계 표시 안에 놓이고, 상한을 넘는 값은 잘리며, 값 출력이 예산을 넘으면 그 뒤의
        값은 생략됩니다 (04 Promptset Rules 2).

        Args:
            template: Template name (graph ``node_id``, or ``default``).
            locale: Optional locale override; falls back to ``default_locale``.
            **variables: Template variables.

        Returns:
            The rendered prompt text.

        Raises:
            AaestroError: MODULE/``MOD_004`` if a variable fails validation, or the
                template is missing, uses syntax that bypasses the boundary, or
                fails to render. VALIDATION/``VAL_002`` if the task input itself
                cannot be rendered (nested too deeply, unprintable number, an
                operation whose result would exceed the limits), or if the
                rendered prompt exceeds ``render_limit``.
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
        _check_source(source, self.ref, template, ErrorCode.MOD_004)
        guarded, notices, truncated = self._guard(template, resolved)
        chosen = locale or self.manifest.spec.default_locale

        failure: AaestroError | None = None
        try:
            boundary = Boundary(
                f"{self.ref}#{template}@{chosen}", guarded, output_budget=self.value_budget
            )
            # 프롬프트는 HTML 이 아니다 — autoescape 는 프롬프트를 훼손한다 (04 Promptset Rules 2)
            environment = GuardedEnvironment(
                output_limit=self.render_limit,
                autoescape=False,
                undefined=StrictUndefined,
                keep_trailing_newline=True,
                finalize=boundary,
            )
            compiled = environment.from_string(source)
            boundary.arm()
            body = self._collect(compiled.generate(**guarded))
        except MemoryError:
            # 삼키지 않는다 — 프로세스의 상태에 대한 신호이지 이 템플릿의 실패가 아니다
            raise
        # 템플릿은 임의 표현식을 평가한다 (0 나누기, 타입 불일치) — 어떤 실패든 typed 에러로
        # 나가야 한다
        except Exception as err:
            failure = self._render_failure(template, err, truncated)
        if failure is not None:
            # except 밖에서 올린다 — 원인 체인을 일부러 끊는다 (07 "원인 체인 보존" 의 예외).
            # 표현식 예외의 문자열에는 값이 실리고, __cause__ · __context__ 로 남으면 로그에 샌다
            raise failure

        if boundary.omitted:
            notices.append(OMISSION_NOTICE.format(count=boundary.omitted))
            log.warning(
                "template output omitted",
                module_ref=self.ref,
                template=template,
                omitted=boundary.omitted,
                budget=self.value_budget,
            )
        if boundary.marked:
            notices.insert(0, boundary.notice())
        # 안내문은 경계 밖 맨 앞에 선다 — 이 렌더 결과 안에서는 값이 안내문 앞에 끼어들 수 없다
        return "\n".join([*notices, "", body]) if notices else body

    def _collect(self, pieces: Iterator[str]) -> str:
        """렌더 조각을 모은다 — 상한을 넘는 순간 멈춘다.

        값 출력은 훅이 예산에서 생략하므로, 여기서 넘치는 것은 반복문이 부풀린 템플릿의 정적인
        글이다. 수억 자를 다 만든 뒤에 재지 않는다.
        """
        kept: list[str] = []
        length = 0
        for piece in pieces:
            length += len(piece)
            if length > self.render_limit:
                raise RenderLimitError(length, self.render_limit)
            kept.append(piece)
        return "".join(kept)

    def _render_failure(self, template: str, err: Exception, truncated: list[str]) -> AaestroError:
        """렌더 중의 실패를 typed 에러로 — 예외 문자열이 아니라 종류와 수치만 싣는다."""
        details: dict[str, Any] = {"promptset": self.ref, "template": template}
        if isinstance(err, RenderLimitError):
            return AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message=f"rendered prompt exceeds the limit: {template}",
                details={**details, "length": err.length, "limit": err.limit},
            )
        if isinstance(err, OperationRefusedError):
            return AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message=f"template operation would exceed the limit: {template}",
                details={**details, "operator": err.operator},
            )
        if truncated:
            # 잘린 값에서 템플릿이 읽던 자리 (첨자 · 고정 키) 가 사라졌을 수 있다 — 템플릿의 결함이
            # 아니라 입력의 문제로 분류한다
            return AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message=f"task input was truncated and the template could not render: {template}",
                details={**details, "error": type(err).__name__, "truncated": truncated},
            )
        return AaestroError(
            category=ErrorCategory.MODULE,
            code=ErrorCode.MOD_004,
            message=f"template render failed: {template}",
            details={**details, "error": type(err).__name__},
        )

    def _guard(
        self, template: str, resolved: dict[str, Any]
    ) -> tuple[dict[str, Any], list[str], list[str]]:
        """값을 변수 상한과 렌더 한 번의 입력 예산까지 자른다.

        예산은 무게가 작은 변수부터 채운다 (같으면 선언 순서) — 분기를 고르는 짧은 값이 큰 값에
        밀려 조용히 비워지지 않게. 넘기는 값과 안내문의 순서는 선언 순서 그대로다.
        잘렸으면 프롬프트에 실을 안내문과 잘린 변수 이름을 돌려주고 WARN 을 남긴다. 로그에는 값을
        싣지 않는다: 값은 다른 태스크의 산출물이다.
        """
        weights = {name: self._weigh(template, name, value) for name, value in resolved.items()}
        remaining = self.value_budget
        kept: dict[str, Any] = {}
        limits: dict[str, int] = {}
        for name in sorted(resolved, key=lambda name: weights[name]):
            # 변수째 출력되면 붙는 표기 한 쌍도 예산에서 나간다
            limits[name] = max(min(self.variable_limit, remaining - FENCE_COST), 0)
            cut = weights[name] > limits[name]
            kept[name] = clip(resolved[name], limits[name]) if cut else resolved[name]
            spent = weights[name] if kept[name] is resolved[name] else limits[name]
            remaining = max(remaining - FENCE_COST - spent, 0)

        truncated = [name for name in resolved if kept[name] is not resolved[name]]
        notices: list[str] = []
        for name in truncated:
            notices.append(
                TRUNCATION_NOTICE.format(name=name, total=weights[name], limit=limits[name])
            )
            log.warning(
                "template variable truncated",
                module_ref=self.ref,
                template=template,
                variable=name,
                original_size=weights[name],
                limit=limits[name],
            )
        return {name: kept[name] for name in resolved}, notices, truncated

    def _weigh(self, template: str, name: str, value: Any) -> int:
        """변수 하나의 무게 — 잴 수 없는 입력은 ``VAL_002`` 로 거절한다."""
        try:
            return weigh(value)
        except (NestingTooDeepError, UnprintableValueError) as err:
            reason = (
                "is nested too deeply"
                if isinstance(err, NestingTooDeepError)
                else "holds a number too large to print"
            )
            raise AaestroError(
                category=ErrorCategory.VALIDATION,
                code=ErrorCode.VAL_002,
                message=f"variable '{name}' {reason}",
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
    """선언된 템플릿 파일이 실재하고 경계를 우회하지 않는지 로드 시점에 확인한다.

    본문은 렌더할 때마다 다시 읽으므로 강제 지점은 렌더다 — 여기서는 같은 검사로 일찍 알린다.
    """
    for name, spec in manifest.spec.templates.items():
        default = path.root / spec.file
        if not default.is_file():
            raise AaestroError(
                category=ErrorCategory.MODULE,
                code=ErrorCode.MOD_003,
                message=f"declared template file is missing: {spec.file}",
                details={"promptset": ref, "template": name},
            )
        overrides = sorted((path.root / "locales").glob(f"*/{Path(spec.file).name}"))
        for candidate in (default, *overrides):
            try:
                source = candidate.read_text(encoding="utf-8")
            except OSError as err:
                raise AaestroError(
                    category=ErrorCategory.MODULE,
                    code=ErrorCode.MOD_003,
                    message=f"failed to read template file: {spec.file}",
                    details={"promptset": ref, "template": name},
                ) from err
            _check_source(source, ref, name, ErrorCode.MOD_003)


def _check_source(source: str, ref: str, template: str, code: ErrorCode) -> None:
    """경계를 우회하는 구문을 거절한다 — 템플릿 원문은 싣지 않는다.

    같은 검사를 로드 (``MOD_003`` — 모듈이 잘못 게시됐다) 와 렌더 (``MOD_004``) 가 쓴다.
    """
    try:
        check_syntax(source)
    except UnsupportedSyntaxError as err:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=code,
            message=f"template uses unsupported syntax: {template}",
            details={"promptset": ref, "template": template, "node": err.kind, "line": err.line},
        ) from err
    except TemplateSyntaxError as err:
        raise AaestroError(
            category=ErrorCategory.MODULE,
            code=code,
            message=f"template syntax is invalid: {template}",
            details={"promptset": ref, "template": template, "line": err.lineno},
        ) from err
