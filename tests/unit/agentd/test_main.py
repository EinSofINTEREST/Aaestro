"""Unit tests for the agentd entrypoint.

컨테이너 없이 검증한다 — 이 계층의 계약은 manifest 로드와 앱 구성이지
프로세스 기동이 아니다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from structlog.testing import capture_logs

from aaestro.agentd.__main__ import (
    CONTROL_PORT,
    ECHO_EXECUTOR,
    EXECUTOR_ENV,
    build_app,
    build_executor,
    load_manifest,
)
from aaestro.agentd.echo import EchoExecutor
from aaestro.core.agent import TaskStatus
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest
from aaestro.observability.metrics import Metrics
from tests.fixtures.builders import make_task

REPO_ROOT = Path(__file__).resolve().parents[3]
ECHO_MANIFEST = REPO_ROOT / "agents" / "echo" / "manifest.yaml"


def write(tmp_path: Path, body: str) -> Path:
    """manifest 파일을 만든다."""
    path = tmp_path / "manifest.yaml"
    path.write_text(body, encoding="utf-8")
    return path


# --- manifest 로드 -----------------------------------------------------------


def test_control_port_is_the_declared_one():
    """컨테이너 내부 control port 는 8080 고정 (02 Network)."""
    assert CONTROL_PORT == 8080


def test_shipped_echo_manifest_is_valid():
    """이미지에 실려 나가는 manifest 가 스키마를 통과해야 한다."""
    manifest = load_manifest(ECHO_MANIFEST)

    assert manifest.name == "echo"
    assert manifest.spec.runtime.image == "aaestro/agent-echo:0.1.0"


def test_missing_manifest_is_a_config_error(tmp_path: Path):
    """읽을 수 없는 manifest 로는 기동하지 않는다."""
    with pytest.raises(AaestroError) as exc_info:
        load_manifest(tmp_path / "absent.yaml")

    assert exc_info.value.code == "CFG_001"


def test_invalid_manifest_is_rejected(tmp_path: Path):
    """미검증 manifest 로 컨테이너를 기동하지 않는다 (02 Manifest Rules 1)."""
    path = write(tmp_path, "apiVersion: aaestro/v1\nkind: Agent\nmetadata: {}\n")

    with pytest.raises(AaestroError) as exc_info:
        load_manifest(path)

    assert exc_info.value.code == "CFG_001"


def test_unparseable_yaml_is_rejected(tmp_path: Path):
    path = write(tmp_path, "apiVersion: [unclosed\n")

    with pytest.raises(AaestroError) as exc_info:
        load_manifest(path)

    assert exc_info.value.code == "CFG_001"


# --- 앱 구성 -----------------------------------------------------------------


def make_client(manifest: AgentManifest, *, token: str | None = None) -> TestClient:
    return TestClient(build_app(manifest, EchoExecutor(), token=token))


def test_card_is_derived_from_the_manifest():
    """AgentCard 는 manifest 에서 생성한다 — 수동 작성 금지 (03 AgentCard)."""
    manifest = load_manifest(ECHO_MANIFEST)

    body = make_client(manifest).get("/v1/card").json()

    assert body["name"] == "echo"
    assert body["version"] == "0.1.0"


def test_app_serves_health_unauthenticated():
    """healthcheck 가 토큰 없이 호출한다."""
    manifest = load_manifest(ECHO_MANIFEST)

    response = make_client(manifest, token="secret").get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_app_requires_the_token_on_other_endpoints():
    manifest = load_manifest(ECHO_MANIFEST)

    assert make_client(manifest, token="secret").get("/v1/card").status_code == 401


def test_concurrency_limit_comes_from_the_manifest(tmp_path: Path):
    """manifest 가 선언한 상한을 그대로 쓴다 — 하드코딩 금지."""
    path = write(
        tmp_path,
        "apiVersion: aaestro/v1\nkind: Agent\n"
        "metadata: {name: sample, version: 0.1.0}\n"
        "spec:\n"
        "  model: {provider: anthropic, name: claude-sonnet-5}\n"
        "  promptset: {ref: promptsets/sample@0.1.0}\n"
        "  runtime: {max_concurrent_tasks: 2}\n",
    )
    manifest = load_manifest(path)

    app = build_app(manifest, EchoExecutor())

    client = TestClient(app)
    assert client.get("/v1/health").status_code == 200


# --- echo 실행기 -------------------------------------------------------------


async def test_echo_returns_the_input():
    task = make_task(input={"msg": "hi"})

    result = await EchoExecutor().execute(task)

    assert result.status == TaskStatus.COMPLETED
    assert result.output == {"msg": "hi"}


async def test_echo_streams_token_then_done():
    task = make_task(input={"msg": "hi"})

    events = [event async for event in EchoExecutor().stream(task)]

    assert [e.type for e in events] == ["token", "done"]
    assert events[-1].output == {"msg": "hi"}


# --- 실행기 선택 --------------------------------------------------------------


async def test_echo_executor_is_opt_in(monkeypatch):
    """테스트 이미지만 echo 대역을 쓴다."""
    monkeypatch.setenv(EXECUTOR_ENV, ECHO_EXECUTOR)

    executor = await build_executor(load_manifest(ECHO_MANIFEST))

    assert isinstance(executor, EchoExecutor)


async def test_base_image_does_not_default_to_echo(monkeypatch):
    """base 이미지가 echo 로 돌면 모든 에이전트가 대역이 된다.

    이제 표준 경로는 실 provider 를 세운다 — echo 로 조용히 떨어지지 않는다.
    """
    monkeypatch.delenv(EXECUTOR_ENV, raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))

    executor = await build_executor(load_manifest(ECHO_MANIFEST))

    assert not isinstance(executor, EchoExecutor)


async def test_unbound_provider_is_rejected(monkeypatch):
    """바인딩 없는 provider 를 조용히 넘기면 운영에서 가짜 응답이 나간다."""
    monkeypatch.delenv(EXECUTOR_ENV, raising=False)
    manifest = load_manifest(ECHO_MANIFEST)
    other = manifest.model_copy(
        update={
            "spec": manifest.spec.model_copy(
                update={"model": manifest.spec.model.model_copy(update={"provider": "openai"})}
            )
        }
    )

    with pytest.raises(AaestroError) as exc_info:
        await build_executor(other)

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.details["provider"] == "openai"


def _with_provider(provider: str):
    manifest = load_manifest(ECHO_MANIFEST)
    model = manifest.spec.model.model_copy(update={"provider": provider, "name": "gpt-compatible"})
    return manifest.model_copy(update={"spec": manifest.spec.model_copy(update={"model": model})})


async def test_openai_compat_manifest_builds_its_adapter_from_the_table(monkeypatch):
    """진입점이 provider 테이블을 쓴다 — anthropic 이 아니라는 이유로 거절하지 않는다 (#361)."""
    from aaestro.agentd.providers.openai_compat import OpenAICompatModel

    monkeypatch.delenv(EXECUTOR_ENV, raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))
    monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "http://egress:8081/openai-compat")
    monkeypatch.setenv("OPENAI_COMPAT_API_KEY", "cred-echo")

    executor = await build_executor(_with_provider("openai-compat"))

    [link] = executor._chain.links  # noqa: SLF001
    assert isinstance(link.model, OpenAICompatModel)
    assert (link.provider, link.model.base_url, link.model.credential) == (
        "openai-compat",
        "http://egress:8081/openai-compat",
        "cred-echo",
    )


