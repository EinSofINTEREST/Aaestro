"""Model fallback chain wiring (#359).

primary 의 재시도가 소진되면 선언된 fallback 으로 턴 단위 전환하고, circuit 이 열린 모델은
부르지 않으며, 태스크가 실어 온 모델 선언(#358)을 시작 시 고정해 쓴다.

06 에 따라 실제로 자지 않는다 — 재시도 대기와 breaker 시계를 주입한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry
from structlog.testing import capture_logs

from aaestro.agentd.executor import Executor, ExecutorConfig, ExecutorServices
from aaestro.agentd.models import ModelChains, trigger_of
from aaestro.agentd.telemetry import ExecutorTelemetry
from aaestro.core.agent import TaskConfig, TaskStatus
from aaestro.core.errors import (
    RATE_LIMIT_RETRY,
    AaestroError,
    ErrorCategory,
    ErrorCode,
)
from aaestro.core.manifest import (
    CircuitDeclaration,
    FallbackModel,
    ModelConfig,
    ModelRetry,
    RetryDeclaration,
)
from aaestro.observability.metrics import Metrics
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, text

REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT = "researcher"
PROVIDER = "anthropic"
ATTEMPTS = 3


def model_error(
    category: ErrorCategory, code: ErrorCode, *, retryable: bool = True
) -> AaestroError:
    return AaestroError(
        category=category, code=code, message="provider failed", retryable=retryable
    )


def rate_limited() -> AaestroError:
    return model_error(ErrorCategory.RATE_LIMIT, ErrorCode.LLM_001)


def server_error() -> AaestroError:
    # anthropic 어댑터가 5xx · 연결 실패에 내는 모양 — MODEL 카테고리라 재시도 정책이 집지 않는다
    return model_error(ErrorCategory.MODEL, ErrorCode.LLM_003)


def context_overflow() -> AaestroError:
    return model_error(ErrorCategory.MODEL, ErrorCode.LLM_002, retryable=False)


def declared(
    *fallbacks: FallbackModel,
    name: str = "primary",
    circuit: CircuitDeclaration | None = None,
    **extra: object,
) -> ModelConfig:
    return ModelConfig.model_validate(
        {
            "provider": PROVIDER,
            "name": name,
            "fallbacks": fallbacks,
            # 재시도 상한을 줄여 primary 의 시도 횟수를 정확히 센다
            "retry": ModelRetry(rate_limit=RetryDeclaration(start_s=1, attempts=ATTEMPTS)),
            "circuit": circuit,
            **extra,
        }
    )


def backup(*on: str, name: str = "backup") -> FallbackModel:
    return FallbackModel.model_validate({"provider": PROVIDER, "name": name, "on": on})


class Clock:
    """breaker 시계 — 실제 시간을 기다리지 않고 앞으로 민다."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class Rig:
    """이름으로 FakeModel 을 고르는 팩토리 위에 실제 조립과 같은 배선을 얹는다."""

    def __init__(self, fakes: dict[str, FakeModel], config: ModelConfig) -> None:
        self.fakes = fakes
        self.waits: list[float] = []
        self.built: list[ModelConfig] = []
        self.clock = Clock()
        self.metrics = Metrics(registry=CollectorRegistry())
        self.chains = ModelChains(
            agent=AGENT, factory=self._factory, metrics=self.metrics, clock=self.clock
        )

        async def sleep(delay: float) -> None:
            self.waits.append(delay)

        self.executor = Executor(
            agent=AGENT,
            model=self.chains.for_config(config),
            tools=FakeTools(),
            render=lambda _task: "prompt",
            config=ExecutorConfig(retry_sleep=sleep),
            services=ExecutorServices(
                chains=self.chains.for_config,
                telemetry=ExecutorTelemetry(
                    self.metrics, agent=AGENT, provider=PROVIDER, model=config.name
                ),
            ),
        )

    def _factory(self, config: ModelConfig) -> FakeModel:
        if config.provider != PROVIDER:
            raise AaestroError(
                category=ErrorCategory.CONFIG,
                code=ErrorCode.CFG_001,
                message="no model provider bound for this provider",
                details={"provider": config.provider},
            )
        self.built.append(config)
        return self.fakes[config.name]

    def requests(self, *, model: str, status: str, fallback_index: int) -> float:
        labels = {
            "agent": AGENT,
            "provider": PROVIDER,
            "model": model,
            "status": status,
            "fallback_index": str(fallback_index),
        }
        return self.metrics.registry.get_sample_value("aaestro_model_requests_total", labels) or 0.0

    def circuit(self, model: str) -> float | None:
        return self.metrics.registry.get_sample_value(
            "aaestro_circuit_state", {"target": f"model:{PROVIDER}/{model}"}
        )


