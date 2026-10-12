"""Declared output key tests.

executor 는 늘 `{"content": ...}` 하나만 돌려줬다. 그래서 그래프의
`output_map` 이 `output.content` 밖을 가리킬 수 없었고, 레퍼런스 그래프가
`GRAPH_003` 으로 실패했다 (#146 / #142).
"""

from __future__ import annotations

import json

import pytest

from aaestro.agentd.executor import Executor, ExecutorServices
from aaestro.core.agent import TaskStatus
from aaestro.core.errors import ErrorCode
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, text


async def run_with(content: str, keys=(), *, node_id: str | None = "planner"):
    """키는 **태스크마다** 고른다 — 같은 에이전트가 노드마다 다른 계약을 갖는다."""
    executor = Executor(
        agent="planner",
        model=FakeModel([text(content)]),
        tools=FakeTools(),
        render=lambda _task: "prompt",
        services=ExecutorServices(output_keys=(lambda _task: keys) if keys else None),
    )
    return await executor.execute(make_task(node_id=node_id))


# --- 선언 없음: 기존 동작 ----------------------------------------------------------


async def test_without_declared_keys_the_output_is_content():
    """미선언 에이전트의 동작이 바뀌면 기존 그래프가 전부 깨진다."""
    result = await run_with("prose answer")

    assert result.output == {"content": "prose answer"}


async def test_without_declared_keys_json_is_not_unwrapped():
    """선언하지 않았는데 파싱하면 평문을 쓰던 에이전트가 놀란다."""
    result = await run_with('{"plan": "x"}')

    assert result.output == {"content": '{"plan": "x"}'}


# --- 선언 있음 -----------------------------------------------------------------


async def test_declared_keys_become_the_output():
    """#146 의 핵심 — 이름 있는 키가 state 로 갈 수 있어야 한다."""
    payload = json.dumps({"plan": "step 1", "needs_research": True})

    result = await run_with(payload, ("plan", "needs_research"))

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"plan": "step 1", "needs_research": True}


async def test_undeclared_extra_keys_are_dropped():
    """선언되지 않은 값이 state 로 흘러가면 02 Rule 5 의 출력 규율이 깨진다."""
    payload = json.dumps({"plan": "p", "secret_scratch": "should not leak"})

    result = await run_with(payload, ("plan",))

    assert result.output == {"plan": "p"}


async def test_nested_values_survive():
    payload = json.dumps({"findings": [{"source": "a", "claim": "b"}]})

    result = await run_with(payload, ("findings",))

    assert result.output["findings"] == [{"source": "a", "claim": "b"}]


# --- 계약 위반 -----------------------------------------------------------------


async def test_a_non_json_response_fails_with_llm_004():
    """조용히 빈 출력으로 떨어지면 다음 노드에서야 GRAPH_003 으로 실패한다."""
    result = await run_with("prose, not json", ("plan",))

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_004


@pytest.mark.parametrize("payload", ["[1, 2]", '"a string"', "42", "null"])
async def test_a_json_non_object_fails(payload):
    """배열/스칼라는 키를 가질 수 없다 — 조용히 통과시키면 안 된다."""
    result = await run_with(payload, ("plan",))

    assert result.status is TaskStatus.FAILED
    assert result.error.code == ErrorCode.LLM_004


async def test_a_missing_declared_key_fails():
    """promptset 이 계약과 어긋나면 그 자리에서 드러나야 한다."""
    result = await run_with(json.dumps({"other": 1}), ("plan",))

    assert result.status is TaskStatus.FAILED
    assert result.error.code == ErrorCode.LLM_004


async def test_the_failure_names_the_declared_keys():
    """무엇을 기대했는지 없으면 promptset 드리프트를 추적할 수 없다."""
    result = await run_with("nope", ("plan", "needs_research"))

    assert "plan" in result.error.details["declared"]
    assert "needs_research" in result.error.details["declared"]


# --- 템플릿 선언 ----------------------------------------------------------------


def test_declared_keys_must_be_identifiers():
    """키는 state schema 필드명과 맞물린다."""
    from pydantic import ValidationError

    from aaestro.modules.promptset import TemplateSpec

    with pytest.raises(ValidationError):
        TemplateSpec(file="t.j2", output_keys=("not an identifier",))


def test_declared_keys_must_be_unique():
    from pydantic import ValidationError

    from aaestro.modules.promptset import TemplateSpec

    with pytest.raises(ValidationError):
        TemplateSpec(file="t.j2", output_keys=("plan", "plan"))


def test_no_declaration_means_no_keys():
    from aaestro.modules.promptset import TemplateSpec

    assert TemplateSpec(file="t.j2").output_keys == ()


# --- 노드별로 다른 계약 (#150) ------------------------------------------------------


async def test_the_same_agent_yields_different_keys_per_node():
    """#150 의 핵심 — 에이전트 단위 선언으로는 불가능했다.

    planner 는 세 그래프에서 각각 plan/seen_ids/pending_spaces 를 낸다.
    """
    per_node = {
        "planner": ("plan", "needs_research"),
        "classifier": ("seen_ids",),
    }

    async def run(node_id: str, content: str):
        executor = Executor(
            agent="planner",
            model=FakeModel([text(content)]),
            tools=FakeTools(),
            render=lambda _task: "prompt",
            services=ExecutorServices(
                output_keys=lambda task: per_node.get(task.template_name, ())
            ),
        )
        return await executor.execute(make_task(node_id=node_id))

    planned = await run("planner", json.dumps({"plan": "p", "needs_research": False}))
    classified = await run("classifier", json.dumps({"seen_ids": [1, 2]}))

    assert planned.output == {"plan": "p", "needs_research": False}
    assert classified.output == {"seen_ids": [1, 2]}


