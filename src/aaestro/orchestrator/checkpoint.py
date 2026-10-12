"""Checkpointer configuration and error mapping.

Checkpointer 설정과 에러 변환. 노드 완료마다 state 를 저장해 실패 시
마지막 checkpoint 에서 재개할 수 있게 한다 — 데이터 손실 없는 재개가 목표다.
"""

from __future__ import annotations

import asyncio
from enum import StrEnum
from typing import TYPE_CHECKING, Any, cast

from langgraph.checkpoint.base import BaseCheckpointSaver

from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator.telemetry import (
    OPERATION_LOAD,
    OPERATION_SAVE,
    STATUS_COMPLETED,
    STATUS_FAILED,
    CheckpointTelemetry,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from aaestro.observability.metrics import Metrics


class CheckpointerKind(StrEnum):
    """Checkpointer 백엔드 종류.

    dev 는 in-memory, 프로덕션은 외부 저장소를 쓴다 (오케스트레이터 다중 인스턴스 전제).
    """

    MEMORY = "memory"
    REDIS = "redis"
    POSTGRES = "postgres"


DEFAULT_CHECKPOINTER = CheckpointerKind.MEMORY


def _storage_error(code: ErrorCode, message: str, **details: Any) -> AaestroError:
    """Checkpoint 저장소 실패를 STORAGE 카테고리로 변환한다."""
    return AaestroError(
        category=ErrorCategory.STORAGE,
        code=code,
        message=message,
        retryable=True,
        details=details,
    )


def build_checkpointer(
    kind: CheckpointerKind | str = DEFAULT_CHECKPOINTER,
    *,
    url: str | None = None,
) -> BaseCheckpointSaver[Any]:
    """Build a checkpointer for the configured backend.

    설정된 백엔드의 checkpointer 를 만듭니다. ``default`` 는 프레임워크 기본값
    (in-memory) 으로 해석됩니다.

    Args:
        kind: Backend kind, or ``"default"`` to use the framework default.
        url: Connection URL required by external backends.

    Returns:
        A LangGraph checkpointer instance.

    Raises:
        AaestroError: CONFIG/``CFG_001`` if the backend is unknown or misconfigured.
    """
    try:
        resolved = DEFAULT_CHECKPOINTER if kind == "default" else CheckpointerKind(kind)
    except ValueError as err:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"unknown checkpointer backend: {kind}",
            details={"checkpointer": str(kind)},
        ) from err

    if resolved is CheckpointerKind.MEMORY:
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    if url is None:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"checkpointer '{resolved}' requires a connection url",
            details={"checkpointer": str(resolved)},
        )

    # Redis/Postgres saver 는 선택 의존성 — 미설치 시 설정 오류로 보고한다
    try:
        if resolved is CheckpointerKind.REDIS:
            return cast("BaseCheckpointSaver[Any]", _deferred_redis_saver(url))

        # **비동기 saver 여야 한다**: 오케스트레이터는 `ainvoke` 로 그래프를
        # 굴리는데 동기 PostgresSaver 는 `aget_tuple` 이 NotImplementedError 다.
        # 이것은 redis 경로에도 똑같이 적용된다 (위)
        #
        # ``from_conn_string`` 은 context manager 를 돌려준다 — 그것을
        # checkpointer 로 쓰면 그래프에 물리는 순간 깨진다
        return cast("BaseCheckpointSaver[Any]", _deferred_postgres_saver(url))
    except ImportError as err:
        raise AaestroError(
            category=ErrorCategory.CONFIG,
            code=ErrorCode.CFG_001,
            message=f"checkpointer backend not installed: {resolved}",
            details={"checkpointer": str(resolved)},
        ) from err


