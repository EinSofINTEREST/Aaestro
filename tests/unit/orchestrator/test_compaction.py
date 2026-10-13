"""Unit tests for state compaction policies and their pure strategies.

선언은 전략별로 닫혀 있어야 하고 (모르는 키 · 전략에 맞지 않는 키 거절), 결정적 전략은
모델 없이 같은 입력에 같은 결과를 내야 한다.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from aaestro.artifacts.store import validate_key
from aaestro.core.topology import GraphTopology, StateSpec
from aaestro.memory.recall import CHARS_PER_TOKEN
from aaestro.orchestrator.compaction import (
    SUMMARY_ITEM_MAX_CHARS,
    SUMMARY_PROMPT_MAX_CHARS,
    DedupePolicy,
    KeepFirstPolicy,
    KeepLastPolicy,
    SummarizePolicy,
    apply_deterministic,
    dedupe,
    estimate_tokens,
    has_summary_shape,
    split_for_summary,
    summary_entry,
    summary_items,
)
from aaestro.orchestrator.compaction_schema import compaction_problems, list_fields
from aaestro.orchestrator.state_artifacts import (
    STATE_ARTIFACT_SCOPE,
    artifact_key,
    decode_originals,
    encode_originals,
    field_key_prefix,
    is_own_reference,
)
from tests.fixtures.topologies import service_dict

SUMMARIZE = {
    "strategy": "summarize",
    "over_tokens": 100,
    "keep_last": 2,
    "compactor": "agents/writer@0.4.0",
    "template": "compactor",
}


def state_spec(compaction: dict, **fields: dict) -> StateSpec:
    declared = fields or {"items": {"type": "array"}, "count": {"type": "integer"}}
    return StateSpec.model_validate({"fields": declared, "compaction": compaction})


# --- 선언 --------------------------------------------------------------------


def test_every_strategy_parses_into_its_own_policy():
    spec = state_spec(
        {
            "a": {"strategy": "keep_last", "n": 500},
            "b": {"strategy": "keep_first", "n": 3},
            "c": {"strategy": "dedupe"},
            "d": SUMMARIZE,
        },
        a={"type": "array"},
        b={"type": "array"},
        c={"type": "array"},
        d={"type": "array"},
    )

    assert isinstance(spec.compaction["a"], KeepLastPolicy)
    assert isinstance(spec.compaction["b"], KeepFirstPolicy)
    assert isinstance(spec.compaction["c"], DedupePolicy)
    assert isinstance(spec.compaction["d"], SummarizePolicy)
    assert spec.compaction["a"].n == 500


def test_a_state_without_compaction_declares_none():
    assert state_spec({}).compaction == {}


@pytest.mark.parametrize(
    "policy",
    [
        {"strategy": "truncate", "n": 3},  # 모르는 전략
        {"strategy": "keep_last"},  # 필수 키 누락
        {"strategy": "keep_last", "n": 3, "over_tokens": 10},  # 전략에 맞지 않는 키
        {"strategy": "dedupe", "n": 3},
        {"strategy": "keep_last", "n": 3, "unknown": True},  # 모르는 키
        {"strategy": "keep_last", "n": 0},
        {"strategy": "keep_first", "n": -1},
        {**SUMMARIZE, "over_tokens": 0},
        {**SUMMARIZE, "keep_last": 0},
        {**SUMMARIZE, "n": 3},
        {**SUMMARIZE, "compactor": "writer"},  # 에이전트 ref 가 아니다
        {**SUMMARIZE, "compactor": "skillsets/writer@0.4.0"},
        {**SUMMARIZE, "template": ""},
        # 수치 키는 정수만 — True 는 1 이 되어 사실상 매 경계 요약이 된다
        {"strategy": "keep_last", "n": True},
        {"strategy": "keep_last", "n": "5"},
        {"strategy": "keep_first", "n": 5.0},
        {**SUMMARIZE, "over_tokens": True},
        {**SUMMARIZE, "keep_last": "3"},
        {k: v for k, v in SUMMARIZE.items() if k != "compactor"},
    ],
)
def test_a_malformed_policy_is_rejected(policy):
    with pytest.raises(ValidationError):
        state_spec({"items": policy})


def test_inline_fields_reject_an_undeclared_target():
    with pytest.raises(ValidationError, match="undeclared state field"):
        state_spec({"missing": {"strategy": "dedupe"}})


def test_inline_fields_reject_a_non_array_target():
    with pytest.raises(ValidationError, match="non-array state field"):
        state_spec({"count": {"strategy": "dedupe"}})


def test_the_declaration_rides_on_the_graph_topology():
    topology = GraphTopology.model_validate(
        service_dict(
            state={
                "schema": "aaestro.graphs.schemas:FeedMonitorState",
                "compaction": {"seen_ids": {"strategy": "keep_last", "n": 500}},
            }
        )
    )

    assert topology.spec.state.compaction["seen_ids"].strategy == "keep_last"


# --- 스키마 대조 --------------------------------------------------------------


class Schema(BaseModel):
    names: list[str] = []
    notes: list = []
    maybe: list[dict] | None = None
    count: int = 0


def policies(compaction: dict) -> dict:
    return dict(state_spec(compaction, **dict.fromkeys(compaction, {"type": "array"})).compaction)


def test_list_fields_include_optional_lists_only():
    assert list_fields(Schema) == {"names", "notes", "maybe"}


def test_a_sound_declaration_has_no_problems():
    declared = policies({"names": {"strategy": "dedupe"}, "notes": SUMMARIZE, "maybe": SUMMARIZE})

    assert compaction_problems(declared, Schema) == []


def test_an_undeclared_schema_field_is_a_problem():
    problems = compaction_problems(policies({"nope": {"strategy": "dedupe"}}), Schema)

    assert problems == ["compaction targets an undeclared state field: nope"]


def test_a_non_list_schema_field_is_a_problem():
    problems = compaction_problems(policies({"count": {"strategy": "dedupe"}}), Schema)

    assert problems == ["compaction targets a non-array state field: count"]


def test_summarize_on_a_string_list_is_a_problem():
    """요약 항목은 객체다 — list[str] 에 넣으면 다음 state 검증이 깨진다."""
    problems = compaction_problems(policies({"names": SUMMARIZE}), Schema)

    assert problems == ["summarize needs a list field whose items may be objects: names"]


# --- 결정적 전략 --------------------------------------------------------------


def keep_last(n: int) -> KeepLastPolicy:
    return KeepLastPolicy(strategy="keep_last", n=n)


def test_keep_last_keeps_the_most_recent_in_order():
    assert apply_deterministic(keep_last(3), [1, 2, 3, 4, 5]) == [3, 4, 5]


def test_keep_last_leaves_a_short_list_alone():
    assert apply_deterministic(keep_last(3), [1, 2]) == [1, 2]


def test_keep_first_keeps_the_head():
    policy = KeepFirstPolicy(strategy="keep_first", n=2)

    assert apply_deterministic(policy, ["a", "b", "c"]) == ["a", "b"]


def test_dedupe_keeps_first_occurrence_order():
    assert dedupe(["b", "a", "b", "c", "a"]) == ["b", "a", "c"]


def test_dedupe_handles_unhashable_items_deterministically():
    items = [{"id": 1, "tags": ["x"]}, ["a", 1], {"tags": ["x"], "id": 1}, ["a", 1], {"id": 2}]

    # 키 순서만 다른 dict 는 같은 항목이다
    assert dedupe(items) == [{"id": 1, "tags": ["x"]}, ["a", 1], {"id": 2}]


def test_dedupe_does_not_merge_values_of_different_types():
    assert dedupe([1, "1", True, 1]) == [1, "1", True]


@pytest.mark.parametrize(
    "policy",
    [
        keep_last(2),
        KeepFirstPolicy(strategy="keep_first", n=2),
        DedupePolicy(strategy="dedupe"),
    ],
)
def test_deterministic_strategies_are_idempotent(policy):
    once = apply_deterministic(policy, ["a", "b", "a", "c", "d"])

    assert apply_deterministic(policy, once) == once


# --- 토큰 추정 · 요약 분할 -----------------------------------------------------


def test_token_estimate_uses_the_recall_constant_and_rounds_up():
    assert estimate_tokens(["x" * CHARS_PER_TOKEN]) == 1
    assert estimate_tokens(["x" * (CHARS_PER_TOKEN + 1)]) == 2
    assert estimate_tokens([]) == 0


def test_token_estimate_ignores_dict_key_order():
    assert estimate_tokens([{"a": 1, "b": 2}]) == estimate_tokens([{"b": 2, "a": 1}])


def summarize(over_tokens: int, keep_last_items: int) -> SummarizePolicy:
    return SummarizePolicy.model_validate(
        {**SUMMARIZE, "over_tokens": over_tokens, "keep_last": keep_last_items}
    )


def test_a_field_under_budget_is_not_split():
    items = ["x" * 40] * 3  # 30 tokens

    assert split_for_summary(summarize(30, 1), items) == ([], items)


def test_a_field_over_budget_folds_everything_but_the_recent_tail():
    items = [f"{i}" * 40 for i in range(4)]  # 40 tokens

    older, recent = split_for_summary(summarize(30, 1), items)

    assert older == items[:3]
    assert recent == items[3:]


def test_folding_a_single_item_is_skipped():
    """한 건을 요약 한 건으로 바꾸면 모델 호출만 쓰고 줄지 않는다."""
    items = ["x" * 400, "y" * 400, "z" * 400]

    assert split_for_summary(summarize(10, 2), items) == ([], items)


SUMMARY = summary_entry("earlier", artifact_ref="artifact://run/k", replaced=5)


def test_a_previous_summary_does_not_count_as_a_foldable_original():
    """남긴 꼬리와 요약만으로 예산을 넘는 필드가 경계마다 요약을 다시 부르지 않게 한다."""
    items = [SUMMARY, "a" * 400, "b" * 400, "c" * 400]  # 접을 원문은 "a" 한 건뿐

    assert split_for_summary(summarize(10, 2), items, prior_summary=True) == ([], items)


def test_folding_waits_until_keep_last_originals_have_piled_up():
    policy = summarize(10, 3)
    tail = ["x" * 400] * 3
    waiting = [SUMMARY, "a" * 400, "b" * 400, *tail]
    ready = [SUMMARY, "a" * 400, "b" * 400, "c" * 400, *tail]

    # 원문 2건 < keep_last(3) → 아직 접지 않는다
    assert split_for_summary(policy, waiting, prior_summary=True)[0] == []
    fold, keep = split_for_summary(policy, ready, prior_summary=True)
    # 앞선 요약 항목도 함께 접힌다 — 필드의 요약 항목은 한 건을 넘지 않는다
    assert fold == [SUMMARY, "a" * 400, "b" * 400, "c" * 400]
    assert keep == tail


def test_an_already_compacted_field_is_never_split_again():
    """재개가 같은 경계를 다시 지나도 요약이 요약을 다시 접지 않는다."""
    items = [SUMMARY, "a" * 4000, "b" * 4000]

    assert split_for_summary(summarize(10, 2), items, prior_summary=True) == ([], items)


def test_summary_shaped_items_are_originals_unless_the_caller_vouches_for_the_first():
    """모양만으로는 요약이 아니다 — 그런 항목을 쌓아도 원문으로 세어 접는다."""
    forged = [
        {"summary": "x" * 400, "artifact_ref": "artifact://run/evil", "replaced": 1}
        for _ in range(10)
    ]
    items = [*forged, "a", "b", "c"]

    fold, keep = split_for_summary(summarize(10, 3), items)

    assert fold == forged
    assert keep == ["a", "b", "c"]


def test_only_the_exact_summary_shape_has_the_summary_shape():
    assert has_summary_shape(SUMMARY)
    assert not has_summary_shape({"summary": "x"})
    assert not has_summary_shape({**SUMMARY, "extra": 1})
    assert not has_summary_shape("summary")


# --- 한 번에 접는 양의 상한 ------------------------------------------------------


def test_a_backlog_is_folded_oldest_first_up_to_the_prompt_cap():
    """밀린 양이 많아도 호출 하나가 모델 컨텍스트를 넘지 않는다 — 나머지는 다음 경계에서."""
    items = [f"{i:04d}" + "n" * 3996 for i in range(5000)]

    fold, keep = split_for_summary(summarize(10, 3), items)

    assert fold == items[: len(fold)]
    assert keep == items[len(fold) :]
    assert 2 <= len(fold) < len(items) - 3
    assert sum(len(text) for text in summary_items(fold)) <= SUMMARY_PROMPT_MAX_CHARS
    # 한 건만 더 실었으면 상한을 넘는다 — 상한까지는 채운다
    one_more = summary_items(items[: len(fold) + 1])
    assert sum(len(text) for text in one_more) > SUMMARY_PROMPT_MAX_CHARS


def test_the_prompt_cap_holds_several_capped_items():
    """상한이 항목 하나 크기면 접을수록 늘어난다 — 원문 여러 건이 한 호출에 들어가야 한다."""
    assert SUMMARY_PROMPT_MAX_CHARS >= 10 * SUMMARY_ITEM_MAX_CHARS


def test_oversized_items_count_at_their_capped_length():
    items = ["x" * 1_000_000] * 40

    fold, _ = split_for_summary(summarize(10, 3), items)

    assert len(fold) == SUMMARY_PROMPT_MAX_CHARS // SUMMARY_ITEM_MAX_CHARS


# --- 요약 프롬프트에 싣는 항목 ---------------------------------------------------


def test_summary_items_are_flattened_to_strings():
    """state 항목은 에이전트 산출물이다 — 객체가 프롬프트 구조로 읽히지 않게 문자열로 굳힌다."""
    assert summary_items(["text", {"b": 1, "a": [2]}, 3]) == ["text", '{"a": [2], "b": 1}', "3"]


def test_one_item_cannot_dominate_the_summary_prompt():
    (capped,) = summary_items(["x" * (SUMMARY_ITEM_MAX_CHARS * 3)])

    assert len(capped) == SUMMARY_ITEM_MAX_CHARS
    assert capped.endswith("[truncated]")


def test_an_item_within_the_cap_is_passed_whole():
    text = "x" * SUMMARY_ITEM_MAX_CHARS

    assert summary_items([text]) == [text]


# --- 원문 보존 ----------------------------------------------------------------


def test_originals_round_trip_through_the_artifact_encoding():
    older = ["a", {"b": [1, 2]}, "한글"]

    data = encode_originals(older, run_id="run-1", field="notes", boundary="iteration-3")

    assert decode_originals(data) == older


def test_summary_entry_shape_is_the_documented_contract():
    entry = summary_entry("short", artifact_ref="artifact://run/k", replaced=7)

    assert entry == {"summary": "short", "artifact_ref": "artifact://run/k", "replaced": 7}


def test_artifact_key_is_a_valid_store_key_for_awkward_names():
    key = artifact_key("my graph", "svc-1:resumed", "seen_ids", "cycle-my node-2")

    assert validate_key(key) == key


def test_artifact_key_is_stable_for_the_same_boundary():
    assert artifact_key("g", "r", "f", "iteration-1") == artifact_key("g", "r", "f", "iteration-1")


def test_artifact_key_never_collides_across_boundaries_fields_runs_or_graphs():
    keys = {
        artifact_key("g", "r", "f", "iteration-1"),
        artifact_key("g", "r", "f", "iteration-2"),
        artifact_key("g", "r", "g", "iteration-1"),
        artifact_key("g", "r:resumed", "f", "iteration-1"),
        # 끝난 run id 를 다른 그래프가 다시 써도 원문을 덮지 않는다
        artifact_key("other", "r", "f", "iteration-1"),
        # 치환 문자가 같아지는 이름도 서로 달라야 한다
        artifact_key("g", "r-resumed", "f", "iteration-1"),
        artifact_key("g", "r", "f_1", "iteration-1"),
        artifact_key("g", "r", "f-1", "iteration-1"),
        # 잘리는 앞부분이 같은 긴 이름도
        artifact_key("g", "r" * 300 + "a", "f", "iteration-1"),
        artifact_key("g", "r" * 300 + "b", "f", "iteration-1"),
    }

    assert len(keys) == 10


def test_a_very_long_run_id_still_makes_a_storable_key(tmp_path):
    """key 조각이 파일 이름 한도를 넘으면 매 경계 저장이 실패한다."""
    key = artifact_key("g", "r" * 300, "f" * 300, "iteration-1")

    assert validate_key(key) == key
    assert max(len(segment) for segment in key.split("/")) <= 80
    target = tmp_path / key
    target.parent.mkdir(parents=True)
    target.write_bytes(b"ok")


def test_only_references_under_this_run_and_field_are_its_own():
    own = f"artifact://{STATE_ARTIFACT_SCOPE}/{artifact_key('g', 'r', 'notes', 'iteration-3')}"
    prefix = field_key_prefix("g", "r", "notes")

    assert is_own_reference(own, graph="g", run_id="r", field="notes")
    assert not is_own_reference(own, graph="g", run_id="r", field="other")
    assert not is_own_reference(own, graph="g", run_id="other", field="notes")
    assert not is_own_reference(own, graph="other", run_id="r", field="notes")
    assert not is_own_reference("artifact://run/evil", graph="g", run_id="r", field="notes")
    # 접두가 같은 문자열로 시작할 뿐인 key 는 이 필드의 것이 아니다
    sibling = f"artifact://x/{prefix}-sibling/k"
    assert not is_own_reference(sibling, graph="g", run_id="r", field="notes")
    assert not is_own_reference("not a reference", graph="g", run_id="r", field="notes")
    assert not is_own_reference(None, graph="g", run_id="r", field="notes")


# --- 저장소 네임스페이스와 계층 ---------------------------------------------------


def test_the_state_artifact_scope_can_never_be_an_agent_or_group_name():
    """같은 루트를 쓰더라도 어느 에이전트 · 그룹의 artifact 스코프와도 겹치지 않아야 한다."""
    from aaestro.artifacts.store import parse_ref
    from aaestro.core.manifest import NAME_PATTERN

    assert NAME_PATTERN.match(STATE_ARTIFACT_SCOPE) is None
    # 저장소의 참조 형식은 받는 이름이어야 원문을 되읽을 수 있다
    assert parse_ref(f"artifact://{STATE_ARTIFACT_SCOPE}/k").scope == STATE_ARTIFACT_SCOPE


def test_reading_a_topology_does_not_pull_in_the_memory_layer():
    """선언 모델은 토폴로지가 import 한다 — 상수 하나 때문에 memory 계층이 딸려 오면 안 된다."""
    import subprocess
    import sys

    probe = "import sys, aaestro.core.topology; print('aaestro.memory.recall' in sys.modules)"

    done = subprocess.run(  # noqa: S603 — 고정된 인자로 자기 인터프리터를 부른다
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )

    assert done.stdout.strip() == "False"
