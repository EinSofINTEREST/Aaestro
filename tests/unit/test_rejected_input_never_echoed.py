"""Property: a rejected input value never comes back in the error (#386).

성질 테스트 — 거절된 입력 값은 **조각으로도** 에러에 되돌아오지 않는다. 특정 validator 나 특정
값을 보는 테스트는 다음에 추가되는 validator 에서 같은 결함이 값만 바꿔 돌아오는 것을 막지
못했다 (세 번 재발했다). 그래서 모든 선언 · 설정 문서의 **모든 잎 위치** 에 표식 변형을 넣고,
세 겹에서 표식의 조각을 찾는다:

1. validator 가 직접 쓴 메시지 (가리기 전) — 새 validator 가 값을 보간하면 여기서 걸린다.
2. ``field_problems`` 의 결과 — pydantic 자신의 메시지가 값을 되비추는 경우까지 가려졌는가.
3. 입구 (로더 · 카탈로그 · agentd · 설정) 에서 올라온 ``AaestroError`` 전체 — 스키마 검증 밖의
   검사 (ref 대조, 이름 · 위치 대조) 가 선언의 값을 메시지에 싣지 않는가.
"""

from __future__ import annotations

import copy
import json
import shutil
import unicodedata
from collections.abc import Callable, Iterator
from dataclasses import asdict
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit

import pytest
import yaml
from pydantic import BaseModel, ValidationError, field_validator

from aaestro.agentd.__main__ import load_manifest
from aaestro.catalog import Catalog, parse_declaration
from aaestro.config import AaestroConfig, load_config
from aaestro.core import errors
from aaestro.core.errors import (
    MAX_FIELD_LENGTH,
    MAX_LABEL_LENGTH,
    MAX_PROBLEM_LENGTH,
    MIN_SHARED_LENGTH,
    REDACTED,
    AaestroError,
    field_problems,
    repeated_positions,
)
from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.modules.memoryset import MemorysetLoader, MemorysetManifest
from aaestro.modules.promptset import PromptsetLoader, PromptsetManifest
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skillset import SkillsetLoader, SkillsetManifest
from aaestro.orchestrator.topology import GraphTopology
from tests.fixtures.declarations import MARKER, REPO_ROOT, _declaration_documents
from tests.fixtures.echo_fuzz import (
    FULLWIDTH,
    IDENTIFIER,
    MARKS,
    MIN_FRAGMENT,
    RICH_AGENT,
    RICH_DOCUMENTS,
    RICH_GRAPH,
    Mark,
    fuzz_documents,
    leaf_positions,
    mutations,
    shape_of,
    with_value,
)

VALIDATOR_AUTHORED = frozenset({"value_error", "assertion_error"})
"""우리 validator 가 던진 예외에서 온 에러 종류 — 메시지를 우리 코드 (또는 그 안에서 새어 나온
표준 라이브러리 예외) 가 썼다."""
MIN_MUTATIONS = 5000
"""퍼즈가 조용히 줄어들지 않았는지 보는 하한 (문서 모양 × 잎 위치 × 표식 변형)."""
SHOWN = 5


def test_the_fragment_length_is_the_length_field_problems_hides():
    assert MIN_FRAGMENT == MIN_SHARED_LENGTH


@pytest.mark.parametrize(("label", "model", "document"), RICH_DOCUMENTS, ids=lambda v: str(v)[:12])
def test_rich_fuzz_documents_are_valid(label: str, model: type[BaseModel], document: Any):
    """채운 문서가 스키마에서 벗어나면 그 아래의 잎은 검증에 닿지 못한다 — 퍼즈가 얕아진다."""
    model.model_validate(document)