def _deferred_redis_saver(url: str) -> Any:
    """Build an async Redis saver whose schema is prepared on first use.

    postgres 와 같은 이유로 미룬다 — ``build_checkpointer`` 는 동기이고
    ``setup()`` 은 비동기다. 다만 redis 는 생성자가 동기라 커넥션 자체는
    바로 만들 수 있고, 미룰 것은 인덱스 준비뿐이다.
    """
    from langgraph.checkpoint.redis.aio import AsyncRedisSaver

    class DeferredAsyncRedisSaver(AsyncRedisSaver):
        """첫 사용 시 인덱스를 준비하는 saver."""

        def __init__(self) -> None:
            super().__init__(redis_url=url)
            self._prepared = False
            self._prepare_lock = asyncio.Lock()

        async def _prepare(self) -> None:
            if self._prepared:
                return
            async with self._prepare_lock:
                if self._prepared:
                    return
                await AsyncRedisSaver.asetup(self)
                self._prepared = True

        async def aget_tuple(self, config: Any) -> Any:
            await self._prepare()
            return await AsyncRedisSaver.aget_tuple(self, config)

        async def alist(self, config: Any, **kwargs: Any) -> Any:
            await self._prepare()
            async for item in AsyncRedisSaver.alist(self, config, **kwargs):
                yield item

        async def aput(self, config: Any, *args: Any, **kwargs: Any) -> Any:
            await self._prepare()
            return await AsyncRedisSaver.aput(self, config, *args, **kwargs)

        async def aput_writes(self, config: Any, *args: Any, **kwargs: Any) -> Any:
            await self._prepare()
            return await AsyncRedisSaver.aput_writes(self, config, *args, **kwargs)

        async def aclose(self) -> None:
            """Release the client this saver opened — 여는 쪽이 닫는다.

            라이브러리의 `__aexit__` 에 위임한다: 커넥션 풀 해제와 redisvl
            인덱스의 클라이언트 참조 정리까지 거기 들어 있어, 직접
            `aclose()` 만 부르면 그 둘이 빠진다.
            """
            await AsyncRedisSaver.__aexit__(self, None, None, None)
            self._prepared = False

    return DeferredAsyncRedisSaver()


