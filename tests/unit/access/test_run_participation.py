"""The registry decides run scope spaces from run records (#608).

run space (``run:<run_id>:<alias>``) 는 선언 파일이 아니라 run 기록으로 판정한다. 확인하는 계약:

- 그 run 의 시작 시점 그래프에서 노드인 에이전트만 — 진행 중이면 rw, 끝났으면 ro
- 노드가 아닌 에이전트 · 다른 그래프에 배포된 신원 · ``direct-`` run · 모르는 run 은 거부
- run 의 시작과 종료가 레지스트리 버전을 올린다 — 강제 지점 캐시가 버려진다
- 판정은 fake clock 과 in-memory run 저장소로 — 실제로 기다리지 않는다 (06 Testing Access Control 6)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaestro.access.baselines import MemoryBaseline
from aaestro.access.model import DECLARATION, Mode, Outcome, ResourceKind
from aaestro.access.registry import AccessRegistry
from aaestro.access.store import InMemoryAccessStore
from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.run import RunManager, RunStatus
from aaestro.orchestrator.runstore import InMemoryRunStore, RunRecord
from tests.fixtures.access import access_workspace
from tests.fixtures.topologies import make_mission

SCRATCH = {"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}
NODE = "planner"
OUTSIDER = "loner"
POLL_S = 2.0


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def runs() -> InMemoryRunStore:
    return InMemoryRunStore()


@pytest.fixture
def registry(tmp_path: Path, clock: Clock, runs: InMemoryRunStore) -> AccessRegistry:
    catalog = Catalog.under(access_workspace(tmp_path))
    return AccessRegistry(
        store=InMemoryAccessStore(),
        catalog=catalog,
        baselines={ResourceKind.MEMORY: MemoryBaseline(catalog, runs=runs)},
        clock=clock,
        declarations_poll_s=POLL_S,
        runs=runs,
    )


@pytest.fixture
def manager(runs: InMemoryRunStore) -> RunManager:
    return RunManager(store=runs)


def start(manager: RunManager, run_id: str = "run-1") -> str:
    """실제 전이 경로 (슬롯 확보) 로 run 을 시작한다 — 스냅샷도 그 경로가 남긴다."""
    manager.acquire(make_mission(memory={"spaces": [SCRATCH]}), run_id=run_id)
    return run_id


def decide(registry: AccessRegistry, agent: str, target: str, mode: Mode, **kw) -> Outcome:
    return registry.decide(agent, ResourceKind.MEMORY, target, mode, **kw).outcome


# --- 판정 -------------------------------------------------------------------


@pytest.mark.parametrize("mode", [Mode.RW, Mode.RO])
def test_a_node_agent_reaches_a_live_runs_space(registry, manager, mode):
    run_id = start(manager)

    decision = registry.decide(NODE, ResourceKind.MEMORY, f"run:{run_id}:scratch", mode)

    assert decision.outcome is Outcome.ALLOW
    assert decision.decided_by == DECLARATION


def test_a_run_space_with_a_trailing_newline_is_not_the_same_space(registry, manager):
    """`$` 는 끝 개행 앞에서도 맞는다 — `x\\n` 이 `x` 와 같은 space 로 판정되면 안 된다 (#622)."""
    run_id = start(manager)

    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.ALLOW
    assert decide(registry, NODE, f"run:{run_id}:scratch\n", Mode.RW) is Outcome.DENY


def test_a_draining_run_is_still_writable(registry, manager):
    run_id = start(manager)
    manager.runs[run_id].request_drain()
    manager.publish(manager.runs[run_id])

    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.ALLOW


@pytest.mark.parametrize(
    "ended", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.HALTED, RunStatus.STOPPED]
)
def test_an_ended_run_is_read_only(registry, manager, ended):
    run_id = start(manager)
    manager.release(run_id, ended)

    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RO) is Outcome.ALLOW
    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.DENY


def test_an_agent_that_is_not_a_node_is_denied(registry, manager):
    run_id = start(manager)

    assert decide(registry, OUTSIDER, f"run:{run_id}:scratch", Mode.RO) is Outcome.DENY


def test_an_unknown_run_is_denied(registry):
    assert decide(registry, NODE, "run:never-ran:scratch", Mode.RO) is Outcome.DENY


def test_an_undeclared_alias_is_denied(registry, manager):
    run_id = start(manager)

    assert decide(registry, NODE, f"run:{run_id}:elsewhere", Mode.RO) is Outcome.DENY


def test_a_direct_run_has_no_run_space(registry, runs):
    """기록이 있어도 ``direct-`` run 은 run space 가 없다 — 접두사가 먼저다."""
    runs.upsert(
        RunRecord(
            run_id="direct-1",
            graph="research-pipeline",
            mode="mission",
            status="running",
            memory_spaces={"scratch": SCRATCH["ref"]},
            agents=(NODE,),
        )
    )

    assert decide(registry, NODE, "run:direct-1:scratch", Mode.RO) is Outcome.DENY


