"""Skill decorator and context.

``@skill`` 데코레이터는 함수 시그니처 + type hint 로부터 tool JSON schema 를
자동 생성한다 — 수기 JSON schema 작성을 금지하기 위한 장치다.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, get_type_hints, runtime_checkable

import structlog
from pydantic import BaseModel, ConfigDict, PydanticUserError, TypeAdapter

if TYPE_CHECKING:
    from aaestro.core.agent import SecretsProvider

SKILL_ATTR = "__aaestro_skill__"

log = structlog.get_logger(__name__)


@runtime_checkable
class ArtifactStore(Protocol):
    """Artifact storage access.

    대용량 산출물 저장소 — output 에 직접 싣지 않고 참조로 전달하기 위한 계약.
    """

    async def put(self, key: str, data: bytes) -> str:
        """산출물을 저장하고 참조를 반환한다."""
        ...

    async def get(self, ref: str) -> bytes:
        """참조로부터 산출물을 읽는다."""
        ...


@dataclass(frozen=True)
class SkillContext:
    """Runtime context handed to every skill invocation.

    Skill 실행 컨텍스트. 로거/secrets/artifact 접근은 반드시 이 컨텍스트를
    통해서만 — 전역 상태나 모듈 레벨 클라이언트 초기화는 금지된다.
    """

    agent: str
    task_id: str
    run_id: str
    logger: Any | None = None
    secrets: SecretsProvider | None = None
    artifacts: ArtifactStore | None = None
    trace: Any | None = None
    """이 태스크의 추적 컨텍스트 — peer 위임이 깊이를 이어받는 데 쓴다.

    03 Rule 5 의 깊이 상한은 위임 체인이 부모의 trace 를 물려받아야 성립한다.
    tool 이 그것 없이 위임하면 순환이 상한에 걸리지 않는다.
    """


class SkillSpec(BaseModel):
    """The tool contract derived from a skill function.

    Skill 함수로부터 도출된 tool 계약 — 모델이 보는 스키마 그 자체다.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    description: str
    parameters: dict[str, Any]

    def to_tool_schema(self) -> dict[str, Any]:
        """Render the schema a model provider consumes.

        모델 provider 가 소비하는 tool 스키마 표현으로 변환합니다.
        """
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


DEFS_KEY = "$defs"


def _schema_for_annotation(annotation: Any) -> dict[str, Any]:
    """타입 힌트를 JSON schema 조각으로 변환한다 — pydantic 에 위임한다.

    손으로 짠 변환은 ``Literal`` 을 ``{}`` 로, 중첩 모델의 ``$defs`` 를 잘린 참조로 냈다
    (#320). pydantic 이 모델 계약과 **같은 규칙**으로 만든 스키마를 쓴다. 참조 대상
    (``$defs``)은 여기 남기고 :func:`build_spec` 이 최상위로 끌어올린다.
    """
    if annotation is Any or annotation is inspect.Parameter.empty:
        return {}
    try:
        return TypeAdapter(annotation).json_schema()
    except (PydanticUserError, NameError, TypeError):
        # 해석 불가한 힌트(문자열 forward ref 등) — 타입 없는 파라미터로 남기고
        # build_spec 의 "no type" 경고가 드러낸다
        return {}


