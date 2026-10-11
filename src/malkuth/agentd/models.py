"""Model chains — primary plus declared fallbacks, with per-model circuit breakers.

모델 체인. primary 의 재시도가 소진되면 선언된 fallback 으로 **턴 단위** 전환한다.

다음 모델을 고르는 것도 재시도의 일부라 같은 계층(agentd)이 맡는다 (05 Retry Layering) —
오케스트레이터의 노드 재시도가 같은 모델로 태스크를 다시 보내면 rate limit 중인 provider 를
계속 두드린다 (#359).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

import structlog

from malkuth.core.errors import (
    NETWORK_RETRY,
    RATE_LIMIT_RETRY,
    CircuitBreaker,
    ErrorCategory,
    ErrorCode,
    MalkuthError,
    RetryPolicy,
)
from malkuth.core.manifest import FallbackTrigger, ModelConfig
from malkuth.observability.circuit import CircuitTelemetry
from malkuth.resilience import retrying_any

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from malkuth.agentd.executor import Model, ModelResponse
    from malkuth.core.manifest import RetryDeclaration
    from malkuth.observability.metrics import Metrics

    ModelFactory = Callable[[ModelConfig], Model]
    """(provider, name, max_tokens, effort) 로 어댑터를 만든다.

    지원하지 않는 provider 는 CFG_001 을 올린다."""

log = structlog.get_logger(__name__)

MODEL_RETRY_POLICIES: Final = (RATE_LIMIT_RETRY, NETWORK_RETRY)
"""모델 호출이 내는 두 실패에 각각의 backoff — rate limit 을 1초 간격으로
두드리면 상황이 악화된다. 순서가 곧 우선순위다.

**기본값이 아니다**: 재시도는 실제로 기다리므로, 조립하는 쪽(`build_executor`)
이 명시적으로 켠다. 기본으로 켜면 fake model 로 실패를 스크립트하는 모든
테스트가 초 단위로 기다리게 된다 (06 Async 2).
"""

RATE_LIMIT: Final[FallbackTrigger] = "rate_limit"
SERVER_ERROR: Final[FallbackTrigger] = "server_error"
CONTEXT_OVERFLOW: Final[FallbackTrigger] = "context_overflow"

_PROVIDER_FAULTS: Final[frozenset[FallbackTrigger]] = frozenset({RATE_LIMIT, SERVER_ERROR})
"""breaker 가 실패로 세는 부류 — provider 가 답하지 못한 것만.