def _reachable(model: type[BaseModel], seen: set[type[BaseModel]]) -> None:
    """선언 모델에서 닿는 모든 중첩 모델."""
    if model in seen:
        return
    seen.add(model)

    def descend(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _reachable(annotation, seen)
        for argument in get_args(annotation):
            descend(argument)

    for field in model.model_fields.values():
        descend(field.annotation)


def _fields_set(value: Any, used: set[tuple[str, str]]) -> None:
    """검증된 문서가 **명시적으로** 채운 (모델, 필드) — 기본값으로 채워진 것은 세지 않는다."""
    if isinstance(value, BaseModel):
        for name in value.model_fields_set:
            used.add((type(value).__name__, name))
            _fields_set(getattr(value, name), used)
    elif isinstance(value, dict):
        for item in value.values():
            _fields_set(item, used)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _fields_set(item, used)


def test_fuzz_documents_set_every_field_of_every_declaration_model():
    """퍼즈는 문서에 **있는** 잎만 바꾼다 — 스키마에 필드를 추가하고 문서에 넣지 않으면 그 필드의
    validator 는 조용히 퍼즈 밖에 놓인다. 새 필드는 ``RICH_*`` 문서에 함께 추가한다."""
    models: set[type[BaseModel]] = set()
    used: set[tuple[str, str]] = set()
    for _label, model, document in fuzz_documents():
        _reachable(model, models)
        _fields_set(model.model_validate(document), used)

    missing = sorted(
        (model.__name__, name)
        for model in models
        for name in model.model_fields
        if (model.__name__, name) not in used
    )
    assert missing == []


# --- 1 · 2: 모델 검증 ------------------------------------------------------------------------


def test_no_validator_message_and_no_field_problem_carries_a_piece_of_the_input():
    authored: list[str] = []
    reported: list[str] = []
    total = rejected = 0

    for label, model, path, mark, mutated in mutations(fuzz_documents()):
        total += 1
        try:
            model.model_validate(mutated)
        except ValidationError as err:
            rejected += 1
            where = f"{label} {'.'.join(map(str, path))} [{mark.name}]"
            for entry in err.errors(include_url=False):
                if entry["type"] in VALIDATOR_AUTHORED and mark.leaked_in(entry["msg"]):
                    authored.append(f"{where}: {entry['msg'][:160]!r}")
            for problem in field_problems(err):
                if mark.leaked_in(problem["problem"]):
                    reported.append(f"{where}: {problem['problem'][:160]!r}")

    assert total >= MIN_MUTATIONS
    assert rejected > total // 2
    # (1) validator 가 값을 메시지에 보간했다 — 고정 문구로 바꾼다 (field_problems 가 가려 주는
    #     것은 두 번째 겹이지, validator 가 값을 실어도 된다는 뜻이 아니다)
    assert authored[:SHOWN] == [], f"{len(authored)} validator messages echo the input"
    # (2) 가리기를 지나고도 조각이 남았다
    assert reported[:SHOWN] == [], f"{len(reported)} field problems echo the input"


# --- 2: 가리기 자체 — 값의 조각 · 변형 --------------------------------------------------------


class _EchoingProbe(BaseModel):
    """새로 추가된 validator 를 흉내 낸다 — 저마다 다른 식으로 값의 **조각** 을 되비춘다."""

    tail: str = ""
    netloc: str = ""
    shouted: str = ""
    seven: str = ""
    composed: str = ""
    lowered: str = ""

    @field_validator("tail")
    @classmethod
    def _echo_tail(cls, value: str) -> str:
        raise ValueError(f"cannot use {value[3:]} here")

    @field_validator("netloc")
    @classmethod
    def _echo_netloc(cls, value: str) -> str:
        raise ValueError(f"netloc {urlsplit(value).netloc!r} is not allowed")

    @field_validator("shouted")
    @classmethod
    def _echo_upper(cls, value: str) -> str:
        raise ValueError(f"unknown name {value.upper()}")

    @field_validator("seven")
    @classmethod
    def _echo_seven(cls, value: str) -> str:
        raise ValueError(f"starts with {value[:7]}")

    @field_validator("composed")
    @classmethod
    def _echo_nfkc(cls, value: str) -> str:
        # 파이썬 식별자 · IDNA · 파일 이름 처리가 하는 일 — 전각이 ASCII 로 나온다
        raise ValueError(f"unknown name '{unicodedata.normalize('NFKC', value)}'")

    @field_validator("lowered")
    @classmethod
    def _echo_casefold(cls, value: str) -> str:
        raise ValueError(f"no such entry: {value.casefold()}")


def _probe_problem(**fields: str) -> str:
    with pytest.raises(ValidationError) as exc_info:
        _EchoingProbe.model_validate(fields)
    (problem,) = field_problems(exc_info.value)
    return problem["problem"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tail", MARKER),
        ("netloc", f"https://user:{MARKER}@host.example/mcp"),
        ("shouted", MARKER),
        # 정규화된 되비침 — 메시지의 글자가 입력의 글자와 다르다
        ("composed", IDENTIFIER.translate(FULLWIDTH)),
        ("lowered", MARKER.upper()),
        # 접으면 길이가 바뀌는 글자가 하나 있어도 접기가 통째로 꺼지지 않는다
        ("lowered", f"İ{MARKER.upper()}"),
        ("lowered", f"STRAßE {MARKER.upper()}"),
        ("composed", f"İ{IDENTIFIER.translate(FULLWIDTH)}ß"),
    ],
)
def test_field_problems_hide_a_piece_of_the_value_not_only_the_whole(field: str, value: str):
    problem = _probe_problem(**{field: value})

    assert REDACTED in problem
    assert MARKS[0].leaked_in(problem) is None
    assert Mark("identifier", IDENTIFIER, (IDENTIFIER,)).leaked_in(problem) is None