def _deferred_postgres_saver(url: str) -> Any:
    """Build an async Postgres saver whose schema is prepared on first use.

    ``build_checkpointer`` 는 동기이고 CLI 는 ``asyncio.run`` 밖에서 부른다 —
    커넥션 열기와 ``setup()`` 은 모두 비동기라 여기서 미룬다.

    LangGraph 는 checkpointer 를 **isinstance 로 검사**하므로 덕타이핑 래퍼는
    통하지 않는다 — 실제 saver 를 상속한다.
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    from psycopg import AsyncConnection
    from psycopg.rows import dict_row

    class DeferredAsyncPostgresSaver(AsyncPostgresSaver):
        """첫 사용 시 커넥션을 열고 스키마를 준비하는 saver."""

        def __init__(self) -> None:
            self._url = url
            self._prepared = False
            self._lock = asyncio.Lock()
            # 부모 생성자는 커넥션을 요구한다 — 준비 전에는 넘길 것이 없다
            self.conn = None  # type: ignore[assignment]
            self.pipe = None
            self.serde = AsyncPostgresSaver.serde

        async def _prepare(self) -> None:
            if self._prepared:
                return
            async with self._lock:
                if self._prepared:
                    return
                connection = await AsyncConnection.connect(
                    self._url, autocommit=True, row_factory=dict_row
                )
                try:
                    AsyncPostgresSaver.__init__(self, conn=connection)
                    await AsyncPostgresSaver.setup(self)
                except BaseException:
                    # setup 이 실패하면 이 커넥션은 주인을 잃는다 — 재시도마다
                    # 하나씩 쌓여, 스키마가 안 만들어지는 것보다 먼저 커넥션
                    # 상한에 부딪힌다
                    await connection.close()
                    self.conn = None  # type: ignore[assignment]
                    raise
                self._prepared = True

        async def aget_tuple(self, config: Any) -> Any:
            await self._prepare()
            return await AsyncPostgresSaver.aget_tuple(self, config)

        async def alist(self, config: Any, **kwargs: Any) -> Any:
            await self._prepare()
            async for item in AsyncPostgresSaver.alist(self, config, **kwargs):
                yield item

        async def aput(self, config: Any, *args: Any, **kwargs: Any) -> Any:
            await self._prepare()
            return await AsyncPostgresSaver.aput(self, config, *args, **kwargs)

        async def aput_writes(self, config: Any, *args: Any, **kwargs: Any) -> Any:
            await self._prepare()
            return await AsyncPostgresSaver.aput_writes(self, config, *args, **kwargs)

        async def aclose(self) -> None:
            """Close the connection this saver opened.

            **여는 쪽이 닫는다.** 커넥션은 `_prepare` 안에서 이 객체가 열었고
            밖에서 넘겨받은 것이 아니므로, 정리 책임도 여기 있다. 한 번도
            쓰이지 않아 아직 열지 않았다면 할 일이 없다.

            상주 프로세스(service run)는 이것을 부르지 않으면 커넥션을 물고
            있는다 — CLI 단발 실행은 프로세스 종료로 정리되지만 그것에
            기대면 소유자가 코드에 드러나지 않는다.
            """
            if not self._prepared:
                return
            async with self._lock:
                if not self._prepared:
                    return
                await self.conn.close()
                self._prepared = False

    return DeferredAsyncPostgresSaver()


async def close_checkpointer(checkpointer: BaseCheckpointSaver[Any]) -> None:
    """Release whatever the checkpointer holds.

    checkpointer 가 쥔 자원을 놓아줍니다. 백엔드마다 정리할 것이 다르고
    (in-memory 는 아무 것도 없다) 호출자는 어느 백엔드인지 알 필요가 없어야
    하므로, 그 분기를 여기서 흡수합니다.

    Args:
        checkpointer: The checkpointer built by `build_checkpointer`.
    """
    closer = getattr(checkpointer, "aclose", None)
    if closer is None:
        return
    await closer()


async def _guarded(
    action: Callable[[], Any],
    *,
    code: ErrorCode,
    message: str,
    operation: str,
    graph: str,
    run_id: str,
    metrics: Metrics | None,
) -> Any:
    """저장/복원 공통 경로 — 실패를 구조화하고 결과를 계측한다."""
    telemetry = None if metrics is None else CheckpointTelemetry(metrics)
    try:
        result = await action()
    except AaestroError:
        if telemetry is not None:
            telemetry.operation(operation=operation, status=STATUS_FAILED)
        raise
    except Exception as err:
        if telemetry is not None:
            telemetry.operation(operation=operation, status=STATUS_FAILED)
        raise _storage_error(code, message, graph=graph, run_id=run_id) from err

    if telemetry is not None:
        telemetry.operation(operation=operation, status=STATUS_COMPLETED)
    return result


async def guarded_save(
    save: Callable[[], Any],
    *,
    graph: str,
    run_id: str,
    metrics: Metrics | None = None,
) -> Any:
    """Run a checkpoint save, converting failures to ``STOR_001``.

    Checkpoint 저장을 수행하고 실패를 ``STOR_001`` 로 변환합니다.

    Raises:
        AaestroError: STORAGE/``STOR_001`` if the save fails.
    """
    return await _guarded(
        save,
        code=ErrorCode.STOR_001,
        message="checkpoint save failed",
        operation=OPERATION_SAVE,
        graph=graph,
        run_id=run_id,
        metrics=metrics,
    )


async def guarded_restore(
    restore: Callable[[], Any],
    *,
    graph: str,
    run_id: str,
    metrics: Metrics | None = None,
) -> Any:
    """Run a checkpoint restore, converting failures to ``STOR_002``.

    Checkpoint 복원을 수행하고 실패를 ``STOR_002`` 로 변환합니다.

    Raises:
        AaestroError: STORAGE/``STOR_002`` if the restore fails.
    """
    return await _guarded(
        restore,
        code=ErrorCode.STOR_002,
        message="checkpoint restore failed",
        operation=OPERATION_LOAD,
        graph=graph,
        run_id=run_id,
        metrics=metrics,
    )