# --- 전환 조건 ---------------------------------------------------------------


async def test_exhausted_rate_limit_falls_back_and_counts_both():
    """primary 가 attempts 만큼 rate limit 을 내면 fallback 이 답한다 — 두 쪽 모두 계수된다."""
    primary = FakeModel([rate_limited()])
    fallback = FakeModel([text("from backup", input_tokens=7, output_tokens=3)])
    rig = Rig({"primary": primary, "backup": fallback}, declared(backup("rate_limit")))

    result = await rig.executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert result.output == {"content": "from backup"}
    assert primary.turns == ATTEMPTS
    assert fallback.turns == 1
    assert len(rig.waits) == ATTEMPTS - 1
    assert rig.requests(model="primary", status="rate_limited", fallback_index=0) == ATTEMPTS
    assert rig.requests(model="backup", status="completed", fallback_index=1) == 1.0


async def test_usage_and_token_labels_follow_the_model_that_answered():
    """라벨이 manifest 값에 고정되면 fallback 이 쓴 토큰이 primary 에 잡힌다."""
    rig = Rig(
        {"primary": FakeModel([rate_limited()]), "backup": FakeModel([text("ok", input_tokens=7)])},
        declared(backup("rate_limit")),
    )

    result = await rig.executor.execute(make_task())

    assert result.usage.model == "backup"
    assert result.usage.provider == PROVIDER
    tokens = rig.metrics.registry.get_sample_value(
        "aaestro_model_tokens_total", {"agent": AGENT, "model": "backup", "direction": "input"}
    )
    assert tokens == 7.0


async def test_fallback_is_not_tried_for_an_undeclared_trigger():
    """``server_error`` 만 선언했는데 rate limit 이면 전환하지 않고 지금처럼 실패한다."""
    fallback = FakeModel([text("unused")])
    rig = Rig(
        {"primary": FakeModel([rate_limited()]), "backup": fallback},
        declared(backup("server_error")),
    )

    result = await rig.executor.execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_001
    assert fallback.turns == 0


async def test_context_overflow_switches_once_without_retrying():
    """LLM_002 는 재시도해도 같은 길이다 — 대기 없이 곧장 더 긴 컨텍스트의 모델로."""
    primary = FakeModel([context_overflow()])
    fallback = FakeModel([text("long context")])
    rig = Rig({"primary": primary, "backup": fallback}, declared(backup("context_overflow")))

    result = await rig.executor.execute(make_task())

    assert result.status is TaskStatus.COMPLETED
    assert primary.turns == 1
    assert fallback.turns == 1
    assert rig.waits == []


async def test_context_overflow_without_a_fallback_fails_as_before():
    rig = Rig({"primary": FakeModel([context_overflow()])}, declared())

    result = await rig.executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_002


async def test_a_failure_no_fallback_can_fix_is_raised_immediately():
    """LLM_004 는 응답 해석 실패다 — 다른 모델로 보낼 실패가 아니다."""
    fallback = FakeModel([text("unused")])
    broken = model_error(ErrorCategory.MODEL, ErrorCode.LLM_004, retryable=False)
    rig = Rig(
        {"primary": FakeModel([broken]), "backup": fallback},
        declared(backup("rate_limit", "server_error", "context_overflow")),
    )

    result = await rig.executor.execute(make_task())

    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_004
    assert fallback.turns == 0