def test_an_identity_deployed_for_another_graph_is_denied(registry, manager):
    """같은 에이전트가 다른 그래프로도 배포돼 있으면 그 신원은 이 run 의 참여자가 아니다."""
    run_id = start(manager)
    target = f"run:{run_id}:scratch"
    own = registry.identity_of(registry.issue_identity(NODE, "dep-1", graph="research-pipeline"))
    other = registry.identity_of(registry.issue_identity(NODE, "dep-2", graph="feed-monitor"))

    assert decide(registry, NODE, target, Mode.RW, identity=own) is Outcome.ALLOW
    assert decide(registry, NODE, target, Mode.RW, identity=other) is Outcome.DENY


def test_a_resumed_service_run_id_with_colons_resolves(registry, manager):
    """재개한 service run 의 id 는 ``:`` 를 품는다 — 별칭은 마지막 조각이다."""
    run_id = start(manager, "svc-1:resumed")

    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.ALLOW


def test_without_run_records_no_run_space_opens(tmp_path, manager):
    start(manager)
    catalog = Catalog.under(access_workspace(tmp_path))
    registry = AccessRegistry(
        store=InMemoryAccessStore(),
        catalog=catalog,
        baselines={ResourceKind.MEMORY: MemoryBaseline(catalog)},
    )

    assert decide(registry, NODE, "run:run-1:scratch", Mode.RO) is Outcome.DENY


def test_the_snapshot_is_the_graph_at_run_start(registry, manager, runs):
    """재개의 재공지가 현재 그래프로 덮지 않는다 — run space 는 시작 때의 그래프가 정한다."""
    run_id = start(manager)
    manager.release(run_id, RunStatus.FAILED)

    manager.acquire(make_mission(), run_id=run_id, resuming=True)  # 지금의 그래프는 space 가 없다

    assert runs.get(run_id).memory_spaces == {"scratch": SCRATCH["ref"]}
    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.ALLOW


# --- 캐시 무효화 ---------------------------------------------------------------


def settle(registry: AccessRegistry, clock: Clock) -> int:
    clock.now += POLL_S
    return registry.version()


def test_a_run_starting_moves_the_version(registry, manager, clock):
    before = settle(registry, clock)

    start(manager)

    assert settle(registry, clock) > before


@pytest.mark.parametrize(
    "ended", [RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.HALTED, RunStatus.STOPPED]
)
def test_a_run_ending_moves_the_version(registry, manager, clock, ended):
    """끝난 run 의 rw 캐시가 남으면 끝난 run 에 계속 쓴다 — 버전이 올라 강제 지점이 버린다."""
    run_id = start(manager)
    before = settle(registry, clock)

    manager.release(run_id, ended)

    assert settle(registry, clock) > before


def test_an_end_resume_end_inside_one_poll_moves_the_version(registry, manager, clock):
    """재개한 사이 rw 를 캐시한 강제 지점이, 같은 주기에 다시 끝난 run 에 계속 쓰면 안 된다."""
    run_id = start(manager)
    manager.release(run_id, RunStatus.COMPLETED)
    before = settle(registry, clock)

    manager.acquire(make_mission(), run_id=run_id, resuming=True)
    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.ALLOW
    manager.release(run_id, RunStatus.COMPLETED)

    assert settle(registry, clock) > before
    assert decide(registry, NODE, f"run:{run_id}:scratch", Mode.RW) is Outcome.DENY


def test_iteration_progress_does_not_move_the_version(registry, manager, clock, runs):
    """iteration 마다 버전이 오르면 모든 판정 캐시가 iteration 마다 버려진다."""
    run_id = start(manager)
    before = settle(registry, clock)

    handle = manager.runs[run_id]
    handle.iteration = 3
    manager.publish(handle)

    assert settle(registry, clock) == before


def test_an_unreadable_run_store_keeps_other_decisions_working(registry, manager, clock, runs):
    """run 저장소 장애가 다른 종류의 판정까지 멈추게 두지 않는다 — 다시 읽히면 전이를 잡는다."""
    run_id = start(manager)
    before = settle(registry, clock)

    def broken() -> tuple[int, int]:
        raise AaestroError(
            category=ErrorCategory.STORAGE, code=ErrorCode.STOR_003, message="run store down"
        )

    healthy = runs.liveness
    runs.liveness = broken  # type: ignore[method-assign]
    manager.release(run_id, RunStatus.COMPLETED)

    assert settle(registry, clock) == before
    assert registry.decide(NODE, ResourceKind.EGRESS, "example.com").outcome is Outcome.DENY

    runs.liveness = healthy  # type: ignore[method-assign]
    assert settle(registry, clock) > before
