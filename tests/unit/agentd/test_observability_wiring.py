"""Metric injection at the agentd startup path.

계측 로직이 있어도 **registry 를 만들어 주입하지 않으면** 런타임에서는 아무
것도 흐르지 않는다 (#95). 여기서는 그 배선만 검증한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from prometheus_client import CollectorRegistry, generate_latest

from malkuth.agentd.__main__ import build_executor, load_manifest
from malkuth.observability.metrics import Metrics

REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = REPO_ROOT / "agents" / "researcher" / "manifest.yaml"


@pytest.fixture(autouse=True)
def _standard_path(monkeypatch):
    """표준 executor 경로 — echo 대역을 쓰지 않는다."""
    monkeypatch.delenv("MALKUTH_EXECUTOR", raising=False)
    monkeypatch.setenv("MALKUTH_ROOT", str(REPO_ROOT))


async def test_executor_receives_the_registry():
    metrics = Metrics(registry=CollectorRegistry())

    executor = await build_executor(load_manifest(RESEARCHER), metrics=metrics)

    assert executor._services.telemetry is not None


async def test_labels_come_from_the_manifest():
    """빈 문자열로 남으면 그룹별·모델별 집계가 무의미해진다."""
    metrics = Metrics(registry=CollectorRegistry())

    executor = await build_executor(load_manifest(RESEARCHER), metrics=metrics)

    telemetry = executor._services.telemetry
    assert telemetry._agent == "researcher"
    assert telemetry._group == "research"
    assert telemetry._provider == "anthropic"
    assert telemetry._model


async def test_executor_works_without_metrics():
    executor = await build_executor(load_manifest(RESEARCHER))

    assert executor._services.telemetry is None


def test_the_exposed_registry_is_the_one_we_fill(monkeypatch):
    """registry 를 넘기지 않으면 prometheus 기본 registry 를 노출하게 된다 —
    우리가 채우는 곳과 달라 endpoint 가 늘 비어 있다."""
    from malkuth.agentd import __main__ as entry

    metrics = Metrics(registry=CollectorRegistry())
    exposed: dict[str, object] = {}

    def capture(port: int, *, registry=None) -> None:
        exposed["port"] = port
        exposed["registry"] = registry

    # _setup_observability 가 함수 안에서 import 하므로 원본 모듈을 패치한다
    monkeypatch.setattr("malkuth.observability.metrics.Metrics", lambda: metrics)
    monkeypatch.setattr("malkuth.observability.metrics.start_metrics_server", capture)
    monkeypatch.setenv("MALKUTH_METRICS_PORT", "9123")

    entry._setup_observability()

    assert exposed["registry"] is metrics.registry
    assert exposed["port"] == 9123


async def test_recorded_metrics_reach_the_exposed_registry():
    """endpoint 를 긁으면 0 이 아닌 값이 나와야 한다 — 배선의 최종 확인이다."""
    registry = CollectorRegistry()
    metrics = Metrics(registry=registry)

    executor = await build_executor(load_manifest(RESEARCHER), metrics=metrics)
    executor._services.telemetry.task_finished(status="completed", duration_s=0.01)

    scraped = generate_latest(registry).decode("utf-8")
    assert 'malkuth_agent_tasks_total{agent="researcher"' in scraped
    assert 'group="research"' in scraped


# --- memory 접속 배선 ----------------------------------------------------------


async def test_injected_endpoint_becomes_memory_access(monkeypatch):
    """runtime 이 주입한 주소·토큰으로만 메모리에 닿는다 (09 Access Enforcement 1)."""
    from malkuth.memory.http import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
    from malkuth.runtime.memory_http import HttpMemoryAccess

    monkeypatch.setenv(MEMORY_URL_ENV, "http://memory:8080")
    monkeypatch.setenv(MEMORY_TOKEN_ENV, "opaque")

    executor = await build_executor(load_manifest(RESEARCHER))

    assert isinstance(executor.binding.tools.memory, HttpMemoryAccess)
    assert executor.binding.tools.memory.token == "opaque"


async def test_without_an_endpoint_there_is_no_memory_access(monkeypatch):
    """주입되지 않았는데 만들면 컨테이너가 저장소를 직접 열려 든다."""
    from malkuth.memory.http import MEMORY_TOKEN_ENV, MEMORY_URL_ENV

    monkeypatch.delenv(MEMORY_URL_ENV, raising=False)
    monkeypatch.delenv(MEMORY_TOKEN_ENV, raising=False)

    executor = await build_executor(load_manifest(RESEARCHER))

    assert executor.binding.tools.memory is None


async def test_memory_search_is_advertised_once_access_exists(monkeypatch):
    """노출과 실행이 일치해야 한다 — #112 에서 확인한 tool 에러 루프 방지."""
    import yaml

    from malkuth.core.manifest import AgentManifest
    from malkuth.memory.http import MEMORY_TOKEN_ENV, MEMORY_URL_ENV
    from malkuth.memory.tool import MEMORY_SEARCH_TOOL

    monkeypatch.setenv(MEMORY_URL_ENV, "http://memory:8080")
    monkeypatch.setenv(MEMORY_TOKEN_ENV, "opaque")
    doc = yaml.safe_load(RESEARCHER.read_text("utf-8"))
    doc["spec"]["memory"] = {
        "spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]
    }

    executor = await build_executor(AgentManifest.model_validate(doc))

    assert MEMORY_SEARCH_TOOL in {spec.name for spec in executor.tool_schemas}


# --- 선택적 협력자 묶음이 실제로 채워지는가 (#235) -------------------------------


async def test_the_assembly_fills_every_declared_collaborator(monkeypatch, tmp_path):
    """묶음으로 옮기면서 하나가 빠져도 조용히 그 기능만 꺼진다.

    `ExecutorServices` 는 미주입이 곧 "그 기능 없음" 이라 **빠뜨려도 예외가
    나지 않는다** — 그래서 조립부가 실제로 채우는지 여기서 붙잡는다.

    `recall` 과 `artifacts` 는 주소/경로가 주입됐을 때만 만들어지므로
    (09 Access Enforcement 1 — 자격증명이 아니라 주소가 들어온다) 그 조건을
    갖춰 놓고 본다. 그러지 않으면 "설계상 None" 과 "배선 누락" 이 구분되지 않는다.
    """
    monkeypatch.setenv("MALKUTH_ROOT", ".")
    monkeypatch.setenv("MALKUTH_EXECUTOR", "")
    monkeypatch.setenv("MALKUTH_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("MALKUTH_MEMORY_URL", "http://memory.test")
    monkeypatch.setenv("MALKUTH_MEMORY_TOKEN", "opaque-token")

    executor = await build_executor(load_manifest(RESEARCHER), metrics=Metrics())

    services = executor._services
    assert services.telemetry is not None, "telemetry 미배선 — 집계가 조용히 멈춘다"
    assert services.artifacts is not None, "artifacts 미배선 — 산출물 참조 전달이 죽는다"
    assert services.recall is not None, "recall 미배선 — memoryset 의 회상 선언이 무동작이다"
    assert callable(services.output_keys), "output_keys 미배선 — output 계약이 사라진다"


async def test_the_assembly_turns_on_model_retry(monkeypatch):
    """재시도는 정책이므로 config 로 옮겼다 — 조립부가 켜는지 함께 본다."""
    monkeypatch.setenv("MALKUTH_ROOT", ".")
    monkeypatch.setenv("MALKUTH_EXECUTOR", "")

    executor = await build_executor(load_manifest(RESEARCHER))

    assert executor._chain.retry_policies, "재시도 정책이 정의만 되고 켜지지 않았다"