async def test_the_second_fallback_takes_over_when_the_first_also_fails():
    first = FakeModel([rate_limited()])
    second = FakeModel([text("second")])
    rig = Rig(
        {"primary": FakeModel([rate_limited()]), "first": first, "second": second},
        declared(backup("rate_limit", name="first"), backup("rate_limit", name="second")),
    )

    result = await rig.executor.execute(make_task())

    assert result.output == {"content": "second"}
    assert first.turns == ATTEMPTS
    assert rig.requests(model="second", status="completed", fallback_index=2) == 1.0


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (rate_limited(), "rate_limit"),
        (context_overflow(), "context_overflow"),
        (server_error(), "server_error"),
        (model_error(ErrorCategory.NETWORK, ErrorCode.NET_001), "server_error"),
        (model_error(ErrorCategory.TIMEOUT, ErrorCode.NET_002), "server_error"),
        (model_error(ErrorCategory.MODEL, ErrorCode.LLM_004, retryable=False), None),
        (model_error(ErrorCategory.CONFIG, ErrorCode.CFG_001, retryable=False), None),
        (RuntimeError("not ours"), None),
    ],
)
def test_trigger_of_classifies_failures(err, expected):
    assert trigger_of(err) == expected


# --- circuit breaker --------------------------------------------------------


OPEN_AFTER_ONE = CircuitDeclaration(max_failures=1, reset_s=60)


async def test_an_open_circuit_skips_the_model_without_calling_it():
    """열린 모델은 호출 0 회 — 다음 태스크는 곧장 server_error fallback 으로 간다."""
    primary = FakeModel([server_error()])
    fallback = FakeModel([text("backup")])
    rig = Rig(
        {"primary": primary, "backup": fallback},
        declared(backup("server_error"), circuit=OPEN_AFTER_ONE),
    )

    await rig.executor.execute(make_task(task_id="opens"))
    assert primary.turns == 1
    assert rig.circuit("primary") == 1.0

    result = await rig.executor.execute(make_task(task_id="skips"))

    assert result.status is TaskStatus.COMPLETED
    assert primary.turns == 1, "열린 모델을 다시 불렀다"
    assert fallback.turns == 2


async def test_an_open_circuit_with_nowhere_to_go_fails_with_circuit_open():
    primary = FakeModel([server_error()])
    rig = Rig({"primary": primary}, declared(circuit=OPEN_AFTER_ONE))

    await rig.executor.execute(make_task(task_id="opens"))
    result = await rig.executor.execute(make_task(task_id="blocked"))

    assert primary.turns == 1
    assert result.error is not None
    assert result.error.code == ErrorCode.LLM_003
    assert result.error.message == "model circuit open"
    assert result.error.retryable is True


async def test_the_circuit_lets_a_probe_through_after_the_reset_timeout():
    primary = FakeModel([server_error(), text("recovered")])
    rig = Rig({"primary": primary}, declared(circuit=OPEN_AFTER_ONE))

    await rig.executor.execute(make_task(task_id="opens"))
    rig.clock.now = OPEN_AFTER_ONE.reset_s

    result = await rig.executor.execute(make_task(task_id="probe"))

    assert result.output == {"content": "recovered"}
    assert rig.circuit("primary") == 0.0


async def test_context_overflow_does_not_open_the_circuit():
    """요청이 큰 것은 provider 장애가 아니다 — 건강한 모델을 건너뛰게 된다."""
    primary = FakeModel([context_overflow(), text("short")])
    rig = Rig({"primary": primary}, declared(circuit=OPEN_AFTER_ONE))

    await rig.executor.execute(make_task(task_id="too-long"))
    result = await rig.executor.execute(make_task(task_id="fits"))

    assert result.output == {"content": "short"}
    assert primary.turns == 2


async def test_rate_limit_retries_count_against_the_circuit():
    """breaker 는 시도마다 센다 — 재시도로 가려지면 provider 압박이 보이지 않는다."""
    circuit = CircuitDeclaration(max_failures=ATTEMPTS, reset_s=60)
    rig = Rig({"primary": FakeModel([rate_limited()])}, declared(circuit=circuit))

    await rig.executor.execute(make_task())

    assert rig.circuit("primary") == 1.0