async def test_openai_compat_manifest_without_its_settings_fails_startup(monkeypatch):
    """주소 없이 뜨면 첫 태스크에서야 드러난다 — 기동에서 막는다."""
    monkeypatch.delenv(EXECUTOR_ENV, raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))
    monkeypatch.delenv("OPENAI_COMPAT_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_COMPAT_API_KEY", raising=False)

    with pytest.raises(AaestroError) as exc_info:
        await build_executor(_with_provider("openai-compat"))

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.details["settings"] == [
        "OPENAI_COMPAT_BASE_URL",
        "OPENAI_COMPAT_API_KEY",
    ]


async def test_unknown_executor_selection_is_rejected(monkeypatch):
    monkeypatch.setenv(EXECUTOR_ENV, "mystery")

    with pytest.raises(AaestroError) as exc_info:
        await build_executor(load_manifest(ECHO_MANIFEST))

    assert exc_info.value.code == "CFG_001"
    assert exc_info.value.details["executor"] == "mystery"


# --- runtime 발급 토큰과의 결합 -------------------------------------------------


def test_runtime_issued_token_authorizes_the_agent():
    """runtime 이 발급해 주입한 토큰으로만 Control API 가 열려야 한다."""
    from aaestro.runtime.tokens import AGENT_TOKEN_ENV, TokenIssuer, authenticated_env

    issuer = TokenIssuer()
    manifest = load_manifest(ECHO_MANIFEST)
    env = authenticated_env(issuer, manifest.name)
    client = TestClient(build_app(manifest, EchoExecutor(), token=env[AGENT_TOKEN_ENV]))

    body = make_task().model_dump(mode="json")

    assert client.post("/v1/invoke", json=body).status_code == 401
    authorized = client.post(
        "/v1/invoke",
        json=body,
        headers={"authorization": f"Bearer {issuer.issue(manifest.name)}"},
    )
    assert authorized.status_code == 200


