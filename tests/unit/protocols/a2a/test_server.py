"""A2A inbound task body and identity tests (#400).

``task_id`` 는 **보낸 쪽이 고른다** — 프레임워크는 그 값이 추측 불가능하다고 가정하지 않는다.
그래서 수신 입구는 (1) peer 본문의 식별자를 형식으로 거르고, 거절할 때 받은 값을 되비추지 않으며
(2) 호출자를 태스크에 실어 실행기의 태스크별 상태가 다른 공간의 것과 섞이지 않게 한다.

실행기는 진짜 ``Executor`` 를 물린다 — 수신 입구가 호출자를 싣는 배선과 실행기가 그것으로
키를 잡는 배선이 **함께** 서야 분리가 성립한다. 한쪽만 보면 다른 쪽을 지워도 통과한다.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from structlog.testing import capture_logs

from aaestro.agentd.executor import Executor
from aaestro.core.agent import TaskKey, TaskRequest, TraceContext
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.core.skill import SkillContext
from aaestro.orchestrator.run import RESUMED_SUFFIX
from aaestro.protocols.a2a.allowlist import Edge, issue_token
from aaestro.protocols.a2a.sdk import CALLER_HEADER, SdkPeerTransport, build_message
from aaestro.protocols.a2a.server import (
    MAX_IDENTIFIER_CHARS,
    VERIFIED_CALLER_STATE,
    GuardedExecutor,
    read_task,
)
from aaestro.protocols.a2a.tool import delegation_id
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, text
from tests.unit.protocols.a2a.test_sdk import (
    BASE_URL,
    CALLEE,
    CALLER,
    SECRET,
    _WiredTransport,
    guard_for,
    serve,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
OTHER_PEER = "writer"
SHARED_ID = "task-shared"

MARK = "LEAK-MARKER-9c41"
"""거절된 값에 심는 표식 — 출력 가능한 조각이라 이스케이프돼도 그대로 남는다."""

IDENTIFIER_FIELDS = [
    "task_id",
    "run_id",
    "node_id",
    "trace.trace_id",
    "trace.span_id",
    "trace.graph",
]
"""peer 본문이 싣는 식별자 전부 — 하나만 검증하면 나머지로 같은 값이 들어온다."""


def body(**overrides: Any) -> str:
    """peer 가 보내는 본문 — ``sdk.build_message`` 가 싣는 모양."""
    payload = {
        "task_id": "task-0001",
        "run_id": "run-0001",
        "node_id": None,
        "input": {},
        "trace": {"trace_id": "trace-0001", "span_id": None, "depth": 0, "graph": None},
        **overrides,
    }
    return json.dumps(payload, ensure_ascii=False)


def body_with(field: str, value: Any) -> str:
    """``field`` (점 표기) 하나만 바꾼 본문."""
    if not field.startswith("trace."):
        return body(**{field: value})
    trace = {"trace_id": "trace-0001", "span_id": None, "depth": 0, "graph": None}
    trace[field.removeprefix("trace.")] = value
    return body(trace=trace)


def value_of(task: TaskRequest, field: str) -> Any:
    target: Any = task
    for part in field.split("."):
        target = getattr(target, part)
    return target


def leaves(value: Any) -> Iterator[Any]:
    """구조의 모든 잎 값 (키 포함) — 직렬화하기 **전에** 본다."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from leaves(key)
            yield from leaves(item)
    elif isinstance(value, list | tuple | set):
        for item in value:
            yield from leaves(item)
    else:
        yield value


def assert_not_reflected(sent: Any, *carriers: Any) -> None:
    """받은 값도, 그 8자 이상 조각도 어디에도 실리지 않았다.

    ``json.dumps`` 한 문자열에서 찾으면 제어 문자가 이스케이프돼 값이 실려도 통과한다 — 그래서
    직렬화 전 구조의 잎 값과 원문을 비교한다.
    """
    sent_texts = [leaf for leaf in leaves(sent) if isinstance(leaf, str) and len(leaf) >= 8]
    assert sent_texts, "표식이 든 값으로 시험해야 한다"
    chunks = {raw[i : i + 8] for raw in sent_texts for i in range(len(raw) - 7)}
    carried = [leaf for carrier in carriers for leaf in leaves(carrier) if isinstance(leaf, str)]
    for leaf in carried:
        assert not any(chunk in leaf for chunk in chunks), f"received value reflected in: {leaf!r}"