def test_field_problems_leave_an_overlap_shorter_than_the_threshold_alone():
    """과잉 가림도 결함이다 — 우연히 겹친 짧은 조각까지 가리면 메시지를 읽을 수 없다."""
    assert _probe_problem(seven="abcdefg-and-the-rest") == "Value error, starts with abcdefg"


def test_field_problems_hide_a_value_spread_over_a_large_input():
    """잎이 많은 입력 (선언 전체가 입력인 after-validator) 에서도 조각을 찾는다."""

    class _Whole(BaseModel):
        items: list[str]

        @field_validator("items")
        @classmethod
        def _echo(cls, value: list[str]) -> list[str]:
            raise ValueError(f"item {value[-1][5:]} repeats")

    with pytest.raises(ValidationError) as exc_info:
        _Whole.model_validate({"items": [f"filler-{n}" for n in range(2000)] + [f"key={MARKER}"]})

    (problem,) = field_problems(exc_info.value)
    assert MARKS[0].leaked_in(problem["problem"]) is None


# --- 가림의 비용: 입력 크기에 선형 -------------------------------------------------------------


class _Verbose(BaseModel):
    text: str

    @field_validator("text")
    @classmethod
    def _echo_something_else(cls, value: str) -> str:
        # 입력만큼 길지만 입력과 겹치지 않는 메시지 — 정규화해 되비추는 validator 가 이렇게 된다
        raise ValueError("#" * len(value))


def test_redaction_looks_up_the_input_a_bounded_number_of_times(monkeypatch: pytest.MonkeyPatch):
    """메시지의 모든 위치에서 입력을 찾으면 (메시지 × 입력) 의 2차 비용이다 — 16만 자 조건식 하나로
    control plane 의 이벤트 루프가 8.8초 멈췄다. 시간이 아니라 찾는 **횟수** 를 본다 (결정적)."""
    lookups: list[int] = []
    find = errors._find_shared

    def counted(haystack: str, piece: str) -> int:
        lookups.append(len(haystack))
        return find(haystack, piece)

    monkeypatch.setattr(errors, "_find_shared", counted)
    size = 200_000

    with pytest.raises(ValidationError) as exc_info:
        _Verbose.model_validate({"text": "x" * size})
    (problem,) = field_problems(exc_info.value)

    assert max(lookups) >= size  # 큰 입력을 실제로 찾았다 — 찾기를 건너뛴 것이 아니다
    assert len(lookups) <= MAX_PROBLEM_LENGTH + MIN_SHARED_LENGTH
    assert len(problem["problem"]) <= MAX_PROBLEM_LENGTH + 1


def test_redaction_then_truncation_leaves_no_piece_at_the_cut():
    """가린 뒤에 자른다 — 자르고 가리면 절단 지점에 걸친 값 조각이 짧게 남아 가림을 피한다."""

    class _Padded(BaseModel):
        text: str

        @field_validator("text")
        @classmethod
        def _echo(cls, value: str) -> str:
            raise ValueError("." * (MAX_PROBLEM_LENGTH - len("Value error, ") - 4) + value)

    with pytest.raises(ValidationError) as exc_info:
        _Padded.model_validate({"text": MARKER})
    (problem,) = field_problems(exc_info.value)

    assert MARKER[:4] not in problem["problem"]
    # 512자에서 잘리므로 가림 표시도 중간에 끊길 수 있다 — 값 대신 표시가 그 자리에 있으면 된다
    assert problem["problem"].rstrip("…").endswith(REDACTED[:4])


# --- 값 대신 위치: 어느 것인지는 말한다 ---------------------------------------------------------


def _problems(model: type[BaseModel], document: dict[str, Any]) -> list[dict[str, str]]:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(document)
    return field_problems(exc_info.value)


