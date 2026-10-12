"""Unit tests for the skill decorator and schema derivation."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from aaestro.core.skill import SkillContext, build_spec, get_spec, skill


@skill
async def search(ctx: SkillContext, query: str, max_results: int = 10) -> list[dict]:
    """웹 검색을 수행하고 상위 결과를 반환합니다.

    Args:
        query: 검색 질의
        max_results: 최대 결과 개수
    """
    return []


def test_skill_attaches_spec():
    spec = get_spec(search)

    assert spec is not None
    assert spec.name == "search"


def test_schema_snapshot():
    """시그니처 → tool schema 계약 고정.

    이 스냅샷이 깨지면 모델이 보는 계약이 바뀐 것이므로 의도한 변경인지 확인해야 한다.
    """
    spec = get_spec(search)
    assert spec is not None

    assert spec.model_dump() == {
        "name": "search",
        "description": "웹 검색을 수행하고 상위 결과를 반환합니다.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 질의"},
                "max_results": {"type": "integer", "description": "최대 결과 개수", "default": 10},
            },
            "required": ["query"],
        },
    }


def test_context_parameter_is_excluded_from_schema():
    """SkillContext 는 프레임워크가 주입한다 — 모델이 채우는 인자가 아니다."""
    spec = get_spec(search)
    assert spec is not None

    assert "ctx" not in spec.parameters["properties"]


def test_tool_schema_shape():
    spec = get_spec(search)
    assert spec is not None

    schema = spec.to_tool_schema()

    assert set(schema) == {"name", "description", "input_schema"}
    assert schema["input_schema"] == spec.parameters


def test_name_override():
    @skill(name="fetch_page")
    async def fetch(ctx: SkillContext, url: str) -> str:
        """URL 의 본문 텍스트를 추출합니다."""
        return ""

    spec = get_spec(fetch)
    assert spec is not None
    assert spec.name == "fetch_page"


def test_sync_function_is_rejected():
    """모든 skill 은 async — 04-module-system.md Skill Implementation Rules 2."""

    def sync_skill(ctx: SkillContext, q: str) -> str:
        """동기 함수."""
        return q

    with pytest.raises(ValueError, match="must be an async function"):
        build_spec(sync_skill)


def test_function_without_context_is_rejected():
    async def no_ctx() -> str:
        """컨텍스트 없음."""
        return ""

    with pytest.raises(ValueError, match="SkillContext as first parameter"):
        build_spec(no_ctx)


async def _typed(
    ctx: SkillContext,
    text: str,
    count: int,
    ratio: float,
    flag: bool,
    items: list[str],
    mapping: dict[str, int],
    optional: str | None = None,
) -> None:
    """다양한 타입."""


def test_type_hint_mapping():
    spec = build_spec(_typed)
    props = spec.parameters["properties"]

    assert props["text"] == {"type": "string"}
    assert props["count"] == {"type": "integer"}
    assert props["ratio"] == {"type": "number"}
    assert props["flag"] == {"type": "boolean"}
    assert props["items"] == {"type": "array", "items": {"type": "string"}}
    assert props["mapping"] == {"type": "object", "additionalProperties": {"type": "integer"}}
    assert props["optional"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
    }
    assert spec.parameters["required"] == [
        "text",
        "count",
        "ratio",
        "flag",
        "items",
        "mapping",
    ]


def test_description_stops_at_args_section():
    async def documented(ctx: SkillContext, q: str) -> str:
        """첫 줄 설명.

        Args:
            q: 질의
        """
        return q

    spec = build_spec(documented)

    assert spec.description == "첫 줄 설명."


def test_missing_docstring_yields_empty_description():
    async def undocumented(ctx: SkillContext) -> None:
        return None

    assert build_spec(undocumented).description == ""


def test_get_spec_returns_none_for_plain_function():
    async def plain(ctx: SkillContext) -> None:
        """평범한 함수."""

    assert get_spec(plain) is None


async def test_decorated_skill_remains_callable():
    ctx = SkillContext(agent="a", task_id="t", run_id="r")

    assert await search(ctx, "query") == []


class Priority(StrEnum):
    """우선순위."""

    LOW = "low"
    HIGH = "high"


class Payload(BaseModel):
    """구조화 입력."""

    title: str


async def _advanced(
    ctx: SkillContext,
    priority: Priority,
    payload: Payload,
    mixed: str | int,
    raw: Any,
    untyped=None,
) -> None:
    """고급 타입."""


def test_str_enum_becomes_enum_schema():
    props = build_spec(_advanced).parameters["properties"]

    assert props["priority"]["type"] == "string"
    assert props["priority"]["enum"] == ["low", "high"]


def test_pydantic_model_becomes_json_schema():
    props = build_spec(_advanced).parameters["properties"]

    assert props["payload"]["type"] == "object"
    assert "title" in props["payload"]["properties"]


def test_multi_type_union_becomes_any_of():
    props = build_spec(_advanced).parameters["properties"]

    assert props["mixed"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


def test_any_and_untyped_params_have_no_constraint():
    props = build_spec(_advanced).parameters["properties"]

    assert props["raw"] == {}
    assert props["untyped"] == {"default": None}


async def _variadic(ctx: SkillContext, first: str, *args: str, **kwargs: str) -> None:
    """가변 인자."""


def test_variadic_params_are_excluded():
    props = build_spec(_variadic).parameters["properties"]

    assert set(props) == {"first"}


def test_wrong_context_type_is_rejected():
    """첫 파라미터가 SkillContext 가 아니면 런타임 주입 계약이 깨진다."""

    async def wrong_ctx(ctx: str, q: str) -> str:
        """잘못된 컨텍스트 타입."""
        return q

    with pytest.raises(ValueError, match="first parameter must be SkillContext"):
        build_spec(wrong_ctx)


def test_unannotated_context_is_allowed():
    """힌트가 없는 동적 정의 skill 은 막지 않는다."""

    async def dynamic(ctx, q: str) -> str:
        """힌트 없는 컨텍스트."""
        return q

    assert build_spec(dynamic).parameters["required"] == ["q"]


def test_optional_union_keeps_null_member():
    """str | None 은 null 을 허용해야 한다.

    None 을 버리면 default 가 None 인데 스키마는 string 전용인 모순이 생기고,
    모델이 값을 비울 방법이 없어진다.
    """
    props = build_spec(_typed).parameters["properties"]

    assert props["optional"]["anyOf"] == [{"type": "string"}, {"type": "null"}]


def test_typed_dict_preserves_value_constraint():
    """dict[str, int] 의 값 제약이 스키마에 남아야 한다."""
    props = build_spec(_typed).parameters["properties"]

    assert props["mapping"]["additionalProperties"] == {"type": "integer"}


async def _untyped_mapping(ctx: SkillContext, raw: dict[str, Any]) -> None:
    """값 타입이 Any 인 매핑."""


def test_any_valued_dict_stays_unconstrained():
    """값이 Any 면 제약을 만들지 않는다 — 없는 계약을 지어내지 않기 위해."""
    props = build_spec(_untyped_mapping).parameters["properties"]

    assert props["raw"] == {"type": "object", "additionalProperties": True}


# --- silent degradation 을 드러내기 ---------------------------------------------


def _captured_warnings(monkeypatch) -> list[dict]:
    """skill 모듈의 WARN 을 가로챈다."""
    recorded: list[dict] = []

    def capture(event: str, **fields: object) -> None:
        recorded.append({"event": event, **fields})

    monkeypatch.setattr("aaestro.core.skill.log.warning", capture)
    return recorded


def test_unresolvable_hints_are_warned(monkeypatch):
    """조용히 넘기면 모델이 타입 없는 tool 을 본다.

    흔한 원인은 SkillContext 를 TYPE_CHECKING 뒤에 두어 런타임에 이름이
    없는 경우다 — 레퍼런스 스킬셋 작성 중 실제로 밟은 함정이다.
    """
    recorded = _captured_warnings(monkeypatch)

    namespace: dict = {}
    exec(  # noqa: S102 — 미해결 힌트를 만들려면 지연 평가가 필요하다
        "from __future__ import annotations\n"
        "from aaestro.core.skill import skill\n"
        "@skill\n"
        "async def broken(ctx: 'NotImportedHere', query: str) -> str:\n"
        '    """설명."""\n',
        namespace,
    )

    resolution = [r for r in recorded if "could not be resolved" in r["event"]]
    assert resolution, recorded
    assert resolution[0]["tool"] == "broken"
    assert resolution[0]["reason"] == "NameError"


def test_untyped_parameters_are_warned(monkeypatch):
    """타입 없는 파라미터를 주면 모델이 어떤 값을 넣을지 알 수 없다."""
    recorded = _captured_warnings(monkeypatch)

    @skill
    async def loose(ctx: SkillContext, thing, count: int = 1) -> str:
        """설명."""
        return "x"

    warned = [r for r in recorded if "have no type" in r["event"]]
    assert warned
    assert warned[0]["parameters"] == ["thing"]


def test_fully_typed_skill_warns_nothing(monkeypatch):
    """정상 skill 이 경고를 내면 경고가 무의미해진다."""
    recorded = _captured_warnings(monkeypatch)

    @skill
    async def clean(ctx: SkillContext, query: str, limit: int = 10) -> list[str]:
        """설명."""
        return []

    assert recorded == []


def test_untyped_skill_still_loads(monkeypatch):
    """동적 정의 skill 을 막지 않는다 — 경고이지 에러가 아니다."""
    _captured_warnings(monkeypatch)

    @skill
    async def dynamic(ctx: SkillContext, payload) -> str:
        """설명."""
        return "x"

    spec = dynamic.__aaestro_skill__
    assert spec.name == "dynamic"
    assert "payload" in spec.parameters["properties"]


def test_optional_union_is_not_reported_as_untyped(monkeypatch):
    """anyOf 스키마도 타입이 있는 것이다 — 오탐이면 경고를 무시하게 된다."""
    recorded = _captured_warnings(monkeypatch)

    @skill
    async def optional(ctx: SkillContext, value: str | None = None) -> str:
        """설명."""
        return "x"

    assert [r for r in recorded if "have no type" in r["event"]] == []


def test_warnings_use_the_exposed_tool_name(monkeypatch):
    """@skill(name=...) 로 바꾼 경우 운영자가 로그의 이름으로 tool 을 찾을 수 있어야 한다."""
    recorded = _captured_warnings(monkeypatch)

    namespace: dict = {}
    exec(  # noqa: S102 — 미해결 힌트를 만들려면 지연 평가가 필요하다
        "from __future__ import annotations\n"
        "from aaestro.core.skill import skill\n"
        "@skill(name='public_name')\n"
        "async def internal_fn(ctx: 'NotImportedHere', q: str) -> str:\n"
        '    """설명."""\n',
        namespace,
    )

    assert recorded
    assert all(r["tool"] == "public_name" for r in recorded), recorded


def test_untyped_warning_also_uses_the_exposed_name(monkeypatch):
    recorded = _captured_warnings(monkeypatch)

    @skill(name="renamed")
    async def internal(ctx: SkillContext, thing) -> str:
        """설명."""
        return "x"

    warned = [r for r in recorded if "have no type" in r["event"]]
    assert warned[0]["tool"] == "renamed"


# --- pydantic 위임 (#320) ---------------------------------------------------------


class Address(BaseModel):
    city: str


class Person(BaseModel):
    name: str
    home: Address


async def _rich(
    ctx: SkillContext,
    mode: Literal["fast", "thorough"],
    person: Person,
    people: list[Person],
    level: Priority = Priority.LOW,
) -> None:
    """풍부한 타입.

    Args:
        mode: 탐색 방식
        person: 대상 한 명
        people (list[Person]): 대상 여러 명 —
            이어지는 줄도 설명이다
        level: 중요도

    Returns:
        없음
    """


def test_literal_becomes_an_enum():
    """Literal 이 {} 가 되면 모델은 허용 값을 모른 채 아무 문자열이나 보낸다."""
    props = build_spec(_rich).parameters["properties"]

    assert props["mode"]["enum"] == ["fast", "thorough"]
    assert props["mode"]["type"] == "string"


def test_nested_model_references_resolve_at_the_top_level():
    """중첩 모델의 $defs 가 파라미터 안에 갇히면 #/$defs/... 참조가 풀리지 않는다."""
    parameters = build_spec(_rich).parameters
    props = parameters["properties"]

    assert props["person"]["properties"]["home"] == {"$ref": "#/$defs/Address"}
    assert props["people"]["items"] == {"$ref": "#/$defs/Person"}
    assert set(parameters["$defs"]) == {"Address", "Person"}
    for schema in props.values():
        assert "$defs" not in schema


def test_parameter_descriptions_come_from_the_args_section():
    props = build_spec(_rich).parameters["properties"]

    assert props["mode"]["description"] == "탐색 방식"
    assert props["people"]["description"] == "대상 여러 명 — 이어지는 줄도 설명이다"
    assert props["level"]["description"] == "중요도"
    assert props["level"]["default"] == "low"


def test_no_defs_key_without_nested_models():
    assert "$defs" not in build_spec(_typed).parameters


def test_the_description_stops_before_any_section():
    assert build_spec(_rich).description == "풍부한 타입."