async def test_no_circuit_declaration_means_no_breaker():
    primary = FakeModel([server_error(), text("again")])
    rig = Rig({"primary": primary}, declared())

    await rig.executor.execute(make_task(task_id="a"))
    result = await rig.executor.execute(make_task(task_id="b"))

    assert result.output == {"content": "again"}
    assert rig.circuit("primary") is None


# --- 태스크가 실어 온 모델 선언 (#358) ------------------------------------------


async def test_the_task_model_declaration_is_used_from_the_primary_call():
    """노드 · 배포 override 가 해석한 모델이 첫 호출부터 쓰인다 — manifest 기본값이 아니라."""
    manifest_default = FakeModel([text("default")])
    override = FakeModel([rate_limited()])
    override_backup = FakeModel([text("override backup")])
    rig = Rig(
        {"primary": manifest_default, "override": override, "spare": override_backup},
        declared(),
    )
    task_model = declared(backup("rate_limit", name="spare"), name="override", max_tokens=512)

    result = await rig.executor.execute(make_task(config=TaskConfig(model=task_model)))

    assert result.output == {"content": "override backup"}
    assert manifest_default.turns == 0
    assert override.turns == ATTEMPTS
    assert rig.requests(model="spare", status="completed", fallback_index=1) == 1.0


async def test_a_task_without_a_declaration_uses_the_manifest_default():
    manifest_default = FakeModel([text("default")])
    rig = Rig({"primary": manifest_default}, declared())

    result = await rig.executor.execute(make_task())

    assert result.output == {"content": "default"}
    assert result.usage.model == "primary"


async def test_fallbacks_inherit_output_limits_from_the_primary():
    rig = Rig(
        {"primary": FakeModel([text("x")]), "backup": FakeModel([text("y")])},
        declared(backup("rate_limit"), max_tokens=256, effort="low"),
    )

    by_name = {config.name: config for config in rig.built}
    assert by_name["backup"].max_tokens == 256
    assert by_name["backup"].effort == "low"
    assert by_name["backup"].fallbacks == ()


async def test_an_unbound_provider_in_the_task_declaration_fails_that_task():
    rig = Rig({"primary": FakeModel([text("x")])}, declared())
    foreign = ModelConfig(provider="openai", name="gpt")

    result = await rig.executor.execute(make_task(config=TaskConfig(model=foreign)))

    assert result.error is not None
    assert result.error.code == ErrorCode.CFG_001


async def test_a_declaration_without_a_factory_is_not_silently_ignored():
    """팩토리가 없으면 기본 체인으로 돌되 WARN 을 남긴다 — override 가 왜 안 먹는지 보이게."""
    model = FakeModel([text("default")])
    executor = Executor(agent=AGENT, model=model, tools=FakeTools(), render=lambda _t: "p")

    with capture_logs() as logs:
        result = await executor.execute(make_task(config=TaskConfig(model=declared())))

    assert result.status is TaskStatus.COMPLETED
    assert any(
        entry["event"] == "task model declaration ignored, no model factory bound"
        and entry["log_level"] == "warning"
        for entry in logs
    )


async def test_the_retry_declaration_shapes_the_backoff():
    """선언하지 않은 부류는 기본 정책 — rate limit 은 선언대로, 상한은 기본값."""
    rig = Rig({"primary": FakeModel([text("x")])}, declared())

    policies = rig.executor._chain.retry_policies

    assert policies[0].max_attempts == ATTEMPTS
    assert policies[0].initial_delay_s == 1
    assert policies[0].max_delay_s == RATE_LIMIT_RETRY.max_delay_s


async def test_chains_are_built_once_per_declaration_and_share_breakers():
    rig = Rig(
        {"primary": FakeModel([text("x")]), "backup": FakeModel([text("y")])},
        declared(backup("rate_limit"), circuit=OPEN_AFTER_ONE),
    )
    config = declared(backup("rate_limit"), circuit=OPEN_AFTER_ONE)
    swapped = declared(
        FallbackModel(provider=PROVIDER, name="primary", on=("rate_limit",)),
        name="backup",
        circuit=OPEN_AFTER_ONE,
    )

    first = rig.chains.for_config(config)
    again = rig.chains.for_config(config)
    other = rig.chains.for_config(swapped)

    assert first is again
    assert first.links[0].breaker is other.links[1].breaker


