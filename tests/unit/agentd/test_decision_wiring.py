"""Decision provider wiring at agentd startup (#595).

``spec.decision`` 이 있으면 agentd 가 어댑터 레지스트리로 provider 클라이언트를 만들고, 그 주소는
runtime 이 주입한 프록시 창구다. 키는 env 에 없다 — 자격은 에이전트 신원이다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
import yaml

from aaestro.access.model import ACCESS_CREDENTIAL_ENV
from aaestro.agentd.__main__ import build_executor
from aaestro.agentd.bootstrap import decision_client
from aaestro.agentd.health import agent_health
from aaestro.core.agent import HealthState
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest
from aaestro.decision import DECISION_KIND_ENV, DECISION_TIMEOUT_ENV, DECISION_URL_ENV

REPO_ROOT = Path(__file__).resolve().parents[3]
RESEARCHER = REPO_ROOT / "agents" / "researcher" / "manifest.yaml"
PROXY = "http://aaestro-egress:8081/decision/jev"
WIRED = {
    DECISION_URL_ENV: PROXY,
    DECISION_KIND_ENV: "jev",
    DECISION_TIMEOUT_ENV: "3.0",
    ACCESS_CREDENTIAL_ENV: "cred-researcher",
}


def deciding() -> AgentManifest:
    doc = yaml.safe_load(RESEARCHER.read_text("utf-8"))
    doc["spec"]["decision"] = {
        "provider": "jev",
        "model": "jev-1.13",
        "sets": [{"ref": "decisionsets/triage@0.1.0", "as": "triage"}],
    }
    return AgentManifest.model_validate(doc)


@pytest.fixture(autouse=True)
def _standard_path(monkeypatch):
    monkeypatch.delenv("AAESTRO_EXECUTOR", raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))


async def test_build_executor_with_spec_decision_builds_the_provider_on_the_proxy(monkeypatch):
    for name, value in WIRED.items():
        monkeypatch.setenv(name, value)

    executor = await build_executor(deciding())

    client = executor.decision
    assert client is not None
    assert (client.agent, client.provider_kind, client.model) == ("researcher", "jev", "jev-1.13")
    assert client.timeout_s == 3.0
    assert client.provider._endpoint == f"{PROXY}/v1/systemone"
    # 키가 아니라 신원을 싣는다 — 프록시가 떼고 운영자 키를 붙인다
    assert client.provider._headers == {"authorization": "Bearer cred-researcher"}
    assert agent_health(executor).components["decision"].state is HealthState.HEALTHY


async def test_build_executor_without_spec_decision_has_no_provider(monkeypatch):
    for name, value in WIRED.items():
        monkeypatch.setenv(name, value)

    plain = AgentManifest.model_validate(yaml.safe_load(RESEARCHER.read_text("utf-8")))

    executor = await build_executor(plain)

    assert executor.decision is None
    assert "decision" not in agent_health(executor).components


def test_decision_client_without_the_proxy_wiring_warns_and_takes_the_original_path():
    with structlog.testing.capture_logs() as logs:
        assert decision_client(deciding(), {}) is None

    [warned] = [e for e in logs if e["event"].startswith("decision provider not wired")]
    assert (warned["log_level"], warned["agent"], warned["provider"]) == (
        "warning",
        "researcher",
        "jev",
    )


@pytest.mark.parametrize(
    ("dropped", "replaced"),
    [
        (ACCESS_CREDENTIAL_ENV, None),
        (DECISION_KIND_ENV, None),
        (DECISION_TIMEOUT_ENV, None),
        (DECISION_TIMEOUT_ENV, "soon"),
        (DECISION_TIMEOUT_ENV, "0"),
    ],
)
def test_decision_client_partial_wiring_raises_cfg_001(dropped, replaced):
    environ = dict(WIRED)
    if replaced is None:
        del environ[dropped]
    else:
        environ[dropped] = replaced

    with pytest.raises(AaestroError) as exc:
        decision_client(deciding(), environ)

    assert exc.value.code == "CFG_001"
    assert exc.value.details["settings"] == [dropped]
    assert "cred-researcher" not in str(exc.value.details)


def test_decision_health_is_degraded_while_the_circuit_is_open():
    client = decision_client(deciding(), WIRED)
    assert client is not None
    for _ in range(client.breaker.max_failures):
        client.breaker.record_failure()

    class Executor:
        decision = client

    component = agent_health(Executor()).components["decision"]
    assert component.state is HealthState.DEGRADED
