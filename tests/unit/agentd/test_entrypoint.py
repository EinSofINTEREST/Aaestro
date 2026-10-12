"""``spec.entrypoint`` loading tests.

02 는 Custom Agent 를 규정하지만, 그 필드는 스키마에 선언만 되어 있고 **아무도
읽지 않았다** — 선언해도 조용히 무시되고 기본 루프가 돌았다 (#132).

조용한 fallback 이 가장 나쁜 실패다: 운영자는 자기 코드가 도는 줄 알지만
실제로는 Messages API 가 불린다. 그래서 해석 실패는 전부 기동 거부다.
"""

from __future__ import annotations

import pytest

from aaestro.agentd.__main__ import build_executor, load_entrypoint
from aaestro.core.errors import AaestroError, ErrorCode
from tests.fixtures.builders import make_manifest

FIXTURES = "tests.fixtures.custom_agents.valid"


def manifest_with(entrypoint: str | None):
    """entrypoint 를 선언한 manifest."""
    base = make_manifest()
    return base.model_copy(update={"spec": base.spec.model_copy(update={"entrypoint": entrypoint})})


# --- 로딩 --------------------------------------------------------------------


async def test_a_declared_executor_is_actually_served():
    """#132 의 핵심 — 선언이 실제로 서빙으로 이어져야 한다."""
    executor = await build_executor(manifest_with(f"{FIXTURES}:WithManifest"))

    result = await executor.execute(object())

    assert result["served_by"] == "WithManifest"


async def test_the_manifest_is_handed_to_the_executor():
    """커스텀 코드가 선언을 읽어야 하는 경우가 있다."""
    executor = await build_executor(manifest_with(f"{FIXTURES}:WithManifest"))

    result = await executor.execute(object())

    assert result["agent"] == "test-agent"


async def test_an_executor_without_constructor_arguments_is_allowed():
    """manifest 가 필요 없는 실행기까지 강제하면 계약이 과하게 좁아진다."""
    executor = await build_executor(manifest_with(f"{FIXTURES}:WithoutManifest"))

    result = await executor.execute(object())

    assert result["served_by"] == "WithoutManifest"


# --- 거부 --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reference", "expected_reason"),
    [
        (f"{FIXTURES}:Missing", "AttributeError"),
        ("nosuch.module:Thing", "ModuleNotFoundError"),
    ],
)
async def test_an_unresolvable_entrypoint_refuses_to_start(reference, expected_reason):
    """조용히 기본으로 떨어지면 운영자가 가짜 실행기를 진짜로 믿는다."""
    with pytest.raises(AaestroError) as exc_info:
        await build_executor(manifest_with(reference))

    assert exc_info.value.code == ErrorCode.CFG_001
    assert exc_info.value.details["reason"] == expected_reason


@pytest.mark.parametrize("reference", ["bogus", ":Thing", "module:", ""])
def test_a_malformed_reference_is_rejected(reference):
    with pytest.raises(AaestroError) as exc_info:
        load_entrypoint(manifest_with(reference))

    assert exc_info.value.code == ErrorCode.CFG_001


@pytest.mark.parametrize("reference", ["bogus", ":Thing", "module:", ""])
async def test_a_malformed_reference_is_rejected_on_the_startup_path(reference, monkeypatch):
    """``load_entrypoint`` 직접 호출은 **프로덕션 경로를 건너뛴다**.

    truthiness 로 보면 `entrypoint: ""` 가 미선언과 같아져 조용히 표준
    실행기로 떨어진다 — 그것을 잡으려면 기동 경로로 들어가야 한다.
    """
    monkeypatch.setenv("AAESTRO_EXECUTOR", "echo")

    with pytest.raises(AaestroError) as exc_info:
        await build_executor(manifest_with(reference))

    assert exc_info.value.code == ErrorCode.CFG_001


@pytest.mark.parametrize(
    "reference",
    [f"{FIXTURES}:NeedsMoreThanManifest", f"{FIXTURES}:ExplodingConstructor"],
)
async def test_a_failing_constructor_is_reported_as_a_config_failure(reference):
    """생성자가 터지면 데몬이 구조화되지 않은 예외로 죽는다 — 설정 문제인지
    코드 버그인지 구분할 단서를 잃는다."""
    with pytest.raises(AaestroError) as exc_info:
        await build_executor(manifest_with(reference))

    assert exc_info.value.code == ErrorCode.CFG_001
    assert exc_info.value.details["reason"] in ("TypeError", "RuntimeError")


@pytest.mark.parametrize(
    "reference",
    [f"{FIXTURES}:MissingStream", f"{FIXTURES}:NotCallableExecute"],
)
async def test_an_executor_that_breaks_the_contract_is_rejected(reference):
    """계약을 확인하지 않으면 **첫 태스크에서야** AttributeError 로 터진다."""
    with pytest.raises(AaestroError) as exc_info:
        await build_executor(manifest_with(reference))

    assert exc_info.value.code == ErrorCode.CFG_001
    assert "missing" in exc_info.value.details["reason"]


# --- 미선언 시 기존 동작 ---------------------------------------------------------


async def test_without_an_entrypoint_the_echo_selection_still_works(monkeypatch):
    """미선언 시 기존 경로가 그대로여야 한다."""
    monkeypatch.setenv("AAESTRO_EXECUTOR", "echo")

    executor = await build_executor(manifest_with(None))

    assert type(executor).__name__ == "EchoExecutor"


async def test_a_declared_entrypoint_wins_over_the_echo_env(monkeypatch):
    """manifest 선언이 env 대역보다 우선이다 — 이미지가 대역으로 새지 않도록."""
    monkeypatch.setenv("AAESTRO_EXECUTOR", "echo")

    executor = await build_executor(manifest_with(f"{FIXTURES}:WithoutManifest"))

    assert type(executor).__name__ == "WithoutManifest"
