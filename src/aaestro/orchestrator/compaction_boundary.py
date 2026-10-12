"""Where in a run state compaction happens.

압축이 일어나는 경계 — service 는 iteration 사이, mission 은 순환 edge 로의 재진입.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from aaestro.orchestrator.run import run_generations

_RUN_ID_KEY = "_run_id"
_TRACE_ID_KEY = "_trace_id"


@dataclass(frozen=True)
class Boundary:
    """The one place in a run where compaction is allowed to happen.

    압축이 일어나는 경계 하나. ``artifact_run`` 과 ``label`` 이 원문 artifact 의 멱등 key 를
    이룬다 — 같은 경계를 다시 지나면 같은 key 를 덮어쓴다.

    Attributes:
        run_id: The run doing the work — 로그와 요약 태스크에 실린다.
        artifact_run: The run the originals are filed under. 재개한 service run 은 id 에
            접미사가 붙지만 같은 run 을 잇는 것이므로, 원래 run id 로 묶어야 재개가 같은
            경계의 원문을 덮어쓴다.
        label: Which boundary — ``iteration-N`` 또는 ``cycle-<node>-<passes>``.
    """

    run_id: str
    trace_id: str
    label: str
    artifact_run: str
    iteration: int | None = None

    @classmethod
    def after_iteration(cls, state: Mapping[str, Any], iteration: int) -> Boundary:
        """service iteration ``iteration`` 과 그다음 iteration 사이."""
        run_id = str(state.get(_RUN_ID_KEY) or "run-unknown")
        return cls(
            run_id=run_id,
            trace_id=str(state.get(_TRACE_ID_KEY) or run_id),
            label=f"iteration-{iteration}",
            artifact_run=run_generations(run_id)[-1],
            iteration=iteration,
        )

    @classmethod
    def on_reentry(cls, state: Mapping[str, Any], node_id: str, passes: int) -> Boundary:
        """mission 이 순환 edge 로 노드에 다시 들어가는 자리."""
        run_id = str(state.get(_RUN_ID_KEY) or "run-unknown")
        return cls(
            run_id=run_id,
            trace_id=str(state.get(_TRACE_ID_KEY) or run_id),
            label=f"cycle-{node_id}-{passes}",
            artifact_run=run_id,
        )

    def log_fields(self) -> dict[str, Any]:
        """05 의 orchestrator 필수 필드 — service 경계는 ``iteration`` 을 더한다."""
        fields: dict[str, Any] = {"run_id": self.run_id, "boundary": self.label}
        if self.iteration is not None:
            fields["iteration"] = self.iteration
        return fields


__all__ = ["Boundary"]