def carriers_of(error: AaestroError) -> tuple[Any, ...]:
    return (error.message, str(error), error.details, error.payload().model_dump())


def rejected(text_: str) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        read_task(text_)
    error = exc_info.value
    assert error.category is ErrorCategory.VALIDATION
    return error


# --- 식별자 형식: 받아들이는 쪽 -----------------------------------------------------------


def framework_ids() -> list[str]:
    """프레임워크가 실제로 만드는 식별자 — 생성 코드를 그대로 부르거나 그 형식을 따른다."""
    ctx = SkillContext(agent=CALLER, task_id=str(uuid.uuid4()), run_id="run-0001")
    return [
        str(uuid.uuid4()),  # orchestrator/builder.py: 노드 태스크 id
        delegation_id(ctx, CALLEE, "무엇부터?"),  # protocols/a2a/tool.py: 위임 id (uuid5)
        f"run-{uuid.uuid4().hex[:12]}",  # orchestrator/submit.py · runs.py
        f"run-{uuid.uuid4()}",  # orchestrator/run.py
        f"svc-{uuid.uuid4().hex[:12]}",  # orchestrator/submit.py: service run
        f"run-{uuid.uuid4().hex[:12]}{RESUMED_SUFFIX}",  # orchestrator/submit.py: 재개
        "run-unknown",  # orchestrator/builder.py: 예약 채널이 빈 state
        f"direct-{uuid.uuid4().hex[:12]}",  # 02 Direct Requests 가 정한 접두사
        "unknown",  # read_task 가 trace 없는 본문에 주는 trace id
    ]


def declared_names() -> list[str]:
    """저장소가 선언한 그래프 이름과 노드 id — ``trace.graph`` 와 ``node_id`` 로 실려 온다."""
    names: set[str] = set()
    for path in sorted((REPO_ROOT / "graphs").glob("*.yaml")):
        graph = yaml.safe_load(path.read_text(encoding="utf-8"))
        names.add(graph["metadata"]["name"])
        names.update(node["id"] for node in graph["spec"]["nodes"])
    assert names, "선언된 그래프를 찾지 못했다 — 이 테스트가 조용히 비면 안 된다"
    return sorted(names)


@pytest.mark.parametrize("field", IDENTIFIER_FIELDS)
def test_read_task_keeps_identifiers_the_framework_generates(field):
    """과잉 거절도 결함이다 — 프레임워크가 만든 값이 걸리면 정상 위임이 전부 막힌다."""
    for value in [*framework_ids(), *declared_names()]:
        assert value_of(read_task(body_with(field, value)), field) == value


@pytest.mark.parametrize(
    "value",
    ["작업 1", "x" * MAX_IDENTIFIER_CHARS, "task-\ufe0f"],
    ids=["unicode-with-inner-space", "at-the-limit", "invisible-but-printable"],
)
@pytest.mark.parametrize("field", IDENTIFIER_FIELDS)
def test_read_task_keeps_well_formed_identifiers_as_sent(field, value):
    assert value_of(read_task(body_with(field, value)), field) == value


def test_read_task_restores_a_delegated_task_from_the_real_wire_format():
    """보내는 쪽 코드가 만든 본문이 그대로 통과해야 한다 — 손으로 쓴 본문만 보면 둘이 어긋난다."""
    sent = make_task(
        task_id=str(uuid.uuid4()),
        node_id=None,
        trace=TraceContext(trace_id="run-0001", graph="research-pipeline").child(span_id="span"),
    )

    restored = read_task(build_message(sent).parts[0].text)

    assert (restored.task_id, restored.run_id, restored.node_id) == (sent.task_id, "run-0001", None)
    assert restored.trace == sent.trace


def test_read_task_without_a_trace_starts_an_unknown_one():
    restored = read_task(json.dumps({"task_id": "task-0001", "run_id": "run-0001"}))

    assert restored.trace == TraceContext(trace_id="unknown")
    assert restored.input == {}