# --- 실제 조립 ---------------------------------------------------------------


async def test_the_production_assembly_builds_the_declared_chain(monkeypatch):
    """조립이 manifest 선언으로 체인을 만들고, 태스크 선언용 팩토리를 함께 건다."""
    from aaestro.agentd.__main__ import build_executor, load_manifest

    monkeypatch.delenv("AAESTRO_EXECUTOR", raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))
    manifest = load_manifest(REPO_ROOT / "agents/researcher/manifest.yaml")
    model = manifest.spec.model.model_copy(
        update={"fallbacks": (backup("rate_limit", name="claude-backup-1"),)}
    )
    manifest = manifest.model_copy(
        update={"spec": manifest.spec.model_copy(update={"model": model})}
    )

    built = await build_executor(manifest)

    assert len(built._chain.links) == 1 + len(model.fallbacks)
    assert [link.name for link in built._chain.links] == [model.name, "claude-backup-1"]
    assert built._services.chains is not None
    task_chain = built._services.chains(ModelConfig(provider=PROVIDER, name="claude-other"))
    assert task_chain.links[0].name == "claude-other"
    with pytest.raises(AaestroError) as exc_info:
        built._services.chains(ModelConfig(provider="openai", name="gpt"))
    assert exc_info.value.code == ErrorCode.CFG_001


# --- circuit 이 연 부류 · 재시도 중 열림 (#359 QA) ----------------------------------


async def test_a_circuit_opened_by_rate_limits_hands_over_to_a_rate_limit_fallback():
    """rate limit 으로 열린 primary 는 rate limit 부류로 채워진다.

    ``server_error`` 로 채우면 ``on: [rate_limit]`` fallback 이 후보에서 빠진다.
    """
    circuit = CircuitDeclaration(max_failures=3, reset_s=60)
    primary = FakeModel([rate_limited()])
    spare = FakeModel([text("from backup")])
    rig = Rig(
        {"primary": primary, "backup": spare},
        declared(backup("rate_limit"), circuit=circuit),
    )

    first = await rig.executor.execute(make_task(task_id="first"))
    assert first.output == {"content": "from backup"}
    assert rig.circuit("primary") == 1.0
    called = primary.turns

    second = await rig.executor.execute(make_task(task_id="second"))

    assert second.output == {"content": "from backup"}
    assert primary.turns == called, "열린 모델을 다시 불렀다"
    assert spare.turns == 2


async def test_a_circuit_opened_by_server_errors_still_fills_in_as_server_error():
    primary = FakeModel([server_error()])
    rig = Rig({"primary": primary}, declared(circuit=OPEN_AFTER_ONE))

    await rig.executor.execute(make_task(task_id="opens"))
    result = await rig.executor.execute(make_task(task_id="blocked"))

    assert result.error is not None
    assert result.error.category == ErrorCategory.MODEL
    assert result.error.details["opened_by"] == "server_error"


async def test_a_circuit_opening_mid_retry_stops_the_remaining_attempts():
    """열린 뒤의 남은 시도는 보내지 않는다 — 열린 provider 를 계속 두드리지 않고 곧장 전환한다."""
    circuit = CircuitDeclaration(max_failures=2, reset_s=60)
    primary = FakeModel([rate_limited()])
    spare = FakeModel([text("from backup")])
    rig = Rig(
        {"primary": primary, "backup": spare},
        declared(
            backup("rate_limit"),
            circuit=circuit,
            retry=ModelRetry(rate_limit=RetryDeclaration(start_s=10, attempts=5)),
        ),
    )

    result = await rig.executor.execute(make_task())

    assert result.output == {"content": "from backup"}
    assert primary.turns == 2
    assert len(rig.waits) == 1
    assert spare.turns == 1


