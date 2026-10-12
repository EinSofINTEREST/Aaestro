"""The permission agent — widens a running agent's permissions within its ceiling (#279).

01 Access Control 3·4 (결정 D1): 실행 중 권한 확장은 작업 에이전트 밖의 이 에이전트가 맡는다. 작업
에이전트는 A2A 로 **요청만** 하고, 이 에이전트가 자기 신원으로 레지스트리 부여 API 를 부른다.

**모델 판단을 쓰지 않는다 — 규칙만으로 결정한다.** 요청은 신뢰할 수 없는 입력이고, 줄 수 있는
범위는 레지스트리가 확장 상한으로 결정적으로 제한한다. 모델을 끼우면 "상한을 무시하라" 류 지시에
흔들릴 표면만 늘고 권한은 늘지 않는다.

규칙:

1. 요청은 A2A 로 와야 한다 — 수신 입구가 확인한 호출자(``TaskRequest.caller``)만 믿는다
2. **호출자 자신에게만** 부여한다 — 요청 본문에 다른 에이전트 이름을 적을 자리가 없다
3. 요청 모양이 맞지 않으면 거절한다 (``VAL_002``) — 추측해서 고치지 않는다
4. 상한·TTL·자기 부여·운영자 회수는 레지스트리가 판정한다 — 거절은 ``ACC_003`` 으로 되돌려준다
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from aaestro.access.client import ACCESS_URL_ENV
from aaestro.access.model import Mode, ResourceKind, mode_problem
from aaestro.access.registry import ACCESS_CREDENTIAL_ENV
from aaestro.core.agent import TaskResult
from aaestro.core.errors import (
    AaestroError,
    ErrorCategory,
    ErrorCode,
    field_problems,
    raise_detached,
)
from aaestro.core.events import DoneEvent, ErrorEvent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from aaestro.core.agent import TaskRequest
    from aaestro.core.events import TaskEvent
    from aaestro.core.manifest import AgentManifest

log = structlog.get_logger(__name__)

REQUEST_TIMEOUT_S = 10.0
REMEMBERED_ANSWERS = 1024
"""재시도에 같은 답을 주려고 기억하는 답의 수 — 넘치면 오래된 것부터 잊는다 (상주 프로세스)."""


class ExpansionRequest(BaseModel):
    """What a worker may ask for — for itself only.

    ``agent`` 필드는 없다: 부여 대상은 확인된 호출자로 정해지고 본문이 바꿀 수 없다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: ResourceKind
    target: str = Field(min_length=1, max_length=512, pattern=r"^[^\s\x00-\x1f]+$")
    mode: Mode | None = None
    ttl_s: float = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _mode_fits(self) -> ExpansionRequest:
        problem = mode_problem(self.kind, self.mode, memory_needs_mode=True)
        if problem is not None:
            raise ValueError(problem)
        return self