def test_read_task_ignores_a_caller_claimed_in_the_body():
    """본문에 적어 보낸 이름은 신원이 아니다 — 키에 들어가는 호출자는 수신 입구가 싣는다."""
    task = read_task(body(caller="planner"))

    assert task.caller is None


# --- 식별자 형식: 거절하는 쪽 -------------------------------------------------------------

MALFORMED = [
    ("", "empty"),
    (MARK * 9, "too_long"),
    (f"{MARK}\nINFO forged log line", "unprintable_characters"),
    (f"{MARK}\x00tail", "unprintable_characters"),
    (f"{MARK}\x1b[2J", "unprintable_characters"),
    (f"{MARK}\u200bhidden", "unprintable_characters"),
    (f" {MARK} ", "surrounding_whitespace"),
    (f"{MARK}\u3000", "unprintable_characters"),
    ("   ", "surrounding_whitespace"),
    (12345678, "not_string"),
    ({"nested": MARK}, "not_string"),
    ([MARK], "not_string"),
]
MALFORMED_IDS = [
    "empty",
    "too-long",
    "newline",
    "nul",
    "escape",
    "zero-width",
    "padded",
    "ideographic-space",
    "only-spaces",
    "number",
    "object",
    "list",
]


@pytest.mark.parametrize(("value", "reason"), MALFORMED, ids=MALFORMED_IDS)
@pytest.mark.parametrize("field", IDENTIFIER_FIELDS)
def test_read_task_malformed_identifier_raises_val_002_naming_only_the_field(field, value, reason):
    error = rejected(body_with(field, value))

    assert error.code == ErrorCode.VAL_002
    assert error.details == {"field": field, "reason": reason}
    if not any(MARK in leaf for leaf in leaves(value) if isinstance(leaf, str)):
        return  # 표식 없는 값(빈 값 · 공백 · 숫자)은 되비칠 조각이 없다
    assert_not_reflected(value, *carriers_of(error))


@pytest.mark.parametrize("field", ["task_id", "run_id"])
def test_read_task_missing_required_identifier_raises_val_001(field):
    payload = json.loads(body())
    del payload[field]

    error = rejected(json.dumps(payload))

    assert error.code == ErrorCode.VAL_001
    assert error.details == {"field": field, "reason": "missing"}


def test_read_task_trace_without_a_trace_id_raises_val_001():
    error = rejected(body(trace={"depth": 1}))

    assert error.code == ErrorCode.VAL_001
    assert error.details == {"field": "trace.trace_id", "reason": "missing"}


@pytest.mark.parametrize(
    ("sent", "field", "reason"),
    [
        (f"{{{MARK} this is not json", "body", "not_json"),
        (json.dumps([MARK, "a list"]), "body", "not_object"),
        (json.dumps(f"{MARK} a bare string"), "body", "not_object"),
        (body(input=[MARK]), "input", "not_object"),
        (body(input=f"{MARK} text"), "input", "not_object"),
        (body(trace=[MARK]), "trace", "not_object"),
        (body(trace=f"{MARK} text"), "trace", "not_object"),
        (body_with("trace.depth", f"{MARK}-3"), "trace.depth", "not_integer"),
        (body_with("trace.depth", {"deep": MARK}), "trace.depth", "not_integer"),
    ],
    ids=[
        "not-json",
        "json-list",
        "json-string",
        "input-list",
        "input-string",
        "trace-list",
        "trace-string",
        "depth-string",
        "depth-object",
    ],
)
def test_read_task_malformed_body_raises_val_002_without_echoing_it(sent, field, reason):
    """모델 검증 메시지(``input_value=...``)나 파서 오류를 그대로 내보내면 받은 값이 실린다."""
    error = rejected(sent)

    assert error.code == ErrorCode.VAL_002
    assert error.details == {"field": field, "reason": reason}
    assert_not_reflected(MARK, *carriers_of(error))
    # 원인 체인으로도 나가지 않는다 — JSONDecodeError 는 본문 원문을 쥐고 있다
    assert error.__cause__ is None
    assert error.__context__ is None or error.__suppress_context__


