"""Echo executor for the test agent image.

테스트 전용 실행기 — 모델을 호출하지 않고 입력을 그대로 돌려준다.
프로토콜/lifecycle 검증(컨테이너 기동, health, invoke/stream, drain)에서
모델 비결정성과 API 비용을 배제하기 위한 대역이다.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from aaestro.core.agent import TaskResult
from aaestro.core.events import DoneEvent, TokenEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aaestro.core.agent import TaskRequest
    from aaestro.core.events import TaskEvent


class EchoExecutor:
    """Returns the task input as its output.

    태스크 입력을 그대로 출력으로 돌려주는 실행기.
    """

    async def execute(self, task: TaskRequest) -> TaskResult:
        """입력을 echo 한 완료 결과를 돌려준다."""
        return TaskResult.completed(task, output=dict(task.input))

    async def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        """입력을 한 토큰으로 흘린 뒤 완료 이벤트를 낸다."""
        yield TokenEvent(task_id=task.task_id, text=str(task.input))
        yield DoneEvent(task_id=task.task_id, output=dict(task.input))


__all__ = ["EchoExecutor"]
