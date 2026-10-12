"""Unit tests for the skillset loader."""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError
from aaestro.core.skill import SkillContext
from aaestro.modules.registry import ModuleRegistry, RegistryRoots
from aaestro.modules.skillset import SkillsetLoader
from tests.fixtures.registry import fixture_registry

REF = "skillsets/web-search@0.2.0"


@pytest.fixture
def loader() -> SkillsetLoader:
    return SkillsetLoader(fixture_registry())


def test_load_binds_all_declared_skills(loader):
    skillset = loader.load(REF)

    assert [s.name for s in skillset.skills] == ["search", "fetch_page"]


def test_tool_schema_snapshot(loader):
    """스킬셋이 노출하는 tool 계약 고정 — 변경 시 모델이 보는 계약이 바뀐다."""
    skillset = loader.load(REF)

    assert skillset.get("search").spec.model_dump() == {
        "name": "search",
        "description": "웹 검색을 수행하고 상위 결과를 반환",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "검색 질의"},
                "max_results": {"type": "integer", "description": "최대 결과 개수", "default": 10},
            },
            "required": ["query"],
        },
    }


def test_declaration_description_overrides_docstring(loader):
    skillset = loader.load(REF)

    # search 는 skillset.yaml 에 description 이 있고, fetch_page 는 없다
    assert skillset.get("search").spec.description == "웹 검색을 수행하고 상위 결과를 반환"
    assert skillset.get("fetch_page").spec.description == "URL 의 본문 텍스트를 추출합니다."


def test_timeout_comes_from_declaration(loader):
    skillset = loader.load(REF)

    assert skillset.get("search").timeout_s == 30
    assert skillset.get("fetch_page").timeout_s == 60


def test_required_env_is_exposed(loader):
    skillset = loader.load(REF)

    assert skillset.required_env == ("SEARCH_API_KEY",)


def test_tools_returns_all_specs(loader):
    skillset = loader.load(REF)

    assert {t.name for t in skillset.tools()} == {"search", "fetch_page"}


def test_get_unknown_skill_raises_mod_001(loader):
    skillset = loader.load(REF)

    with pytest.raises(AaestroError) as exc_info:
        skillset.get("nonexistent")

    assert exc_info.value.code == "MOD_001"


async def test_loaded_skill_is_callable(loader):
    skillset = loader.load(REF)
    ctx = SkillContext(agent="researcher", task_id="t", run_id="r")

    results = await skillset.get("search").fn(ctx, "query", 2)

    assert len(results) == 2


def _write_skillset(tmp_path, *, yaml_body: str, modules: dict[str, str] | None = None):
    module = tmp_path / "modules" / "skillsets" / "custom" / "0.1.0"
    (module / "skills").mkdir(parents=True)
    (module / "skills" / "__init__.py").write_text("", encoding="utf-8")
    (module / "skillset.yaml").write_text(yaml_body, encoding="utf-8")
    for name, source in (modules or {}).items():
        (module / "skills" / f"{name}.py").write_text(source, encoding="utf-8")
    return SkillsetLoader(ModuleRegistry.under(tmp_path))


HEADER = """apiVersion: aaestro/v1
kind: Skillset
metadata:
  name: custom
  version: 0.1.0
spec:
  skills:
"""


def test_missing_skill_module_raises_mod_003(tmp_path):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: a\n      entrypoint: skills.absent:a\n",
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "module not found" in exc_info.value.message


def test_missing_entrypoint_function_raises_mod_003(tmp_path):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: a\n      entrypoint: skills.mod:absent\n",
        modules={"mod": "value = 1\n"},
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "entrypoint not found" in exc_info.value.message


def test_sync_skill_raises_mod_003(tmp_path):
    """모든 skill 은 async — 동기 함수는 로드 단계에서 걸러진다."""
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: a\n      entrypoint: skills.mod:a\n",
        modules={"mod": "def a(ctx, q: str) -> str:\n    '''동기.'''\n    return q\n"},
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_module_import_failure_raises_mod_003(tmp_path):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: a\n      entrypoint: skills.mod:a\n",
        modules={"mod": "raise RuntimeError('boom')\n"},
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"
    assert "import failed" in exc_info.value.message


