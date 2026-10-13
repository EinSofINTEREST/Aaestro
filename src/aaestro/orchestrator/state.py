"""Graph state schema utilities and merge rules.

그래프 state 스키마 해석과 병합 규칙. state 는 워크플로 계약 데이터이며,
노드는 선언된 키만 병합할 수 있다 — 전체 덮어쓰기는 금지된다.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, create_model

from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    field_problems,
    raise_detached,
)
from aaestro.orchestrator.topology import NodeSpec, StateField, StateSpec, resolve_import_ref

_STATE_PREFIX = "state."
_OUTPUT_PREFIX = "output."


def _state_error(message: str, **details: Any) -> AaestroError:
    """state 스키마 불일치/병합 실패를 ``GRAPH_003`` 으로 만든다."""
    return AaestroError(
        category=ErrorCategory.GRAPH,
        code=ErrorCode.GRAPH_003,
        message=message,
        details=details,
    )


def resolve_state_schema(ref: str) -> type[BaseModel]:
    """Resolve a graph state schema reference to a pydantic model.

    그래프 state 스키마 참조를 pydantic 모델로 해석합니다.

    Args:
        ref: Importable reference such as ``aaestro.graphs.schemas:ResearchState``.

    Returns:
        The resolved state model class.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` if the ref cannot be imported,
            GRAPH/``GRAPH_003`` if the target is not a pydantic model.
    """
    target = resolve_import_ref(ref)
    if not (isinstance(target, type) and issubclass(target, BaseModel)):
        raise _state_error(f"state schema is not a pydantic model: {ref}", ref=ref)
    return target


_FIELD_TYPES: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list[Any],
    "object": dict[str, Any],
}
_EMPTY_FACTORIES: dict[str, Callable[[], Any]] = {"array": list, "object": dict}
_INLINE_MODELS: dict[str, type[BaseModel]] = {}


def resolve_graph_state(state: StateSpec, *, graph: str = "graph") -> type[BaseModel]:
    """The pydantic model for a graph's state, whichever way it was declared.

    그래프 state 의 pydantic 모델 — 인라인 ``fields`` 면 만들고, ``schema`` 면 import 합니다.

    Raises:
        AaestroError: GRAPH/``GRAPH_001`` or ``GRAPH_003`` from :func:`resolve_state_schema`.
    """
    if state.declared_fields is None:
        assert state.schema_ref is not None  # noqa: S101 — StateSpec 이 둘 중 하나를 강제
        return resolve_state_schema(state.schema_ref)
    return inline_state_model(graph, state.declared_fields)


def inline_state_model(graph: str, fields: Mapping[str, StateField]) -> type[BaseModel]:
    """YAML 필드 선언으로 state 모델을 만든다 — 같은 선언이면 같은 클래스를 돌려준다.

    레포의 state 모델과 같은 규칙이다: 불변(frozen), 목록·객체는 비어 있는 채로 시작한다.
    """
    key = json.dumps(
        [graph, {name: spec.model_dump(mode="json") for name, spec in fields.items()}],
        sort_keys=True,
    )
    cached = _INLINE_MODELS.get(key)
    if cached is not None:
        return cached
    definitions: dict[str, Any] = {name: _field_definition(spec) for name, spec in fields.items()}
    model: type[BaseModel] = create_model(
        f"{_class_name(graph)}State", __config__=ConfigDict(frozen=True), **definitions
    )
    _INLINE_MODELS[key] = model
    return model


def _field_definition(spec: StateField) -> tuple[Any, Any]:
    kind = _FIELD_TYPES[spec.type]
    description = spec.description
    if spec.required:
        return kind, Field(..., description=description)
    if spec.default is not None:
        default = spec.default
        return kind, Field(default_factory=lambda: copy.deepcopy(default), description=description)
    if spec.type in _EMPTY_FACTORIES:
        return kind, Field(default_factory=_EMPTY_FACTORIES[spec.type], description=description)
    return kind | None, Field(default=None, description=description)


def _class_name(graph: str) -> str:
    return "".join(part.capitalize() for part in graph.replace("_", "-").split("-")) or "Graph"


def state_fields(schema: type[BaseModel]) -> frozenset[str]:
    """List the declared field names of a state schema.

    state 스키마의 선언된 필드 이름을 반환합니다 — ``input_map`` 검증에 쓰입니다.
    """
    return frozenset(schema.model_fields)


def schema_defaults(schema: type[BaseModel] | None) -> dict[str, Any]:
    """Collect the schema's declared default values.

    state schema 가 선언한 기본값을 모읍니다 — LangGraph 채널은 값이 설정되기
    전까지 키를 만들지 않으므로, 기본값이 있는 필드도 state dict 에는 없습니다.
    """
    if schema is None:
        return {}
    return {
        name: field.get_default(call_default_factory=True)
        for name, field in schema.model_fields.items()
        if not field.is_required()
    }


def extract_input(
    node: NodeSpec, state: dict[str, Any], *, schema: type[BaseModel] | None = None
) -> dict[str, Any]:
    """Build a task input mapping from graph state.

    ``input_map`` 선언에 따라 state 에서 태스크 입력을 추출합니다.
    ``state.<field>`` 형식만 state 를 참조하며, 그 외 값은 리터럴로 취급합니다.

    ``schema`` 를 주면 **선언된 기본값을 사용**합니다. LangGraph 채널은 값이
    설정되기 전까지 키를 만들지 않으므로, 기본값이 있는 필드를 읽는 노드가
    첫 실행에서 ``GRAPH_003`` 으로 막히는 것을 방지합니다 — 04 규칙은
    "``input_map`` 의 키가 **schema 에 존재**" 만 요구합니다.

    Args:
        node: The node whose ``input_map`` drives extraction.
        state: Current graph state.
        schema: State schema supplying declared defaults.

    Returns:
        The task input mapping.

    Raises:
        AaestroError: GRAPH/``GRAPH_003`` if a referenced field is absent from
            state and the schema declares no default for it.
    """
    defaults = schema_defaults(schema)
    extracted: dict[str, Any] = {}
    for key, source in node.input_map.items():
        # state 참조는 문자열 `state.<path>` 뿐 — 숫자/불리언/리스트 등은 리터럴
        if not (isinstance(source, str) and source.startswith(_STATE_PREFIX)):
            extracted[key] = source
            continue
        path = source.removeprefix(_STATE_PREFIX)
        extracted[key] = _read_path(state, path, node_id=node.id, source=source, defaults=defaults)
    return extracted


def _read_path(
    state: dict[str, Any],
    path: str,
    *,
    node_id: str,
    source: str,
    defaults: dict[str, Any] | None = None,
) -> Any:
    """점 표기 경로로 state 값을 읽는다 — 없으면 schema 기본값으로 떨어진다."""
    parts = path.split(".")
    current: Any = state
    for index, part in enumerate(parts):
        if isinstance(current, dict) and part in current:
            current = current[part]
            continue
        # 최상위 필드이고 schema 가 기본값을 선언했으면 그것을 쓴다
        if index == 0 and defaults is not None and part in defaults:
            current = defaults[part]
            continue
        raise _state_error(
            f"state field not found: {source}",
            node_id=node_id,
        )
    return current


def merge_output(
    node: NodeSpec,
    output: dict[str, Any],
    *,
    schema: type[BaseModel] | None = None,
) -> dict[str, Any]:
    """Project a task output onto declared state keys.

    ``output_map`` 에 선언된 키만 state 갱신 대상으로 투영합니다 —
    노드가 state 전체를 덮어쓰는 패턴을 구조적으로 차단합니다.

    Args:
        node: The node whose ``output_map`` drives the projection.
        output: The task result output.
        schema: Optional state schema used to reject undeclared target keys.

    Returns:
        The partial state update (only declared keys).

    Raises:
        AaestroError: GRAPH/``GRAPH_003`` if a mapped source is absent from the
            output, or a target key is not declared in the state schema.
    """
    update: dict[str, Any] = {}
    known = state_fields(schema) if schema is not None else None

    for state_key, source in node.output_map.items():
        if known is not None and state_key not in known:
            raise _state_error(
                f"output_map targets unknown state field: {state_key}",
                node_id=node.id,
            )
        path = source.removeprefix(_OUTPUT_PREFIX) if source.startswith(_OUTPUT_PREFIX) else source
        value = _read_path(output, path, node_id=node.id, source=source)
        if schema is not None:
            # **변환 결과**를 쓴다: 검증만 하고 원값을 넣으면 "3" 이 int 필드에
            # 문자열로 남아, 계약을 강제한다면서 계약과 다른 값을 넣게 된다
            value = _coerced(schema, state_key, value, node_id=node.id)
        update[state_key] = value

    return update


def _coerced(schema: type[BaseModel], field: str, value: Any, *, node_id: str) -> Any:
    """Validate one projected value against its declared field.

    투영값 하나를 선언된 필드로 검증하고, **변환된 값**을 돌려줍니다.

    **모델 전체가 아니라 필드 단위로 본다**: 노드는 state 일부만 돌려주므로,
    전체를 검증하면 아직 채워지지 않은 필수 필드에 걸린다.

    Returns:
        The value as the declared type sees it.

    Raises:
        AaestroError: GRAPH/``GRAPH_003`` if the value does not satisfy the
            declared type — 선언과 다른 타입이 들어가면 그것을 소비하는 노드가
            런타임에 깨지고, 원인이 몇 단계 뒤에서 드러난다.
    """
    declared = schema.model_fields.get(field)
    if declared is None:  # 앞선 known 검사가 이미 걸렀다
        return value
    try:
        return TypeAdapter(declared.annotation).validate_python(value)
    except ValidationError as err:
        raise_detached(
            _state_error(
                f"output does not satisfy the state field: {field}",
                node_id=node_id,
                field=field,
                problem=(field_problems(err) or [{"problem": "type mismatch"}])[0]["problem"],
            )
        )


def validate_state(schema: type[BaseModel], state: dict[str, Any]) -> dict[str, Any]:
    """Validate a state mapping against the graph state schema.

    state 매핑을 그래프 state 스키마로 검증합니다.

    Args:
        schema: The graph state model.
        state: The state mapping to validate.

    Returns:
        The validated state as a plain mapping.

    Raises:
        AaestroError: GRAPH/``GRAPH_003`` if the state does not satisfy the schema.
    """
    try:
        return schema.model_validate(state).model_dump()
    except ValidationError as err:
        # 어느 필드가 왜 틀렸는지 알려주지 않으면 운영자가 고칠 수 없다
        # (config.py 의 CFG_001 과 같은 수준으로 맞춘다)
        raise_detached(
            _state_error(
                "state does not satisfy the graph schema",
                schema=schema.__name__,
                errors=field_problems(err),
            )
        )