def _repeat_item(document: dict[str, Any], path: tuple[str | int, ...]) -> dict[str, Any]:
    """``path`` 의 리스트에 항목 둘을 더해 0번과 마지막이 같게 — 가운데에 다른 항목이 낀다."""
    mutated = copy.deepcopy(document)
    items: Any = mutated
    for step in path:
        items = items[step]
    other = copy.deepcopy(items[0])
    for key in ("id", "name", "as"):
        if key in other:
            other[key] = "other"
    items.extend([other, copy.deepcopy(items[0])])
    return mutated


@pytest.mark.parametrize(
    ("model", "document", "path", "field", "problem"),
    [
        (
            GraphTopology,
            RICH_GRAPH,
            ("spec", "nodes"),
            "spec.nodes",
            "duplicate node id at index 0 and 3",
        ),
        (
            AgentManifest,
            RICH_AGENT,
            ("spec", "memory", "spaces"),
            "spec.memory.spaces",
            "duplicate memory space alias at index 0 and 2",
        ),
        (
            SkillsetManifest,
            None,
            ("spec", "skills"),
            "spec.skills",
            "duplicate skill name at index 0 and",
        ),
    ],
    ids=["node-id", "memory-alias", "skill-name"],
)
def test_a_duplicate_is_reported_by_position(
    model: type[BaseModel],
    document: dict[str, Any] | None,
    path: tuple[str | int, ...],
    field: str,
    problem: str,
):
    """값은 싣지 않지만 **어느 것** 인지는 말한다 — 노드 수십 개를 눈으로 대조하게 두지 않는다."""
    source = document or next(
        doc for doc in _declaration_documents().values() if doc["kind"] == "Skillset"
    )

    (found,) = _problems(model, _repeat_item(source, path))

    assert found["field"] == field
    assert problem in found["problem"]


def test_a_repeated_mcp_server_name_is_reported_by_position():
    servers = [
        {"name": "a", "transport": "stdio", "command": ["x"]},
        {"name": "b", "transport": "stdio", "command": ["y"]},
        {"name": "a", "transport": "stdio", "command": ["z"]},
    ]
    document = with_value(RICH_AGENT, ("spec", "mcp"), {"servers": servers})

    (found,) = _problems(AgentManifest, document)

    assert found == {
        "field": "spec.mcp.servers",
        "problem": "Value error, duplicate mcp server name at index 0 and 2",
    }


def test_a_repeated_fallback_says_which_one_and_what_it_repeats():
    fallbacks = [
        {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]},
        {"provider": "anthropic", "name": "claude-opus-4-5", "on": ["rate_limit"]},
        {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]},
    ]
    repeats_fallback = with_value(RICH_AGENT, ("spec", "model", "fallbacks"), fallbacks)
    repeats_primary = with_value(
        RICH_AGENT,
        ("spec", "model", "fallbacks"),
        [fallbacks[1], {**fallbacks[0], "name": RICH_AGENT["spec"]["model"]["name"]}],
    )

    (first,) = _problems(AgentManifest, repeats_fallback)
    (second,) = _problems(AgentManifest, repeats_primary)

    assert first == {
        "field": "spec.model",
        "problem": "Value error, fallbacks[2] repeats fallbacks[0]",
    }
    assert second["problem"] == "Value error, fallbacks[1] repeats the primary model"


def test_missing_mcp_env_keys_are_reported_by_position():
    document = with_value(
        RICH_AGENT,
        ("spec", "mcp", "servers", 0, "env_allowlist"),
        ["FILES_ROOT_HINT", "NOPE", "ANTHROPIC_API_KEY", "ALSO_NOPE"],
    )

    (found,) = _problems(AgentManifest, document)

    assert found["problem"] == (
        "Value error, spec.mcp.servers[0].env_allowlist names keys absent from "
        "runtime.env_allowlist (at index 1, 3)"
    )


def test_a_bad_state_field_name_is_named_because_it_is_a_key():
    """state 필드 이름은 매핑의 키다 — 키는 구조라 내보낸다 (정제해서)."""
    fields = {"ok": {"type": "string"}, "_bad\x1b[31m": {"type": "string"}}
    document = with_value(RICH_GRAPH, ("spec", "state", "fields"), fields)
    # 필드를 통째로 바꾸면 압축 정책의 대상이 사라진다 — 여기서 보는 것은 이름 문제 하나다
    document["spec"]["state"].pop("compaction", None)

    (found,) = _problems(GraphTopology, document)

    assert found == {
        "field": "spec.state",
        "problem": (
            "Value error, state field name '_bad?[31m' must be an identifier not starting with '_'"
        ),
    }