class PermissionAgent:
    """Executor for the permission agent — no model, no tools, one registry call per request."""

    def __init__(
        self,
        manifest: AgentManifest | None = None,
        *,
        base_url: str | None = None,
        credential: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self._agent = manifest.name if manifest is not None else "permission-agent"
        self._base_url = base_url or os.environ.get(ACCESS_URL_ENV, "")
        self._credential = credential or os.environ.get(ACCESS_CREDENTIAL_ENV, "")
        if not (self._base_url and self._credential):
            # 신원 없이 뜨면 아무것도 줄 수 없다 — 조용히 뜨면 원인 모를 실패만 남는다
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_001,
                message="permission agent needs the access registry URL and its own identity",
                agent=self._agent,
                details={"settings": [ACCESS_URL_ENV, ACCESS_CREDENTIAL_ENV]},
            )
        self._http = http or httpx.AsyncClient(base_url=self._base_url, timeout=REQUEST_TIMEOUT_S)
        self._completed: OrderedDict[tuple[str | None, str], TaskResult] = OrderedDict()
        self._inflight: dict[tuple[str | None, str], asyncio.Future[TaskResult]] = {}
        """진행 중인 요청 — 키가 끝날 때까지 이 표가 태스크를 쥔다 (완료 콜백이 뺀다)."""

    async def execute(self, task: TaskRequest) -> TaskResult:
        # 호출자까지 키에 넣는다 — task id 는 호출자가 고르므로, 남의 id 로 남의 답을 받으면 안 된다
        key = (task.caller, task.task_id)
        cached = self._completed.get(key)
        if cached is not None:
            # 같은 요청의 재시도가 부여를 두 번 기록하지 않게 (02 Rule 3)
            return cached
        running = self._inflight.get(key)
        if running is None:
            # 같은 요청이 동시에 들어오면 한 번만 부여하고 같은 답을 나눠 준다 — 기억은 끝난 뒤에야
            # 생기므로 그것만으로는 동시 재시도가 부여를 둘 만든다 (#295 리뷰)
            running = asyncio.ensure_future(self._answer(task, key))
            self._inflight[key] = running
            running.add_done_callback(lambda _: self._inflight.pop(key, None))
        # 기다리던 쪽이 취소돼도 부여 요청은 끝까지 간다 — 반쯤 기록된 부여를 남기지 않는다
        return await asyncio.shield(running)

    async def _answer(self, task: TaskRequest, key: tuple[str | None, str]) -> TaskResult:
        try:
            result = TaskResult.completed(task, output=await self._grant(task))
        except AaestroError as err:
            result = TaskResult.failed(task, err)
            if err.retryable:
                return result
        self._remember(key, result)
        return result

    def _remember(self, key: tuple[str | None, str], result: TaskResult) -> None:
        self._completed[key] = result
        while len(self._completed) > REMEMBERED_ANSWERS:
            self._completed.popitem(last=False)

    async def stream(self, task: TaskRequest) -> AsyncIterator[TaskEvent]:
        result = await self.execute(task)
        if result.error is not None:
            yield ErrorEvent(task_id=task.task_id, error=result.error)
            return
        yield DoneEvent(task_id=task.task_id, output=result.output)

    async def _grant(self, task: TaskRequest) -> dict[str, Any]:
        caller = task.caller
        if caller is None:
            raise self._refused(
                "expansion requests must arrive over A2A from the agent that needs them",
                task=task,
            )
        asked = self._parse(task, caller)
        body: dict[str, Any] = {
            "agent": caller,
            "kind": asked.kind.value,
            "target": asked.target,
            "ttl_s": asked.ttl_s,
            "reason": asked.reason,
            "requested_by": caller,
        }
        if asked.mode is not None:
            body["mode"] = asked.mode.value
        try:
            response = await self._http.post(
                "/v1/access/grants",
                json=body,
                headers={"Authorization": f"Bearer {self._credential}"},
            )
        except httpx.TransportError as err:
            raise self._unreachable(task, caller, asked) from err
        if response.status_code >= 500:
            raise self._unreachable(task, caller, asked)
        if response.status_code != 201:
            raise self._registry_refusal(task, caller, asked, response)
        record = response.json()
        log.info(
            "expansion granted",
            agent=caller,
            resource=asked.kind.value,
            target=asked.target,
            grant_id=record["rule_id"],
            decided_by=self._agent,
            task_id=task.task_id,
        )
        return {
            "granted": True,
            "rule_id": record["rule_id"],
            "kind": record["kind"],
            "target": record["target"],
            "mode": record["mode"],
            "expires_at": record["expires_at"],
        }

    def _parse(self, task: TaskRequest, caller: str) -> ExpansionRequest:
        raw: Any = task.input
        # ask_peer 는 요청을 문자열 하나로 보낸다 — JSON 문서만 받는다, 문장을 해석하지 않는다
        if set(raw) == {"request"} and isinstance(raw["request"], str):
            try:
                raw = json.loads(raw["request"])
            except ValueError as err:
                raise self._invalid(
                    task, caller, "request must be a JSON expansion request"
                ) from err
        if not isinstance(raw, dict):
            raise self._invalid(task, caller, "request must be a JSON object")
        try:
            return ExpansionRequest.model_validate(raw)
        except ValidationError as err:
            raise_detached(
                self._invalid(
                    task, caller, "malformed expansion request", errors=field_problems(err)
                )
            )

    def _refused(self, message: str, *, task: TaskRequest, **details: Any) -> AaestroError:
        log.warning(
            "expansion refused", agent=task.caller or "", error_code="ACC_003", reason=message
        )
        return AaestroError(
            category=ErrorCategory.FORBIDDEN,
            code=ErrorCode.ACC_003,
            message=message,
            agent=self._agent,
            task_id=task.task_id,
            details=details,
        )

    def _invalid(
        self, task: TaskRequest, caller: str, message: str, **details: Any
    ) -> AaestroError:
        log.warning("expansion request malformed", agent=caller, error_code="VAL_002")
        return AaestroError(
            category=ErrorCategory.VALIDATION,
            code=ErrorCode.VAL_002,
            message=message,
            agent=self._agent,
            task_id=task.task_id,
            details=details,
        )

    def _unreachable(self, task: TaskRequest, caller: str, asked: ExpansionRequest) -> AaestroError:
        log.warning(
            "expansion undecidable",
            agent=caller,
            resource=asked.kind.value,
            target=asked.target,
            error_code="ACC_002",
        )
        return AaestroError(
            category=ErrorCategory.NETWORK,
            code=ErrorCode.ACC_002,
            message="access registry unreachable",
            agent=self._agent,
            task_id=task.task_id,
            retryable=True,
        )

    def _registry_refusal(
        self, task: TaskRequest, caller: str, asked: ExpansionRequest, response: httpx.Response
    ) -> AaestroError:
        try:
            error = response.json()["error"]
        except (ValueError, KeyError, TypeError):
            error = {"code": "ACC_003", "message": "grant refused", "details": {}}
        log.warning(
            "expansion refused by registry",
            agent=caller,
            resource=asked.kind.value,
            target=asked.target,
            error_code=error.get("code"),
            decided_by=self._agent,
        )
        return AaestroError(
            category=ErrorCategory.FORBIDDEN,
            code=ErrorCode.ACC_003,
            message=str(error.get("message") or "grant refused"),
            agent=self._agent,
            task_id=task.task_id,
            details={"registry_code": error.get("code"), **dict(error.get("details") or {})},
        )


__all__ = ["ExpansionRequest", "PermissionAgent"]