def test_another_agents_token_is_rejected():
    """한 에이전트의 토큰으로 다른 에이전트를 열 수 없다."""
    from aaestro.runtime.tokens import TokenIssuer

    issuer = TokenIssuer()
    manifest = load_manifest(ECHO_MANIFEST)
    client = TestClient(build_app(manifest, EchoExecutor(), token=issuer.issue(manifest.name)))

    other = issuer.issue("someone-else")
    response = client.post(
        "/v1/invoke",
        json=make_task().model_dump(mode="json"),
        headers={"authorization": f"Bearer {other}"},
    )

    assert response.status_code == 401


# --- 노출과 실행의 일치 ---------------------------------------------------------


async def researcher_with_memory(monkeypatch):
    """memory space 를 선언한 researcher manifest."""
    import yaml

    from aaestro.core.manifest import AgentManifest

    monkeypatch.delenv(EXECUTOR_ENV, raising=False)
    monkeypatch.setenv("AAESTRO_ROOT", str(REPO_ROOT))
    doc = yaml.safe_load((REPO_ROOT / "agents" / "researcher" / "manifest.yaml").read_text("utf-8"))
    doc["spec"]["memory"] = {
        "spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm"}]
    }
    return await build_executor(AgentManifest.model_validate(doc))


async def test_unrunnable_tools_are_not_advertised(monkeypatch):
    """실행할 수 없는 tool 을 보이면 모델이 고를 때마다 거부되어 루프에 빠진다."""
    from aaestro.memory.tool import MEMORY_SEARCH_TOOL

    executor = await researcher_with_memory(monkeypatch)

    advertised = {spec.name for spec in executor.tool_schemas}
    assert MEMORY_SEARCH_TOOL not in advertised
    assert "search" in advertised


async def test_every_advertised_tool_is_runnable(monkeypatch):
    """노출과 실행이 어긋나면 그 자체가 결함이다."""
    executor = await researcher_with_memory(monkeypatch)

    for spec in executor.tool_schemas:
        assert executor.binding.tools.timeout_for(spec.name) >= 0.0
        assert spec.name in executor.binding.tools._skills


def test_mcp_tools_carry_their_schema():
    """registry 가 스키마를 담아야 모델이 인자를 채울 수 있다.

    예전에는 서버 이름 문자열을 담아서, 그대로 넘기면 provider 가
    ``to_tool_schema()`` 를 부르다 터졌다 (#115 에서 해소).
    """
    from aaestro.agentd.__main__ import _executable_schemas
    from aaestro.agentd.tools import AgentToolRegistry
    from aaestro.core.skill import SkillSpec

    mcp_spec = SkillSpec(
        name="mcp__filesystem__read_file",
        description="MCP tool from filesystem",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}},
    )

    class Result:
        tools = {
            "search": SkillSpec(name="search", description="d", parameters={}),
            "mcp__filesystem__read_file": mcp_spec,
        }

    class LiveMcp:
        async def call_tool(self, name, arguments):  # pragma: no cover - 도달 안 함
            return None

    schemas = _executable_schemas(Result(), AgentToolRegistry(agent="researcher", mcp=LiveMcp()))

    assert all(isinstance(spec, SkillSpec) for spec in schemas)
    assert {spec.name for spec in schemas} == {"search", "mcp__filesystem__read_file"}


def test_mcp_tools_are_hidden_without_a_session():
    """세션이 없으면 부를 수 없다 — 광고하면 tool 에러 루프에 빠진다."""
    from aaestro.agentd.__main__ import _executable_schemas
    from aaestro.agentd.tools import AgentToolRegistry
    from aaestro.core.skill import SkillSpec

    class Result:
        tools = {
            "search": SkillSpec(name="search", description="d", parameters={}),
            "mcp__filesystem__read_file": SkillSpec(
                name="mcp__filesystem__read_file", description="d", parameters={}
            ),
        }

    schemas = _executable_schemas(Result(), AgentToolRegistry(agent="researcher"))

    assert [spec.name for spec in schemas] == ["search"]


# --- 진입점 배선: 판정한 토큰 · 바인드 주소가 그대로 서빙까지 간다 (#394) ----------------