@pytest.mark.parametrize(("depth", "reason"), [(True, "not_integer"), (-1, "negative")])
def test_read_task_depth_that_would_dodge_the_limit_is_refused(depth, reason):
    """음수 깊이는 수신 측 상한(A2A_005)을 그 크기만큼 미룬다."""
    error = rejected(body_with("trace.depth", depth))

    assert error.details == {"field": "trace.depth", "reason": reason}


# --- 거절 로그 -------------------------------------------------------------------------


def inbound(text_: str, **state: Any) -> SimpleNamespace:
    """SDK 가 실행기에 넘기는 컨텍스트의 대역 — 본문과 호출 상태만 있다."""
    return SimpleNamespace(
        get_user_input=lambda: text_,
        call_context=SimpleNamespace(state=state),
        task_id="sdk-task",
        context_id="sdk-context",
    )


async def refuse(context: SimpleNamespace) -> tuple[AaestroError, dict[str, Any]]:
    async def handler(task):  # pragma: no cover - 도달하면 안 된다
        raise AssertionError("a malformed body reached the agent")

    executor = GuardedExecutor(guard_for((CALLER, CALLEE)), handler)
    with capture_logs() as logs, pytest.raises(AaestroError) as exc_info:
        await executor.execute(context, None)
    [entry] = [e for e in logs if e["event"] == "a2a inbound task rejected"]
    return exc_info.value, entry


async def test_a_rejected_body_is_logged_as_a_warning_naming_only_the_field():
    """운영자는 거절을 봐야 한다 — 그러나 거절한 값이 로그에 실리면 거절한 이유가 실현된다."""
    forged = f"{MARK}\nINFO forged log line\x00"

    _, entry = await refuse(inbound(body(run_id=forged)))

    assert entry["log_level"] == "warning"
    assert (entry["a2a_callee"], entry["error_code"]) == (CALLEE, ErrorCode.VAL_002)
    assert (entry["field"], entry["reason"]) == ("run_id", "unprintable_characters")
    assert_not_reflected(forged, entry)


async def test_a_rejected_body_is_not_logged_under_an_unverified_header_claim():
    """토큰 모드는 본문을 읽는 시점에 호출자를 확인하기 전이다 — 헤더의 주장을 적으면 남의 이름으로
    로그가 오염된다."""
    claim = f"{MARK}-claimed-name"

    _, entry = await refuse(inbound(body(task_id=""), headers={CALLER_HEADER: claim}))

    assert "a2a_caller" not in entry
    assert_not_reflected(claim, entry)


async def test_a_rejected_body_is_logged_under_the_caller_the_entrance_verified():
    """레지스트리 모드는 입구(``admit``)가 본문보다 먼저 표의 주인을 확인해 둔다."""
    state = {VERIFIED_CALLER_STATE: CALLER, "headers": {CALLER_HEADER: f"{MARK}-claimed-name"}}

    _, entry = await refuse(inbound(body(task_id=""), **state))

    assert entry["a2a_caller"] == CALLER


# --- 실제 SDK 경로: 호출자별 키 공간 --------------------------------------------------------


def executor_answering(*answers: str) -> tuple[Executor, FakeModel]:
    """수신 에이전트의 진짜 실행기 — 모델만 대역이다."""
    model = FakeModel([text(answer) for answer in answers])
    executor = Executor(
        agent=CALLEE,
        model=model,
        tools=FakeTools(),
        render=lambda task: str(task.input),
    )
    return executor, model


def transport_of(caller: str, app) -> SdkPeerTransport:
    """``caller`` 로서 그 앱에 말하는 transport."""
    import httpx

    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL)
    return _WiredTransport(agent=caller, addresses={CALLEE: BASE_URL}, http=http)


async def send(transport: SdkPeerTransport, caller: str, task: TaskRequest):
    return await transport.send(
        callee=CALLEE,
        task=task,
        token=issue_token(SECRET, Edge(caller=caller, callee=CALLEE)),
        headers={},
    )


def shared(query: str) -> TaskRequest:
    return make_task(task_id=SHARED_ID, input={"query": query})


