"""Redis checkpointer against a real server.

postgres 와 **같은 계약**을 검증한다 — 오케스트레이터는 `ainvoke` 로 굴리므로
동기 saver 로는 `aget_tuple` 이 `NotImplementedError` 다. postgres 만 고치고
redis 를 두면 같은 버그가 한쪽에 남는다 (#171 리뷰 지적).
"""

from __future__ import annotations

import os

import pytest

from aaestro.core.agent import TaskResult
from aaestro.orchestrator.checkpoint import build_checkpointer, close_checkpointer
from aaestro.orchestrator.run import RunStatus
from aaestro.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_mission

pytestmark = pytest.mark.integration

URL = os.environ.get("AAESTRO_TEST_REDIS_URL", "")
requires_redis = pytest.mark.skipif(not URL, reason="AAESTRO_TEST_REDIS_URL not set")


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node, task) -> TaskResult:
        return TaskResult.completed(task, output={})


@requires_redis
async def test_a_graph_run_completes_with_the_redis_checkpointer():
    """반환된 객체가 실제로 checkpointer 여야 한다 — context manager 면 여기서 깨진다."""
    checkpointer = build_checkpointer("redis", url=URL)
    submitter = RunSubmitter(runtime=EchoRuntime(), checkpointer=checkpointer)

    result = await submitter.submit(make_mission(), {"query": "q"}, run_id="rd-complete")

    assert result.status is RunStatus.COMPLETED
    await close_checkpointer(checkpointer)


@requires_redis
async def test_the_checkpoint_is_readable_after_the_run():
    """저장이 안 되면 재개도 재현도 불가능하다 — run 이 끝났다는 것만으로는 부족하다."""
    checkpointer = build_checkpointer("redis", url=URL)
    submitter = RunSubmitter(runtime=EchoRuntime(), checkpointer=checkpointer)
    await submitter.submit(make_mission(), {"query": "q"}, run_id="rd-readable")

    found = await checkpointer.aget_tuple({"configurable": {"thread_id": "rd-readable"}})

    assert found is not None
    await close_checkpointer(checkpointer)


@requires_redis
async def test_the_async_contract_is_served():
    """동기 saver 를 돌려주면 ainvoke 가 NotImplementedError 로 죽는다."""
    checkpointer = build_checkpointer("redis", url=URL)

    assert await checkpointer.aget_tuple({"configurable": {"thread_id": "rd-absent"}}) is None
    await close_checkpointer(checkpointer)


@requires_redis
async def test_two_runs_do_not_share_a_thread():
    """thread 가 섞이면 한 run 의 재개가 다른 run 의 state 를 집는다."""
    checkpointer = build_checkpointer("redis", url=URL)
    submitter = RunSubmitter(runtime=EchoRuntime(), checkpointer=checkpointer)
    await submitter.submit(make_mission(), {"query": "a"}, run_id="rd-a")
    await submitter.submit(make_mission(), {"query": "b"}, run_id="rd-b")

    first = await checkpointer.aget_tuple({"configurable": {"thread_id": "rd-a"}})
    second = await checkpointer.aget_tuple({"configurable": {"thread_id": "rd-b"}})

    assert first is not None
    assert second is not None
    assert first.config["configurable"]["thread_id"] != second.config["configurable"]["thread_id"]
    await close_checkpointer(checkpointer)


@requires_redis
async def test_closing_disconnects_the_sockets():
    """여는 쪽이 닫는다 — 상주 프로세스는 프로세스 종료에 기댈 수 없다.

    redis-py 클라이언트는 다음 명령에 **자동 재연결**하므로 "닫힌 뒤 실패"
    로는 검증할 수 없다. 실제로 끊긴 것은 풀이 쥔 소켓이다.
    """
    checkpointer = build_checkpointer("redis", url=URL)
    await checkpointer.aget_tuple({"configurable": {"thread_id": "rd-close"}})
    pooled = checkpointer._redis.connection_pool._available_connections
    assert any(connection.is_connected for connection in pooled)

    await close_checkpointer(checkpointer)

    assert not any(connection.is_connected for connection in pooled)


@requires_redis
async def test_closing_clears_the_search_index_client():
    """라이브러리의 정리 경로에 위임해야 하는 이유.

    redisvl 인덱스가 클라이언트 참조를 쥔 채 남으면, 다른 스레드의 이벤트
    루프에서 그것을 닫으려 든다 — `_redis.aclose()` 만 부르면 빠지는 부분이다.
    """
    checkpointer = build_checkpointer("redis", url=URL)
    await checkpointer.aget_tuple({"configurable": {"thread_id": "rd-index"}})
    assert checkpointer.checkpoints_index._redis_client is not None

    await close_checkpointer(checkpointer)

    assert checkpointer.checkpoints_index._redis_client is None