def test_repeated_positions_groups_every_repeat():
    assert repeated_positions(["a", "b", "c"]) is None
    assert repeated_positions(["a", "b", "a"]) == "0 and 2"
    assert repeated_positions(["x", "y", "x", "y", "z", "x"]) == "0, 2 and 5; 1 and 3"


# --- 키 자리: 나가되 묶인다 -------------------------------------------------------------------


def test_field_path_and_problem_are_bounded_and_printable():
    """키 이름은 구조라 ``field`` 로 나간다 (의도) — 길이와 문자는 묶는다."""
    long_key = "k" * 10_000
    control_key = "bad\x1b[31mkey\x07\n"
    document = {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": "a", "version": "0.1.0", long_key: 1, control_key: 2},
        "spec": dict.fromkeys((f"{n:03d}" + "x" * 60 for n in range(40)), 1),
    }

    with pytest.raises(ValidationError) as exc_info:
        AgentManifest.model_validate(document)

    problems = field_problems(exc_info.value)
    fields = [p["field"] for p in problems]
    assert "metadata." + "k" * MAX_LABEL_LENGTH + "…" in fields
    assert "metadata.bad?[31mkey??" in fields
    for problem in problems:
        assert len(problem["field"]) <= MAX_FIELD_LENGTH + 1
        assert len(problem["problem"]) <= MAX_PROBLEM_LENGTH + 1
        assert (problem["field"] + problem["problem"]).isprintable()


# --- 3: 입구 ----------------------------------------------------------------------------------

ENTRY_MARK_NAMES = frozenset({"plain", "lower", "control", "nfkc-url"})
"""입구 퍼즈가 쓰는 표식 변형 — 모든 잎 위치는 그대로 두고 변형만 줄인다.

입구 한 번은 파일 쓰기와 yaml 파싱 (순수 파이썬) 이라 변형 전부 (17종 × 입구 2~3개) 를 돌리면 분
단위가 된다. 값의 모양에 따른 차이는 위의 모델 검증 퍼즈가 전부 본다. 여기서 더 보는 것은 스키마
검증 **밖의** 검사와 입구의 배선이므로, 그 검사에 닿는 모양만 남긴다: 그대로의 값, 이름 규칙을
통과하는 값 (스키마가 걸러 주지 않는다), 제어문자 (yaml 왕복), URL 조각.
"""
MIN_ENTRY_MUTATIONS = 1500

MODULE_KINDS: dict[type[BaseModel], tuple[str, Any]] = {
    SkillsetManifest: ("skillsets", SkillsetLoader),
    PromptsetManifest: ("promptsets", PromptsetLoader),
    MemorysetManifest: ("memorysets", MemorysetLoader),
}

Entry = Callable[[], Any]


