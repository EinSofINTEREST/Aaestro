"""A run submitted through the CLI's resolution must survive the process.

#220 이전에는 CLI 가 URL 을 줄 통로가 없어 `memory` checkpointer 밖에 쓸 수 없었다.
`memory` 는 프로세스와 함께 사라지므로, CLI 로 낸 run 은 **전부 재개 불가**였다 —
01 의 "마지막 checkpoint 에서 재개 가능, 데이터 손실 없음" 이 성립하지 않았다.

여기서는 CLI 가 쓰는 해석 경로(`checkpointer_for`)로 만든 checkpointer 에
run 이 실제로 남는지를, **별개로 만든 두 번째** checkpointer 로 확인한다.
"""

from __future__ import annotations

import argparse
import os
from typing import Any

import pytest
import yaml

from aaestro.cli.main import checkpointer_for
from aaestro.core.agent import TaskResult
from aaestro.orchestrator.checkpoint import close_checkpointer
from aaestro.orchestrator.submit import RunSubmitter
from tests.fixtures.topologies import make_mission

URL = os.environ.get("AAESTRO_TEST_POSTGRES_URL", "")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="AAESTRO_TEST_POSTGRES_URL not set"),
]


class EchoRuntime:
    """노드를 빈 출력으로 완료시키는 runtime 대역."""

    async def invoke(self, node: Any, task: Any) -> TaskResult:
        return TaskResult.completed(task, output={})


@pytest.fixture
def config_dir(tmp_path):
    """CLI 가 읽을 설정 — backend 와 URL 을 여기서 쥔다."""
    (tmp_path / "local.yaml").write_text(
        yaml.safe_dump({"orchestrator": {"checkpointer": "postgres", "checkpointer_url": URL}}),
        encoding="utf-8",
    )
    return tmp_path


def cli_checkpointer(config_dir) -> Any:
    return checkpointer_for(
        argparse.Namespace(checkpointer=None, environment="local", config_dir=str(config_dir))
    )


async def test_a_cli_resolved_run_is_visible_to_another_process(config_dir):
    """같은 run_id 를 **새로 만든** checkpointer 가 다시 찾는다."""
    run_id = f"run-cli-{os.getpid()}"
    topology = make_mission()

    first = cli_checkpointer(config_dir)
    try:
        await RunSubmitter(runtime=EchoRuntime(), checkpointer=first).submit(
            topology, {"query": "q"}, run_id=run_id
        )
    finally:
        await close_checkpointer(first)

    # 재시작된 프로세스를 흉내낸다 — 설정만 보고 새로 만든다
    second = cli_checkpointer(config_dir)
    try:
        state = await second.aget({"configurable": {"thread_id": run_id}})
    finally:
        await close_checkpointer(second)

    assert state is not None, "CLI 해석 경로로 만든 checkpointer 에 run 이 남지 않았다"


async def test_memory_would_not_survive(config_dir, tmp_path):
    """대조군 — `memory` 는 프로세스를 넘기지 못한다. 그것이 #220 의 상태였다."""
    run_id = f"run-mem-{os.getpid()}"
    args = argparse.Namespace(checkpointer="memory", environment="local", config_dir=str(tmp_path))

    first = checkpointer_for(args)
    await RunSubmitter(runtime=EchoRuntime(), checkpointer=first).submit(
        make_mission(), {"query": "q"}, run_id=run_id
    )

    second = checkpointer_for(args)
    assert await second.aget({"configurable": {"thread_id": run_id}}) is None