async def test_a_direct_request_follows_the_default_template():
    """direct 요청은 node_id 가 없어 default 를 쓴다 (04 Compatibility Rules 4)."""
    seen: list[str] = []

    executor = Executor(
        agent="planner",
        model=FakeModel([text("prose")]),
        tools=FakeTools(),
        render=lambda _task: "prompt",
        services=ExecutorServices(output_keys=lambda task: seen.append(task.template_name) or ()),
    )

    await executor.execute(make_task(node_id=None))

    assert seen == ["default"]


@pytest.fixture
def manifest_declaring_output():
    """실제 manifest — 파일 I/O 는 동기 fixture 에 둔다.

    async 테스트 안에서 파일을 열면 린터가 막고, 실제로도 이벤트 루프에서
    blocking I/O 를 하는 셈이다.
    """
    from pathlib import Path

    import yaml

    from aaestro.core.manifest import AgentManifest

    declared = yaml.safe_load(Path("agents/echo/manifest.yaml").read_text(encoding="utf-8"))
    return AgentManifest.model_validate(declared)


async def test_the_assembled_executor_carries_the_declared_keys(
    monkeypatch, manifest_declaring_output
):
    """헬퍼만 검증하면 **조립부에서 안 넘겨도 통과한다** — 이 세션에서 반복된 함정."""
    from aaestro.agentd.__main__ import build_executor

    monkeypatch.setenv("AAESTRO_ROOT", ".")
    monkeypatch.setenv("AAESTRO_EXECUTOR", "")

    built = await build_executor(manifest_declaring_output)

    # 조립부가 콜러블을 넘겨야 태스크마다 템플릿 선언을 볼 수 있다
    assert callable(built._services.output_keys)


# --- 실제 promptset 을 로드해 키를 고른다 -------------------------------------------


@pytest.fixture
def planner_manifest():
    """레퍼런스 manifest — 실제 promptset 을 가리킨다."""
    from pathlib import Path

    import yaml

    from aaestro.core.manifest import AgentManifest

    declared = Path("agents/planner/manifest.yaml").read_text(encoding="utf-8")
    return AgentManifest.model_validate(yaml.safe_load(declared))


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [
        ("planner", ("plan", "needs_research")),
        ("classifier", ("seen_ids",)),
        ("scanner", ("pending_spaces",)),
        (None, ()),
    ],
)
async def test_keys_come_from_the_loaded_promptset(
    monkeypatch, planner_manifest, node_id, expected
):
    """**로드된** promptset 에서 골라야 한다 — 모양을 잘못 짚으면 AttributeError 다.

    실제로 그랬다: `result.promptset.spec` 로 접근했는데 `LoadedPromptset` 에는
    그 속성이 없다. 이 경로를 태우는 테스트가 없어 컨테이너에서야 드러났다.
    """
    from aaestro.agentd.__main__ import build_executor

    monkeypatch.setenv("AAESTRO_ROOT", ".")
    monkeypatch.setenv("AAESTRO_EXECUTOR", "")

    built = await build_executor(planner_manifest)

    assert tuple(built._services.output_keys(make_task(node_id=node_id))) == expected


# --- 두 경로가 같은 계약을 낸다 (#233) -----------------------------------------


async def stream_with(content: str, keys=(), *, node_id: str | None = "planner"):
    """`execute` 와 **같은 조립**으로 stream 을 돌려 종료 이벤트를 집는다."""
    executor = Executor(
        agent="planner",
        model=FakeModel([text(content)]),
        tools=FakeTools(),
        render=lambda _task: "prompt",
        services=ExecutorServices(output_keys=(lambda _task: keys) if keys else None),
    )
    events = [event async for event in executor.stream(make_task(node_id=node_id))]
    return events[-1]


async def test_the_stream_path_honours_declared_keys():
    """#233 — `_shape_output` 이 execute 경로에서만 불려 계약이 갈렸다.

    같은 에이전트가 같은 태스크를 받는데 `/invoke` 로 부르면 계약된 키로,
    `/stream` 으로 부르면 `{"content": ...}` 로 나왔다.
    """
    payload = json.dumps({"plan": "step 1", "needs_research": True})

    done = await stream_with(payload, ("plan", "needs_research"))

    assert done.output == {"plan": "step 1", "needs_research": True}


async def test_both_paths_agree_on_the_output():
    """엔드포인트가 달라도 output 은 같아야 한다 — 소비자가 갈라지지 않게."""
    payload = json.dumps({"plan": "step 1", "needs_research": False})
    keys = ("plan", "needs_research")

    invoked = await run_with(payload, keys)
    streamed = await stream_with(payload, keys)

    assert invoked.output == streamed.output


async def test_both_paths_agree_without_declared_keys():
    """미선언 경로도 갈리지 않는다 — 기존 동작이 두 경로에서 같아야 한다."""
    invoked = await run_with("prose answer")
    streamed = await stream_with("prose answer")

    assert invoked.output == streamed.output == {"content": "prose answer"}