def test_undecorated_async_function_still_gets_schema(tmp_path):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: a\n      entrypoint: skills.mod:a\n",
        modules={"mod": "async def a(ctx, q: str) -> str:\n    '''설명.'''\n    return q\n"},
    )

    skillset = loader.load("skillsets/custom@0.1.0")

    assert skillset.get("a").spec.parameters["required"] == ["q"]


def test_declared_name_overrides_function_name(tmp_path):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: renamed\n      entrypoint: skills.mod:original\n",
        modules={
            "mod": (
                "from aaestro.core.skill import SkillContext, skill\n\n\n"
                "@skill\nasync def original(ctx: SkillContext, q: str) -> str:\n"
                "    '''설명.'''\n    return q\n"
            )
        },
    )

    skillset = loader.load("skillsets/custom@0.1.0")

    assert skillset.get("renamed").spec.name == "renamed"


def test_duplicate_skill_names_raise_mod_003(tmp_path):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER
        + "    - name: a\n      entrypoint: skills.mod:a\n"
        + "    - name: a\n      entrypoint: skills.mod:a\n",
        modules={"mod": "async def a(ctx) -> None:\n    '''설명.'''\n"},
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_empty_skillset_raises_mod_003(tmp_path):
    loader = _write_skillset(tmp_path, yaml_body=HEADER + "    []\n")

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


@pytest.mark.parametrize("entrypoint", ["skills.mod", "skills.mod:a:b", ":a", "skills.mod:"])
def test_malformed_entrypoint_raises_mod_003(tmp_path, entrypoint):
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + f"    - name: a\n      entrypoint: '{entrypoint}'\n",
    )

    with pytest.raises(AaestroError) as exc_info:
        loader.load("skillsets/custom@0.1.0")

    assert exc_info.value.code == "MOD_003"


def test_skillset_supports_intra_package_imports(tmp_path):
    """스킬셋 내부의 상대 import 가 동작해야 한다.

    중간 패키지를 등록하지 않으면 `from .util import ...` 같은 평범한 구조의
    스킬셋이 통째로 로드되지 않는다.
    """
    root = tmp_path / "web-search" / "0.1.0"
    (root / "skills").mkdir(parents=True)
    (root / "skillset.yaml").write_text(
        "apiVersion: aaestro/v1\n"
        "kind: Skillset\n"
        "metadata: {name: web-search, version: 0.1.0}\n"
        "spec:\n"
        "  skills:\n"
        "    - name: search\n"
        "      entrypoint: skills.search:search\n"
        "      description: d\n"
    )
    (root / "skills" / "__init__.py").write_text("")
    (root / "skills" / "util.py").write_text("HELPER = 'ok'\n")
    (root / "skills" / "search.py").write_text(
        "from aaestro.core.skill import SkillContext, skill\n"
        "from .util import HELPER\n"
        "\n"
        "@skill\n"
        "async def search(ctx: SkillContext, q: str) -> str:\n"
        '    """s."""\n'
        "    return HELPER\n"
    )
    roots = RegistryRoots(
        skillsets=tmp_path,
        promptsets=tmp_path,
        memorysets=tmp_path,
        agents=tmp_path,
        graphs=tmp_path,
        groups=tmp_path,
    )

    loaded = SkillsetLoader(ModuleRegistry(roots)).load("skillsets/web-search@0.1.0")

    assert [s.name for s in loaded.skills] == ["search"]


# --- 타입 없는 파라미터 리포트 ---------------------------------------------------


def test_reference_skillset_has_no_untyped_parameters(tmp_path):
    """배포되는 스킬셋이 타입 없는 tool 을 모델에 노출하면 안 된다."""
    from pathlib import Path

    from aaestro.modules.registry import ModuleRegistry

    repo_root = Path(__file__).resolve().parents[3]
    loaded = SkillsetLoader(ModuleRegistry.under(repo_root)).load("skillsets/web-search@0.2.0")

    assert loaded.untyped_parameters() == {}