def _hoist_defs(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    """파라미터 스키마의 ``$defs`` 를 모은다 — 참조(``#/$defs/X``)는 최상위에서 풀린다."""
    if DEFS_KEY not in schema:
        return schema
    defs.update(schema[DEFS_KEY])
    return {key: value for key, value in schema.items() if key != DEFS_KEY}


_SECTION_HEADERS = {"Args", "Returns", "Raises", "Yields", "Examples", "Example", "Note", "Notes"}


def _description_from_docstring(fn: Callable[..., Any]) -> str:
    """docstring 첫 문단을 tool description 으로 쓴다."""
    doc = inspect.getdoc(fn)
    if not doc:
        return ""
    lines: list[str] = []
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped:
            break
        if stripped.rstrip(":") in _SECTION_HEADERS:
            break
        lines.append(stripped)
    return " ".join(lines)


def _argument_descriptions(fn: Callable[..., Any]) -> dict[str, str]:
    """Google-style ``Args:`` 절의 파라미터 설명 — 모델이 각 인자의 뜻을 알게 한다.

    ``name: 설명`` 한 줄에, 더 깊이 들여쓴 줄은 앞 설명의 이어짐이다.
    ``name (type): 설명`` 형태도 받는다.
    """
    doc = inspect.getdoc(fn)
    if not doc:
        return {}
    descriptions: dict[str, str] = {}
    in_args = False
    entry_indent: int | None = None
    current: str | None = None
    for line in doc.splitlines():
        stripped = line.strip()
        if not in_args:
            in_args = stripped == "Args:"
            continue
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break  # 다음 절 (Returns: 등)
        if entry_indent is None:
            entry_indent = indent
        if indent == entry_indent and ":" in stripped:
            name, _, text = stripped.partition(":")
            current = name.split("(", 1)[0].strip()
            descriptions[current] = text.strip()
        elif current is not None:
            descriptions[current] = f"{descriptions[current]} {stripped}".strip()
    return descriptions


def build_spec(fn: Callable[..., Any], *, name: str | None = None) -> SkillSpec:
    """Derive a tool contract from a skill function.

    Skill 함수 시그니처와 docstring 으로부터 tool 계약을 도출합니다.
    첫 파라미터인 ``SkillContext`` 는 스키마에서 제외됩니다 — 프레임워크가
    주입하는 값이지 모델이 채우는 인자가 아니기 때문입니다.

    Args:
        fn: The skill function (must be ``async def``).
        name: Optional tool name override; defaults to the function name.

    Returns:
        The derived skill specification.

    Raises:
        ValueError: If the function is not async, has no parameters, or its first
            parameter is annotated as something other than :class:`SkillContext`.
    """
    if not inspect.iscoroutinefunction(fn):
        raise ValueError(f"skill '{fn.__name__}' must be an async function")

    signature = inspect.signature(fn)
    params = list(signature.parameters.values())
    if not params:
        raise ValueError(f"skill '{fn.__name__}' must accept a SkillContext as first parameter")

    try:
        hints = get_type_hints(fn)
    except Exception as err:  # noqa: BLE001 - 해석 불가한 힌트는 스키마 없이 진행
        # 조용히 넘기면 **모델이 타입 없는 tool 을 본다** — 어떤 값을 넣어야
        # 할지 알 수 없는 계약이 된다. 흔한 원인은 SkillContext 를
        # TYPE_CHECKING 뒤에 두어 런타임에 이름이 없는 경우다.
        # 동적 정의 skill 을 막지 않기 위해 에러 대신 경고로 드러낸다
        log.warning(
            "skill type hints could not be resolved",
            # 노출되는 이름을 쓴다 — fn.__name__ 을 쓰면 @skill(name=...) 로
            # 바꾼 경우 운영자가 로그의 이름으로 tool 을 찾을 수 없다
            tool=name or fn.__name__,
            reason=type(err).__name__,
            detail=str(err),
        )
        hints = {}

    # 첫 파라미터는 반드시 SkillContext — 어긋나면 런타임 주입 계약이 조용히 깨진다.
    # 힌트가 아예 없는 경우는 허용한다 (동적 정의 skill 을 막지 않기 위해)
    context_hint = hints.get(params[0].name)
    if context_hint is not None and context_hint is not SkillContext:
        raise ValueError(
            f"skill '{fn.__name__}' first parameter must be SkillContext, "
            f"got {getattr(context_hint, '__name__', context_hint)}"
        )

    properties: dict[str, Any] = {}
    required: list[str] = []
    untyped: list[str] = []
    defs: dict[str, Any] = {}
    described = _argument_descriptions(fn)

    for param in params[1:]:
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            continue
        annotation = hints.get(param.name, param.annotation)
        schema = _hoist_defs(_schema_for_annotation(annotation), defs)
        if not schema:
            untyped.append(param.name)
        if param.name in described:
            schema = {**schema, "description": described[param.name]}
        if param.default is inspect.Parameter.empty:
            required.append(param.name)
        else:
            schema = {**schema, "default": param.default}
        properties[param.name] = schema

    if untyped:
        # 타입 없는 파라미터를 모델에게 주면 어떤 값을 넣어야 할지 알 수 없다.
        # 스키마 자체는 그대로 두고(동적 skill 허용) 경고로만 드러낸다
        log.warning(
            "skill parameters have no type",
            tool=name or fn.__name__,
            parameters=untyped,
        )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "required": required,
    }
    if defs:
        parameters[DEFS_KEY] = defs

    return SkillSpec(
        name=name or fn.__name__,
        description=_description_from_docstring(fn),
        parameters=parameters,
    )


def skill(fn: Callable[..., Any] | None = None, *, name: str | None = None) -> Callable[..., Any]:
    """Mark an async function as a skill and attach its derived tool schema.

    Async 함수를 skill 로 표시하고 도출된 tool 스키마를 부착합니다.
    시그니처가 곧 스키마이므로 수기 JSON schema 작성은 필요하지 않습니다.

    Args:
        fn: The skill function when used without parentheses.
        name: Optional tool name override.

    Returns:
        The same function, annotated with its :class:`SkillSpec`.
    """

    def decorate(target: Callable[..., Any]) -> Callable[..., Any]:
        spec = build_spec(target, name=name)
        setattr(target, SKILL_ATTR, spec)
        return target

    if fn is not None:
        return decorate(fn)
    return decorate


def get_spec(fn: Callable[..., Any]) -> SkillSpec | None:
    """Read the skill spec attached by :func:`skill`.

    ``@skill`` 이 부착한 스펙을 읽습니다. skill 이 아니면 ``None``.
    """
    spec = getattr(fn, SKILL_ATTR, None)
    return spec if isinstance(spec, SkillSpec) else None