context 초과(LLM_002)는 요청이 너무 큰 것이고 LLM_004 는 응답 해석 실패라, provider 가 멀쩡한데
breaker 가 열리면 건강한 모델을 건너뛰게 된다.
"""


def trigger_of(err: BaseException) -> FallbackTrigger | None:
    """Classify a model failure into the fallback trigger it matches.

    모델 실패를 fallback 선언의 ``on`` 어휘로 분류합니다. 전환 대상이 아닌 실패
    (LLM_004, 설정 오류 등)는 None 입니다 — 다른 모델로 보내도 같은 결과입니다.
    """
    if not isinstance(err, MalkuthError):
        return None
    if err.category is ErrorCategory.RATE_LIMIT:
        return RATE_LIMIT
    if err.code == ErrorCode.LLM_002:
        return CONTEXT_OVERFLOW
    # provider 가 답하지 못한 것은 전부 server_error 부류다 — 연결 실패와 5xx 를 가를 이유가 없다
    if err.code == ErrorCode.LLM_003 or err.category in (
        ErrorCategory.NETWORK,
        ErrorCategory.TIMEOUT,
    ):
        return SERVER_ERROR
    return None


@dataclass(frozen=True)
class ModelLink:
    """One model in a chain — the adapter and what lets the chain reach it.

    체인의 한 칸. ``index`` 0 이 primary 이고, ``on`` 은 fallback 에만 의미가 있다.
    ``provider`` / ``name`` 이 빈 문자열이면 라벨을 모르는 단일 모델이다 (생성자 기본 라벨을 쓴다).
    """

    model: Model
    provider: str
    name: str
    index: int = 0
    on: frozenset[FallbackTrigger] = frozenset()
    breaker: CircuitBreaker | None = None


@dataclass(frozen=True)
class ModelChain:
    """Primary and fallbacks, tried in order for one model turn.

    한 턴을 primary 부터 순서대로 시도한다. 태스크는 시작할 때 체인 하나를 잡아 끝까지 쓴다.
    """

    links: tuple[ModelLink, ...]
    retry_policies: tuple[RetryPolicy, ...] = ()

    async def turn(
        self,
        call: Callable[[ModelLink], Awaitable[ModelResponse]],
        *,
        sleep: Callable[[float], Awaitable[None]] | None,
        agent: str,
    ) -> ModelResponse:
        """Run one model turn across the chain.

        각 링크는 자기 재시도 정책을 다 쓴 뒤에야 다음으로 넘어간다. fallback 은 직전 실패의
        부류가 그 ``on`` 에 있을 때만 시도한다.

        circuit 이 열린 모델은 호출하지 않고, 그 자리를 **breaker 가 냈을 실패**
        (MODEL/``LLM_003``, 곧 ``server_error``) 로 채운다 — primary 가 열려 있을 때
        ``server_error`` fallback 이 이어받게 하기 위해서다. 앞선 실제 실패가 있으면 그것을 남긴다.

        Raises:
            MalkuthError: The last failure when no link succeeds — 모든 후보가 열려 있었다면
                MODEL/``LLM_003`` ``model circuit open``. 전환 대상이 아닌 실패는 즉시 올린다.
        """
        last: MalkuthError | None = None
        for position, link in enumerate(self.links):
            if link.index > 0 and (last is None or trigger_of(last) not in link.on):
                continue
            if link.breaker is not None and not link.breaker.can_attempt():
                log.debug(
                    "model circuit open, skipping",
                    agent=agent,
                    model=link.name,
                    provider=link.provider,
                    fallback_index=link.index,
                )
                last = last or _circuit_open(link.breaker, agent=agent)
                continue
            try:
                return await retrying_any(
                    self.retry_policies,
                    _guarded(call, link),
                    sleep=sleep,
                    agent=agent,
                    model=link.name,
                    provider=link.provider,
                )
            except MalkuthError as err:
                if trigger_of(err) is None:
                    raise
                last = err
                self._warn_falling_back(link, err, position=position, agent=agent)
        # 링크가 하나 이상이고 primary 는 항상 후보라, 여기 오면 last 는 채워져 있다
        if last is None:  # pragma: no cover
            raise AssertionError("model chain has no links")
        raise last

    def _warn_falling_back(
        self, link: ModelLink, err: MalkuthError, *, position: int, agent: str
    ) -> None:
        """전환 WARN — 어느 모델에서 어디로, 원인 코드. 다음 후보가 없으면 그 사실만 남긴다."""
        fields: dict[str, object] = {
            "agent": agent,
            "model": link.name,
            "provider": link.provider,
            "fallback_index": link.index,
            "error_code": err.code,
        }
        trigger = trigger_of(err)
        upcoming = next(
            (later for later in self.links[position + 1 :] if trigger in later.on), None
        )
        if upcoming is not None:
            fields["next_model"] = upcoming.name
            fields["next_provider"] = upcoming.provider
        log.warning("model call failed, falling back", **fields)


def _circuit_open(breaker: CircuitBreaker, *, agent: str) -> MalkuthError:
    """열린 breaker 가 ``call`` 로 막았다면 냈을 실패 — 같은 카테고리·코드다."""
    return MalkuthError(
        category=breaker.open_category,
        code=breaker.open_code,
        message="model circuit open",
        agent=agent,
        retryable=True,
        details={"target": breaker.target},
    )


def _guarded(
    call: Callable[[ModelLink], Awaitable[ModelResponse]], link: ModelLink
) -> Callable[[], Awaitable[ModelResponse]]:
    """시도 하나를 breaker 에 기록한다 — 재시도 **안쪽**이라 시도마다 센다."""

    async def attempt() -> ModelResponse:
        try:
            response = await call(link)
        except MalkuthError as err:
            if link.breaker is not None and trigger_of(err) in _PROVIDER_FAULTS:
                link.breaker.record_failure()
            raise
        if link.breaker is not None:
            link.breaker.record_success()
        return response

    return attempt


def _policy(declared: RetryDeclaration | None, base: RetryPolicy) -> RetryPolicy:
    """선언 하나를 실행 정책으로 — 상한을 생략하면 그 부류의 기본 상한을 쓴다."""
    if declared is None:
        return base
    return RetryPolicy(
        max_attempts=declared.attempts,
        initial_delay_s=declared.start_s,
        max_delay_s=declared.max_s or base.max_delay_s,
        retryable_categories=base.retryable_categories,
    )


@dataclass
class ModelChains:
    """Builds and caches model chains from resolved model declarations.

    해석된 모델 선언(``ModelConfig``)으로 체인을 만든다. agentd 는 manifest 를 다시 읽지 않는다 —
    태스크가 실어 온 선언이든 manifest 기본값이든 같은 길로 체인이 된다.

    breaker 는 ``(provider, name)`` 하나에 하나다: 같은 모델이 여러 체인에 나와도 provider
    상태는 하나이기 때문이다. 같은 모델에 다른 circuit 임계값이 오면 먼저 만든 것을 쓴다.
    """

    agent: str
    factory: ModelFactory
    metrics: Metrics | None = None
    default_policies: tuple[RetryPolicy, ...] = MODEL_RETRY_POLICIES
    clock: Callable[[], float] | None = None
    _chains: dict[str, ModelChain] = field(default_factory=dict, init=False)
    _breakers: dict[tuple[str, str], CircuitBreaker] = field(default_factory=dict, init=False)

    def for_config(self, config: ModelConfig) -> ModelChain:
        """Return the chain for a resolved declaration, building it once.

        Raises:
            MalkuthError: CONFIG/``CFG_001`` if the factory binds no adapter for a provider.
        """
        key = config.model_dump_json()
        cached = self._chains.get(key)
        if cached is not None:
            return cached
        primary = ModelLink(
            model=self.factory(config),
            provider=config.provider,
            name=config.name,
            breaker=self._breaker(config, config.provider, config.name),
        )
        fallbacks = tuple(
            ModelLink(
                # fallback 은 출력 상한과 effort 를 primary 에서 물려받는다
                model=self.factory(
                    ModelConfig(
                        provider=fallback.provider,
                        name=fallback.name,
                        max_tokens=config.max_tokens,
                        effort=config.effort,
                    )
                ),
                provider=fallback.provider,
                name=fallback.name,
                index=index,
                on=frozenset(fallback.on),
                breaker=self._breaker(config, fallback.provider, fallback.name),
            )
            for index, fallback in enumerate(config.fallbacks, start=1)
        )
        chain = ModelChain(links=(primary, *fallbacks), retry_policies=self._policies(config))
        self._chains[key] = chain
        return chain

    def _policies(self, config: ModelConfig) -> tuple[RetryPolicy, ...]:
        if config.retry is None:
            return self.default_policies
        return (
            _policy(config.retry.rate_limit, RATE_LIMIT_RETRY),
            _policy(config.retry.network, NETWORK_RETRY),
        )

    def _breaker(self, config: ModelConfig, provider: str, name: str) -> CircuitBreaker | None:
        """선언이 있을 때만 — 이미 있는 (provider, name) 이면 그것을 공유한다.

        circuit 을 선언하지 않은 체인에는 공유 breaker 도 걸지 않는다 — 선언이 없는데 건너뛰면
        운영자가 선언에서 원인을 찾지 못한다.
        """
        if config.circuit is None:
            return None
        existing = self._breakers.get((provider, name))
        if existing is not None:
            return existing
        target = f"model:{provider}/{name}"
        observer = (
            CircuitTelemetry(self.metrics, target=target) if self.metrics is not None else None
        )
        breaker = CircuitBreaker(
            max_failures=config.circuit.max_failures,
            reset_timeout_s=config.circuit.reset_s,
            target=target,
            open_category=ErrorCategory.MODEL,
            open_code=ErrorCode.LLM_003,
            on_transition=observer.observe if observer is not None else None,
        )
        if self.clock is not None:
            breaker.clock = self.clock
        self._breakers[(provider, name)] = breaker
        return breaker


def unsupported_provider(agent: str, provider: str) -> MalkuthError:
    """팩토리가 묶지 못한 provider — 조용히 대역으로 떨어지면 운영에서 가짜 응답이 나간다."""
    return MalkuthError(
        category=ErrorCategory.CONFIG,
        code=ErrorCode.CFG_001,
        message="no model provider bound for this provider",
        agent=agent,
        details={"provider": provider},
    )


__all__ = [
    "MODEL_RETRY_POLICIES",
    "ModelChain",
    "ModelChains",
    "ModelLink",
    "trigger_of",
    "unsupported_provider",
]