def _site(label: str, model: type[BaseModel], document: dict[str, Any], root: Path) -> Any:
    """문서 하나를 제 자리에 둔 작은 저장소 — ``(선언 파일, 그 파일을 읽는 입구들)``.

    위치는 원래 문서의 이름 · 버전에서 나온다. 그래서 이름 · 버전 · kind 잎을 바꾼 변이는 스키마
    검증뿐 아니라 "선언과 위치가 다르다" 는 대조 (registry · catalog) 에도 닿는다.
    """
    catalog = Catalog.under(root)
    name = document.get("metadata", {}).get("name", "")
    entries: dict[str, Entry]
    if model is AgentManifest:
        path = root / "agents" / name / "manifest.yaml"
        entries = {
            "catalog.agents": catalog.agents,
            "catalog.agent": lambda: catalog.agent(name),
            "agentd.load_manifest": lambda: load_manifest(path),
        }
    elif model is GroupManifest:
        path = root / "groups" / f"{name}.yaml"
        entries = {"catalog.groups": catalog.groups, "catalog.group": lambda: catalog.group(name)}
    elif model is GraphTopology:
        path = root / "graphs" / f"{name}.yaml"
        entries = {
            "catalog.graphs": catalog.graphs,
            "catalog.graph": lambda: catalog.graph(name),
            "parse_declaration": lambda: parse_declaration(path, GraphTopology),
        }
    elif model is AaestroConfig:
        path = root / "configs" / "dev.yaml"
        entries = {
            "load_config": lambda: load_config("dev", config_dir=root / "configs", environ={})
        }
    else:
        module_type, loader = MODULE_KINDS[model]
        version = document["metadata"]["version"]
        ref = f"{module_type}/{name}@{version}"
        source = REPO_ROOT / label
        path = root / "modules" / module_type / name / version / source.name
        # 스킬 코드 · 템플릿까지 옮긴다 — 스키마를 통과한 변이가 그 다음 단계 (파일 해석) 에 닿게
        shutil.copytree(source.parent, path.parent)
        entries = {
            f"catalog.modules({module_type})": lambda: catalog.modules(module_type),
            "catalog.module": lambda: catalog.module(module_type, name, version),
            f"{loader.__name__}.load": lambda: loader(ModuleRegistry.under(root)).load(ref),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    return path, entries


def _surfaced(entry: Entry) -> Iterator[str]:
    """입구가 밖으로 내놓은 실패 — 던진 에러의 message · details, 목록이 싣는 problems."""
    try:
        result = entry()
    except AaestroError as err:
        yield json.dumps({"m": err.message, "d": err.details}, ensure_ascii=False, default=str)
        return
    for problem in getattr(result, "problems", ()):
        yield json.dumps(asdict(problem), ensure_ascii=False, default=str)


def _entry_documents() -> list[tuple[str, type[BaseModel], dict[str, Any]]]:
    """입구 퍼즈의 문서 — 저장소 선언 (모듈은 실제 파일과 함께) 과 채운 문서."""
    repository = [(label, model, doc) for label, model, doc in fuzz_documents() if ":" not in label]
    return [
        *(entry for entry in repository if not entry[0].startswith("configs/")),
        *RICH_DOCUMENTS,
    ]


def test_no_entry_point_error_carries_a_piece_of_the_input(tmp_path: Path):
    """값 자리의 변이만 본다 — 키 이름은 의도된 노출이다 (위의 길이 · 문자 테스트가 묶는다)."""
    leaks: list[str] = []
    surfaced = total = 0
    seen: set[tuple[str, tuple[str, ...]]] = set()
    entry_marks = [mark for mark in MARKS if mark.name in ENTRY_MARK_NAMES]
    assert len(entry_marks) == len(ENTRY_MARK_NAMES)

    for number, (label, model, document) in enumerate(_entry_documents()):
        path, entries = _site(label, model, document, tmp_path / str(number))
        for position in leaf_positions(document):
            shape = (model.__name__, shape_of(position))
            if shape in seen:
                continue
            seen.add(shape)
            for mark in entry_marks:
                total += 1
                path.write_text(
                    yaml.safe_dump(with_value(document, position, mark.value), allow_unicode=True),
                    encoding="utf-8",
                )
                for entry_name, entry in entries.items():
                    for text in _surfaced(entry):
                        surfaced += 1
                        piece = mark.leaked_in(text)
                        if piece is not None:
                            where = f"{entry_name} {label} {'.'.join(map(str, position))}"
                            leaks.append(f"{where} [{mark.name}] {piece!r}: {text[:200]}")

    assert total >= MIN_ENTRY_MUTATIONS
    assert surfaced > total
    assert leaks[:SHOWN] == [], f"{len(leaks)} entry-point errors echo the input"


def _leak_of(mark: Mark, entry: Entry) -> str | None:
    return next((piece for text in _surfaced(entry) if (piece := mark.leaked_in(text))), None)


@pytest.mark.parametrize("field", ["kind", "name", "version"])
def test_module_integrity_mismatch_names_the_expected_value_only(field: str, tmp_path: Path):
    """무결성 검사는 스키마 검증보다 먼저 돈다 — 선언에 적힌 값이 무엇이든 메시지에 싣지 않는다."""
    label, document = next(
        (str(path.relative_to(REPO_ROOT)), doc)
        for path, doc in _declaration_documents().items()
        if doc["kind"] == "Skillset"
    )
    path, entries = _site(label, SkillsetManifest, document, tmp_path)
    position = ("kind",) if field == "kind" else ("metadata", field)
    path.write_text(yaml.safe_dump(with_value(document, position, MARKER)), encoding="utf-8")

    with pytest.raises(AaestroError) as exc_info:
        entries["SkillsetLoader.load"]()

    assert exc_info.value.code == "MOD_003"
    assert f"{field} mismatch" in exc_info.value.message
    assert _leak_of(MARKS[0], entries["SkillsetLoader.load"]) is None
    assert _leak_of(MARKS[0], entries["catalog.modules(skillsets)"]) is None
    # 기대 값은 말한다 — 무엇으로 고쳐야 하는지는 운영자가 알아야 한다
    expected = {"kind": "Skillset", "name": document["metadata"]["name"]}.get(
        field, document["metadata"]["version"]
    )
    assert expected in exc_info.value.message