async def test_a_declared_network_retry_runs_before_falling_back():
    """어댑터가 NET_001 을 내면 선언 retry.network 의 1초 계열로 기다린 뒤 전환한다."""
    unreachable = model_error(ErrorCategory.NETWORK, ErrorCode.NET_001)
    primary = FakeModel([unreachable])
    spare = FakeModel([text("from backup")])
    rig = Rig(
        {"primary": primary, "backup": spare},
        declared(
            backup("server_error"),
            retry=ModelRetry(network=RetryDeclaration(start_s=1, attempts=3)),
        ),
    )

    result = await rig.executor.execute(make_task())

    assert result.output == {"content": "from backup"}
    assert primary.turns == 3
    assert len(rig.waits) == 2
    # jitter 는 대기를 최대 25% 줄인다 — 1, 2 초 계열
    assert 0.75 <= rig.waits[0] <= 1.0
    assert 1.5 <= rig.waits[1] <= 2.0


async def test_a_declared_rate_limit_ceiling_caps_the_waits():
    """``max_s`` 를 버리면 기본 상한(300초)으로 20·40·80 초를 기다린다."""
    rig = Rig(
        {"primary": FakeModel([rate_limited()])},
        declared(retry=ModelRetry(rate_limit=RetryDeclaration(start_s=10, attempts=5, max_s=12))),
    )

    await rig.executor.execute(make_task())

    assert len(rig.waits) == 4
    assert all(wait <= 12 for wait in rig.waits), rig.waits


# --- 로그 -------------------------------------------------------------------------


async def test_a_single_model_failure_is_not_logged_as_falling_back():
    """fallback 이 없으면 전환이 아니라 실패다 — 변경 전과 같은 로그."""
    rig = Rig({"primary": FakeModel([server_error()])}, declared())

    with capture_logs() as logs:
        result = await rig.executor.execute(make_task())

    assert result.status is TaskStatus.FAILED
    assert not [entry for entry in logs if entry["event"] == "model call failed, falling back"]


async def test_the_fallback_warning_names_the_next_model():
    rig = Rig(
        {"primary": FakeModel([server_error()]), "backup": FakeModel([text("ok")])},
        declared(backup("server_error")),
    )

    with capture_logs() as logs:
        await rig.executor.execute(make_task())

    [warning] = [entry for entry in logs if entry["event"] == "model call failed, falling back"]
    assert warning["log_level"] == "warning"
    assert warning["model"] == "primary"
    assert warning["next_model"] == "backup"
    assert warning["next_provider"] == PROVIDER


async def test_skipping_an_open_circuit_is_a_warning_with_its_cause():
    rig = Rig(
        {"primary": FakeModel([server_error()]), "backup": FakeModel([text("ok")])},
        declared(backup("server_error"), circuit=OPEN_AFTER_ONE),
    )
    await rig.executor.execute(make_task(task_id="opens"))

    with capture_logs() as logs:
        await rig.executor.execute(make_task(task_id="skips"))

    [warning] = [entry for entry in logs if entry["event"] == "model circuit open, skipping"]
    assert warning["log_level"] == "warning"
    assert warning["model"] == "primary"
    assert warning["opened_by"] == "server_error"
    assert warning["next_model"] == "backup"


async def test_a_circuit_opened_by_another_task_during_the_wait_stops_the_retry():
    """대기 중 다른 태스크가 같은 breaker 를 열면 남은 시도는 나가지 않는다."""
    circuit = CircuitDeclaration(max_failures=5, reset_s=60)
    primary = FakeModel([rate_limited()])
    rig = Rig(
        {"primary": primary, "backup": FakeModel([text("from backup")])},
        declared(backup("rate_limit"), circuit=circuit),
    )
    shared = rig.chains.for_config(declared(backup("rate_limit"), circuit=circuit)).links[0].breaker
    assert shared is not None

    class OpeningWaits(list[float]):
        def append(self, delay: float) -> None:
            super().append(delay)
            # 다른 태스크가 대기 중에 실패를 쌓아 연 것과 같다
            for _ in range(circuit.max_failures):
                shared.record_fault("rate_limit")

    rig.waits = OpeningWaits()

    result = await rig.executor.execute(make_task())

    assert result.output == {"content": "from backup"}
    assert primary.turns == 1