def _captured_loader_warnings(monkeypatch) -> list[dict]:
    """skillset 로더의 WARN 을 가로챈다."""
    recorded: list[dict] = []

    def capture(event: str, **fields: object) -> None:
        recorded.append({"event": event, **fields})

    monkeypatch.setattr("aaestro.modules.skillset.log.warning", capture)
    return recorded


def test_loader_warns_with_the_skillset_ref(tmp_path, monkeypatch):
    """어느 skillset 에서 왔는지는 로더만 안다 — 데코레이터 시점엔 ref 가 없다."""
    recorded = _captured_loader_warnings(monkeypatch)
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: loose\n      entrypoint: skills.loose:loose\n",
        modules={
            "loose": (
                "from aaestro.core.skill import SkillContext, skill\n"
                "@skill\n"
                "async def loose(ctx: SkillContext, thing, count: int = 1) -> str:\n"
                '    """설명."""\n'
                "    return 'x'\n"
            )
        },
    )

    loader.load("skillsets/custom@0.1.0")

    warned = [r for r in recorded if "have no type" in r["event"]]
    assert warned, "로더가 타입 없는 파라미터를 경고하지 않았다"
    assert warned[0]["skillset"] == "skillsets/custom@0.1.0"
    assert warned[0]["tool"] == "loose"
    assert warned[0]["parameters"] == ["thing"]


def test_loader_stays_quiet_when_every_parameter_is_typed(tmp_path, monkeypatch):
    """오탐이 나면 운영자가 이 경고를 무시하게 된다."""
    recorded = _captured_loader_warnings(monkeypatch)
    loader = _write_skillset(
        tmp_path,
        yaml_body=HEADER + "    - name: tidy\n      entrypoint: skills.tidy:tidy\n",
        modules={
            "tidy": (
                "from aaestro.core.skill import SkillContext, skill\n"
                "@skill\n"
                "async def tidy(ctx: SkillContext, query: str) -> str:\n"
                '    """설명."""\n'
                "    return 'x'\n"
            )
        },
    )

    loader.load("skillsets/custom@0.1.0")

    assert [r for r in recorded if "have no type" in r["event"]] == []


# --- 리로드: 고친 스킬 코드가 다시 실행되는가 (#274 리뷰) --------------------------------


def _versioned_skill(value: str) -> str:
    return (
        "from aaestro.core.skill import SkillContext, skill\n"
        "from .helper import SUFFIX\n"
        "@skill\n"
        "async def answer(ctx: SkillContext) -> str:\n"
        '    """설명."""\n'
        f"    return '{value}' + SUFFIX\n"
    )


async def test_a_new_generation_executes_changed_skill_code(tmp_path):
    """같은 세대면 import 캐시가 옛 함수를 준다 — 리로드가 세대를 올리는 이유."""
    body = HEADER + "    - name: answer\n      entrypoint: skills.answer:answer\n"
    loader = _write_skillset(
        tmp_path,
        yaml_body=body,
        modules={"answer": _versioned_skill("v1"), "helper": "SUFFIX = '-a'\n"},
    )
    ctx = SkillContext(agent="a", task_id="t", run_id="r")
    first = loader.load("skillsets/custom@0.1.0").get("answer").fn

    skills = tmp_path / "modules" / "skillsets" / "custom" / "0.1.0" / "skills"
    (skills / "answer.py").write_text(_versioned_skill("v2"), encoding="utf-8")
    (skills / "helper.py").write_text("SUFFIX = '-b'\n", encoding="utf-8")
    registry = ModuleRegistry.under(tmp_path)
    same_generation = SkillsetLoader(registry).load("skillsets/custom@0.1.0").get("answer").fn
    next_generation = (
        SkillsetLoader(registry, generation=1).load("skillsets/custom@0.1.0").get("answer").fn
    )

    assert await first(ctx) == "v1-a"
    assert await same_generation(ctx) == "v1-a", "같은 세대는 캐시를 공유한다"
    assert await next_generation(ctx) == "v2-b", "새 세대는 패키지 하위 모듈까지 다시 실행한다"
    assert await first(ctx) == "v1-a", "옛 함수는 진행 중 태스크를 위해 그대로 남는다"