@pytest.fixture
def entry(monkeypatch):
    """`main()` 을 실제 서버 없이 돌린다 — 판정과 서빙 인자만 가로채 본다."""
    import aaestro.agentd.__main__ as agentd

    for key in (
        agentd.TOKEN_ENV,
        agentd.BIND_HOST_ENV,
        "AAESTRO_ALLOW_UNAUTHENTICATED",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv(agentd.MANIFEST_ENV, str(ECHO_MANIFEST))
    monkeypatch.setenv(EXECUTOR_ENV, ECHO_EXECUTOR)
    monkeypatch.setattr(agentd, "_configure_logging", lambda: None)
    monkeypatch.setattr(agentd, "_setup_observability", Metrics)
    seen: dict[str, object] = {}
    real_build_app = agentd.build_app

    def spy_build_app(*args, **kwargs):
        seen["token"] = kwargs.get("token")
        return real_build_app(*args, **kwargs)

    async def fake_serve(app, manifest, executor, bind_host):
        seen["bind_host"] = bind_host

    monkeypatch.setattr(agentd, "build_app", spy_build_app)
    monkeypatch.setattr(agentd, "_serve", fake_serve)
    return agentd, monkeypatch, seen


def test_main_without_a_token_refuses_to_start(entry):
    agentd, _, seen = entry

    with capture_logs() as logs, pytest.raises(SystemExit) as exc:
        agentd.main()

    assert exc.value.code == 1
    (line,) = [entry for entry in logs if entry["log_level"] == "critical"]
    assert line["event"] == "agent startup rejected"
    assert line["error_code"] == "CFG_001"
    assert line["details"]["setting"] == agentd.TOKEN_ENV
    assert agentd.TOKEN_ENV in line["error_message"]
    assert seen == {}, "거절됐는데 앱 조립까지 갔다"


def test_main_opt_in_with_a_wildcard_bind_refuses_to_start(entry):
    agentd, monkeypatch, seen = entry
    monkeypatch.setenv("AAESTRO_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv(agentd.BIND_HOST_ENV, "0.0.0.0")  # noqa: S104

    with capture_logs() as logs, pytest.raises(SystemExit) as exc:
        agentd.main()

    assert exc.value.code == 1
    (line,) = [entry for entry in logs if entry["log_level"] == "critical"]
    assert line["error_code"] == "CFG_001"
    assert line["details"]["setting"] == "AAESTRO_ALLOW_UNAUTHENTICATED"
    assert "loopback" in line["error_message"]
    assert seen == {}


def test_main_opt_in_on_loopback_serves_without_a_token(entry):
    agentd, monkeypatch, seen = entry
    monkeypatch.setenv("AAESTRO_ALLOW_UNAUTHENTICATED", "1")
    monkeypatch.setenv(agentd.BIND_HOST_ENV, "127.0.0.1")

    agentd.main()

    assert seen == {"token": None, "bind_host": "127.0.0.1"}


def test_main_passes_the_judged_token_and_bind_host_to_the_server(entry):
    agentd, monkeypatch, seen = entry
    monkeypatch.setenv(agentd.TOKEN_ENV, "issued-token")
    monkeypatch.setenv(agentd.BIND_HOST_ENV, "10.1.2.3")

    agentd.main()

    assert seen == {"token": "issued-token", "bind_host": "10.1.2.3"}


def test_main_without_bind_override_binds_the_container_default(entry):
    agentd, monkeypatch, seen = entry
    monkeypatch.setenv(agentd.TOKEN_ENV, "issued-token")

    agentd.main()

    assert seen["bind_host"] == agentd.DEFAULT_BIND_HOST


def test_main_configures_logging_before_judging_the_token(entry):
    agentd, monkeypatch, _ = entry
    order: list[str] = []
    monkeypatch.setattr(agentd, "_configure_logging", lambda: order.append("logging"))
    real = agentd.resolve_required_token

    def spy(*args, **kwargs):
        order.append("judge")
        return real(*args, **kwargs)

    monkeypatch.setattr(agentd, "resolve_required_token", spy)
    monkeypatch.setenv(agentd.TOKEN_ENV, "issued-token")

    agentd.main()

    assert order == ["logging", "judge"]


@pytest.fixture
def uvicorn_spy(monkeypatch):
    """uvicorn 을 띄우지 않고 Config 인자만 기록한다."""
    import aaestro.agentd.__main__ as agentd

    configs: list[tuple[object, dict]] = []

    class FakeConfig:
        def __init__(self, app, **kwargs):
            configs.append((app, kwargs))

    class FakeServer:
        def __init__(self, config):
            self.config = config

        async def serve(self):
            return None

    monkeypatch.setattr(agentd.uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(agentd.uvicorn, "Server", FakeServer)
    return agentd, configs


async def test_serve_binds_the_control_server_to_the_given_host(uvicorn_spy, monkeypatch):
    agentd, configs = uvicorn_spy
    monkeypatch.delenv("AAESTRO_A2A_PORT", raising=False)

    await agentd._serve("app", load_manifest(ECHO_MANIFEST), EchoExecutor(), "127.0.0.1")

    assert [(app, kw["host"]) for app, kw in configs] == [("app", "127.0.0.1")]


async def test_serve_both_binds_control_and_peer_to_the_given_host(uvicorn_spy):
    agentd, configs = uvicorn_spy

    await agentd._serve_both("control", "peer", 19100, "127.0.0.1")

    assert {app: kw["host"] for app, kw in configs} == {
        "control": "127.0.0.1",
        "peer": "127.0.0.1",
    }