async def test_a_peer_sending_a_node_tasks_id_does_not_get_the_node_tasks_result():
    """#400 — 같은 id 를 보냈다고 Control API 로 온 노드 태스크의 결과를 받으면 안 된다."""
    executor, model = executor_answering("the node's result", "the peer's own result")
    app = serve(guard_for((CALLER, CALLEE)), executor.execute)
    # 오케스트레이터가 Control API 로 보낸 노드 태스크 — 호출자가 없는 공간이다
    await executor.execute(shared("node input"))

    result = await send(transport_of(CALLER, app), CALLER, shared("peer input"))

    assert result.output == {"content": "the peer's own result"}
    assert model.turns == 2
    opening, _ = model.calls[1]
    assert "peer input" in opening, "peer 의 태스크가 자기 입력으로 실행되지 않았다"


async def test_a_peer_sending_another_peers_id_does_not_get_that_peers_result():
    executor, model = executor_answering("for the first peer", "for the second peer")
    app = serve(guard_for((CALLER, CALLEE), (OTHER_PEER, CALLEE)), executor.execute)
    await send(transport_of(OTHER_PEER, app), OTHER_PEER, shared("first peer input"))

    result = await send(transport_of(CALLER, app), CALLER, shared("second peer input"))

    assert result.output == {"content": "for the second peer"}
    assert model.turns == 2


async def test_an_id_a_peer_finished_first_does_not_answer_the_node_task_or_its_retry():
    """peer 가 먼저 심은 결과가 노드 태스크로 돌아가면 graph state 에 peer 의 출력이 병합된다."""
    executor, model = executor_answering("planted by the peer", "the node's own result")
    app = serve(guard_for((CALLER, CALLEE)), executor.execute)
    await send(transport_of(CALLER, app), CALLER, shared("peer input"))

    first = await executor.execute(shared("node input"))
    retried = await executor.execute(shared("node input"))

    assert first.output == {"content": "the node's own result"}
    assert retried == first
    assert model.turns == 2


async def test_the_same_peer_retrying_its_task_id_is_still_idempotent():
    """A2A 재시도는 같은 id 로 온다 (05 Rules 5) — 호출자가 같으면 한 번만 실행한다."""
    executor, model = executor_answering("once", "twice")
    app = serve(guard_for((CALLER, CALLEE)), executor.execute)
    transport = transport_of(CALLER, app)

    first = await send(transport, CALLER, shared("peer input"))
    retried = await send(transport, CALLER, shared("peer input"))

    assert first.output == retried.output == {"content": "once"}
    assert model.turns == 1


async def test_the_executor_sees_the_peers_own_id_keyed_by_the_verified_caller():
    """밖으로 보이는 id 는 peer 가 준 그대로다 — 갈리는 것은 키뿐이고 호출자는 입구가 싣는다."""
    seen: list[TaskRequest] = []
    executor, _ = executor_answering("done")

    async def recording(task: TaskRequest):
        seen.append(task)
        return await executor.execute(task)

    app = serve(guard_for((CALLER, CALLEE)), recording)

    result = await send(transport_of(CALLER, app), CALLER, shared("peer input"))

    [task] = seen
    assert task.task_id == SHARED_ID
    assert task.key == TaskKey(caller=CALLER, task_id=SHARED_ID)
    assert result.task_id == SHARED_ID


@pytest.mark.parametrize("field", ["task_id", "run_id", "node_id"])
async def test_a_malformed_identifier_is_refused_on_the_real_sdk_path_before_the_agent_runs(field):
    """거절이 호출자에게 닿을 때도 받은 값은 돌아가지 않는다 — 사유의 코드만 간다."""

    async def handler(task):  # pragma: no cover - 도달하면 안 된다
        raise AssertionError("a malformed identifier reached the agent")

    forged = f"{MARK}\nINFO forged log line"
    app = serve(guard_for((CALLER, CALLEE)), handler)

    with pytest.raises(AaestroError) as exc_info:
        await send(transport_of(CALLER, app), CALLER, make_task(**{field: forged}))

    error = exc_info.value
    assert ErrorCode.VAL_002 in str(error.details["reason"])
    assert_not_reflected(forged, *carriers_of(error))
