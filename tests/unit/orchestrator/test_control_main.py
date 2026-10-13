"""The Control Plane must be servable as a process.

`create_app` 은 구현돼 있었지만 **그것을 만드는 곳이 테스트뿐이었다** —
`run-list` / `run-drain` / `run-resume` 세 명령이 붙을 서버가 없었다 (#221).

여기서는 진입점이 설정을 어떻게 읽는지와, run 을 **기록하지 않으면** 그 표면이
빈 목록을 돌려준다는 사실을 못 박는다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest
import yaml

from aaestro.cli.main import build_parser, cmd_run, run_manager_for
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.orchestrator import __main__ as entrypoint
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import SqliteRunStore


def write_config(directory, orchestrator: dict) -> None:
    (directory / "local.yaml").write_text(
        yaml.safe_dump({"orchestrator": orchestrator}), encoding="utf-8"
    )


def args_for(config_dir):
    return argparse.Namespace(environment="local", config_dir=str(config_dir))


def test_serving_without_a_store_is_refused(tmp_path, monkeypatch):
    """빈 목록은 "run 이 없다" 로 읽힌다 — 설정 누락과 구분되지 않는다."""
    write_config(tmp_path, {"control_port": 18999})
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)
    # 거절이 사라지면 진짜 서버가 떠서 테스트가 **멈춘다** — 실패는 소리나야 한다
    served: list[object] = []
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kwargs: served.append(app))

    with pytest.raises(AaestroError) as excinfo:
        entrypoint._start(None)

    assert excinfo.value.code == ErrorCode.CFG_001
    assert "run_store" in excinfo.value.message
    assert not served, "저장소 없이 서빙하면 조회가 조용히 비어 보인다"


def test_the_configured_store_is_served(tmp_path, monkeypatch):
    """설정의 저장소를 열어 서빙해야 한다 — 다른 저장소를 보면 늘 비어 있다."""
    store_path = tmp_path / "runs.db"
    write_config(tmp_path, {"run_store": str(store_path), "control_port": 18999})
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    served: dict = {}

    def capture(app, *, host, port, log_config):  # noqa: ARG001
        served["app"] = app
        served["host"] = host
        served["port"] = port

    monkeypatch.setattr(entrypoint.uvicorn, "run", capture)
    entrypoint.main()

    assert served["port"] == 18999
    assert served["host"] == "127.0.0.1"
    assert served["app"] is not None


async def test_the_served_app_refuses_resume(tmp_path):
    """이 프로세스는 run 을 구동하지 않는다 — 조용히 성공하면 운영자가 오해한다.

    라우트가 *있는지* 가 아니라 **실제로 거절하는지**를 본다 — 라우트 목록 검사는
    라우터 구조가 바뀌면 깨지고, 있다는 것만으로는 501 인지 200 인지 모른다.
    """
    import httpx

    from aaestro.orchestrator.runstore import RunRecord

    store = SqliteRunStore(path=str(tmp_path / "runs.db"))
    store.upsert(RunRecord(run_id="halted", graph="g", mode="service", status="halted"))
    transport = httpx.ASGITransport(app=create_app(store))

    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as api:
        response = await api.post("/v1/runs/halted/resume")

    assert response.status_code == 501
    assert "does not drive the run" in response.text


def test_the_cli_records_runs_when_a_store_is_configured(tmp_path):
    """#221 — 기록하지 않으면 control plane 이 떠 있어도 빈 목록을 돌려준다."""
    store_path = tmp_path / "runs.db"
    write_config(tmp_path, {"run_store": str(store_path)})

    manager = run_manager_for(args_for(tmp_path))

    assert manager.store is not None, "설정에 저장소가 있는데 manager 가 기록하지 않는다"


def test_the_cli_stays_storeless_without_configuration(tmp_path):
    """설정하지 않은 사람에게 파일을 만들지 않는다 — 기본은 그대로다."""
    write_config(tmp_path, {})

    manager = run_manager_for(args_for(tmp_path))

    assert manager.store is None


def test_the_cli_honours_the_configured_slots(tmp_path):
    """상한도 설정이 정한다 — manager 를 새로 만들면서 놓치기 쉽다."""
    write_config(tmp_path, {"max_concurrent_runs": 3, "max_service_runs": 2})

    manager = run_manager_for(args_for(tmp_path))

    assert (manager._max_mission, manager._max_service) == (3, 2)


def test_a_submitted_run_is_actually_recorded(tmp_path):
    """#221 — `run_manager_for` 가 있어도 **부르는 곳이 없으면** 기록되지 않는다.

    헬퍼만 따로 검사하면 배선이 빠져도 초록이다 — 이 프로젝트가 반복해서
    만난 함정이다. 그래서 `cmd_run` 을 실제로 태운다.

    에이전트를 주지 않아 run 은 실패하지만, **실패한 run 도 기록되어야** 한다:
    운영자가 control plane 에서 봐야 하는 것이 바로 그런 run 이다.
    """
    store_path = tmp_path / "runs.db"
    write_config(tmp_path, {"run_store": str(store_path)})
    graph = Path(__file__).resolve().parents[3] / "graphs" / "research-pipeline.yaml"

    parser = build_parser()
    args = parser.parse_args(
        [
            "run",
            str(graph),
            "--input",
            '{"query": "q"}',
            "--run-id",
            "recorded-run",
            "--env",
            "local",
            "--config-dir",
            str(tmp_path),
        ]
    )
    cmd_run(args)

    recorded = SqliteRunStore(path=str(store_path)).get("recorded-run")
    assert recorded is not None, "제출한 run 이 저장소에 남지 않았다"
    assert recorded.graph == "research-pipeline"


@pytest.mark.parametrize("value", ["", ":memory:", " :memory: ", "file::memory:?cache=shared"])
@pytest.mark.parametrize("setting", ["deployment_store", "material_store"])
def test_a_private_store_is_rejected_for_every_store_setting(tmp_path, setting, value):
    """run_store 와 같은 이유 — 연결마다 다른 private DB 는 미설정보다 나쁘다.

    배포 기록과 빌드 재료는 **프로세스를 넘겨** 읽혀야 한다: 재부착(#243)도, 저작과 빌드가
    다른 시점에 일어나는 것(#264/#265)도 그 전제 위에 있다.
    """
    from aaestro.config import load_config

    write_config(tmp_path, {"run_store": str(tmp_path / "runs.db"), setting: value})

    with pytest.raises(AaestroError) as excinfo:
        load_config("local", config_dir=str(tmp_path))

    assert excinfo.value.code == ErrorCode.CFG_001
    assert setting in str(excinfo.value.details)


@pytest.mark.parametrize("setting", ["deployment_store", "material_store"])
def test_a_file_path_is_accepted_for_every_store_setting(tmp_path, setting):
    """과잉 거절 방지 — 정상 경로는 통과해야 한다."""
    from aaestro.config import load_config

    write_config(
        tmp_path, {"run_store": str(tmp_path / "runs.db"), setting: str(tmp_path / "x.db")}
    )

    config = load_config("local", config_dir=str(tmp_path))

    assert getattr(config.orchestrator, setting) == str(tmp_path / "x.db")


@pytest.mark.parametrize("value", ["", ":memory:", " :memory: ", "file::memory:?cache=shared"])
def test_a_private_run_store_is_rejected(tmp_path, value):
    """공유되지 않는 저장소는 미설정보다 나쁘다 — 설정한 줄 알았는데 아무도 같은 것을 안 본다.

    sqlite 의 ``""`` 와 ``":memory:"`` 는 오류가 아니라 **연결마다 다른 private DB** 를
    연다. 그대로 두면 CLI 와 control plane 이 서로 다른 저장소를 보게 되어, 이 설정이
    있는데도 run 이 보이지 않는 상태가 조용히 재현된다 — #221 이 고치려던 바로 그것이다.
    """
    write_config(tmp_path, {"run_store": value})

    with pytest.raises(AaestroError) as excinfo:
        run_manager_for(args_for(tmp_path))

    assert excinfo.value.code == ErrorCode.CFG_001


def test_a_file_path_is_accepted(tmp_path):
    """거절이 너무 넓으면 정상 설정까지 막는다 — 파일 경로는 통과해야 한다."""
    write_config(tmp_path, {"run_store": str(tmp_path / "runs.db")})

    assert run_manager_for(args_for(tmp_path)).store is not None


@pytest.mark.parametrize("value", ["", ":memory:"])
def test_a_private_store_would_not_be_shared(value, tmp_path, monkeypatch):
    """왜 거절하는지를 근거로 남긴다 — 두 연결이 서로를 보지 못한다.

    **tmp_path 안에서 연결한다**: 빈 값은 임시 파일을 만들고, 경로로 해석되는 값은
    실제 파일을 남긴다. 저장소 루트에서 돌리면 그것이 커밋에 실린다 — 실제로
    한 번 실렸다.
    """
    import sqlite3

    monkeypatch.chdir(tmp_path)

    first = sqlite3.connect(value)
    first.execute("create table probe(x)")

    second = sqlite3.connect(value)

    with pytest.raises(sqlite3.OperationalError):
        second.execute("select * from probe")


def test_a_whitespace_padded_memory_marker_is_still_rejected(tmp_path):
    """` :memory: ` 는 sqlite 에겐 **그 이름의 파일**이지만, 사람에겐 오타다.

    문자 그대로 해석되므로 공유는 되지만, 저장소 경로로 그런 이름을 의도했을 가능성은
    없다. 오타를 그대로 받아 이상한 파일을 만드는 대신 거절한다.
    """
    write_config(tmp_path, {"run_store": " :memory: "})

    with pytest.raises(AaestroError) as excinfo:
        run_manager_for(args_for(tmp_path))

    assert excinfo.value.code == ErrorCode.CFG_001


def test_the_served_author_pins_running_declarations(monkeypatch, tmp_path):
    """#249 리뷰 — 프로덕션 Author 에 in_use 가 비어 있으면 가드가 무동작이다."""
    write_config(tmp_path, {"run_store": str(tmp_path / "runs.db"), "control_port": 18999})
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AAESTRO_REPO_ROOT", str(Path(__file__).resolve().parents[3]))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)
    captured: dict = {}
    monkeypatch.setattr(
        entrypoint, "create_app", lambda store, **kw: captured.update(kw) or object()
    )
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kw: None)

    entrypoint.main()

    author = captured["author"]
    assert author.in_use is not None, "in_use 미배선 — 배포/실행 중 선언이 보호되지 않는다"
    assert author.in_use("graph", "research-pipeline") is False  # run 이 없으면 사용 중이 아니다


def test_deployments_ask_the_builder_before_starting_custom_agents(tmp_path, monkeypatch):
    """#266 — 게이트가 있어도 진입점이 빌더를 물리지 않으면 배포는 아무것도 묻지 않는다."""
    from aaestro.runtime.docker import client as docker_client
    from aaestro.runtime.images import ImageBuilder

    write_config(
        tmp_path,
        {
            "run_store": str(tmp_path / "runs.db"),
            "deployment_store": str(tmp_path / "deployments.db"),
            "material_store": str(tmp_path / "materials.db"),
            "build_store": str(tmp_path / "builds.db"),
            "control_port": 18999,
        },
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)
    # 데몬 없이 조립만 본다 — SDK 핸들은 만들기만 하고 부르지 않는다
    monkeypatch.setattr(docker_client.docker, "from_env", lambda: object())

    class Deployments:
        author = None
        images = None
        launcher = None

        def in_use(self, kind: str, name: str) -> bool:
            return False

    deployments = Deployments()
    monkeypatch.setattr(entrypoint, "_deployment_manager", lambda *a, **k: deployments)
    monkeypatch.setattr(entrypoint, "_run_service", lambda *a, **k: None)
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kwargs: None)

    entrypoint.main()

    assert isinstance(deployments.images, ImageBuilder)


async def test_the_served_app_drives_runs_when_deployments_are_configured(tmp_path, monkeypatch):
    """#244 — 배포 표면이 열리면 이 프로세스가 구동 프로세스다: resume 이 501 이 아니다.

    라우트 목록이 아니라 **응답**을 본다 — 미지의 run 은 404 (구동자가 그래프를 찾다
    실패) 이지 501 (구동자 없음) 이 아니어야 한다.
    """
    import httpx

    from aaestro.orchestrator.runstore import RunRecord
    from aaestro.runtime.docker.engine import DockerEngine
    from aaestro.runtime.launcher import AgentLauncher
    from tests.fixtures.fake_docker import FakeDockerClient

    store_path = tmp_path / "runs.db"
    write_config(
        tmp_path,
        {
            "run_store": str(store_path),
            "deployment_store": str(tmp_path / "deployments.db"),
            "control_port": 18999,
        },
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    class NoDockerDeployments:
        """Docker 데몬 없이 조립만 본다 — launcher 는 대역 위에 둔다."""

        launcher = AgentLauncher(engine=DockerEngine(client=FakeDockerClient()))
        author = None

        def in_use(self, kind: str, name: str) -> bool:
            return False

        def get(self, deployment_id: str):
            raise AaestroError(
                category=ErrorCategory.NOT_FOUND, code=ErrorCode.NF_001, message="unknown"
            )

        async def reattach(self):
            return []

    monkeypatch.setattr(entrypoint, "_deployment_manager", lambda *a, **k: NoDockerDeployments())
    served: dict = {}
    monkeypatch.setattr(
        entrypoint.uvicorn, "run", lambda app, **_kwargs: served.__setitem__("app", app)
    )
    entrypoint.main()

    SqliteRunStore(path=str(store_path)).upsert(
        RunRecord(run_id="halted", graph="no-such-graph", mode="service", status="halted")
    )
    transport = httpx.ASGITransport(app=served["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as api:
        resumed = await api.post("/v1/runs/halted/resume")
        submitted = await api.post("/v1/runs", json={"deployment_id": "dep-nope", "input": {}})

    assert resumed.status_code == 404, resumed.text
    assert submitted.status_code == 404, submitted.text


# --- 권한 레지스트리 조립 (#277) -----------------------------------------------------


def _serve_with(tmp_path, monkeypatch, orchestrator: dict):
    """진입점을 데몬 없이 돌려 조립된 앱과 배포 관리자, 노출되는 계측기를 돌려준다."""
    from aaestro.observability.metrics import Metrics

    write_config(
        tmp_path, {"run_store": str(tmp_path / "runs.db"), "control_port": 18999, **orchestrator}
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    served_metrics = Metrics()
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: served_metrics)

    class Deployments:
        author = None
        images = None
        access = None
        launcher = None

        def in_use(self, kind: str, name: str) -> bool:
            return False

    deployments = Deployments()
    monkeypatch.setattr(entrypoint, "_deployment_manager", lambda *a, **k: deployments)
    captured: dict = {}
    monkeypatch.setattr(
        entrypoint, "_run_service", lambda *a, **k: captured.update(run_metrics=k.get("metrics"))
    )
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kwargs: captured.update(app=app))

    entrypoint.main()
    return captured, deployments, served_metrics


async def test_the_registry_reaches_deployments_and_the_served_app(tmp_path, monkeypatch):
    """배포에 물리지 않으면 신원이 발급되지 않고, 앱에 없으면 강제 지점이 물을 곳이 없다."""
    import httpx

    from aaestro.access.registry import AccessRegistry

    captured, deployments, served_metrics = _serve_with(
        tmp_path,
        monkeypatch,
        {
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": "enforcer",
            "access_agent_url": "http://control-plane:8700",
            "access_stewards": ["permission-agent"],
        },
    )

    assert isinstance(deployments.access, AccessRegistry)
    assert deployments.access.stewards == frozenset({"permission-agent"})
    assert deployments.access_url == "http://control-plane:8700", "에이전트가 표를 받을 곳을 모른다"
    from aaestro.access.baselines import A2ABaseline, MemoryBaseline
    from aaestro.access.model import ResourceKind

    # 선언 판정이 빠지면 선언된 메모리·연결이 전부 기본 거부된다
    assert isinstance(deployments.access.baselines[ResourceKind.MEMORY], MemoryBaseline)
    a2a = deployments.access.baselines[ResourceKind.A2A]
    assert isinstance(a2a, A2ABaseline)
    assert a2a.store is deployments.access.store, "배포 기록과 다른 저장소를 보면 그래프를 모른다"
    from aaestro.access.baselines import EgressBaseline, McpToolBaseline

    assert isinstance(deployments.access.baselines[ResourceKind.EGRESS], EgressBaseline)
    # 빠지면 선언된 원격 MCP 도구가 전부 기본 거부된다 (#282)
    assert isinstance(deployments.access.baselines[ResourceKind.MCP_TOOL], McpToolBaseline)
    # 계측기는 노출되는 것 하나 — 따로 만들면 기록은 되는데 아무도 못 본다
    assert deployments.access.metrics is served_metrics
    assert captured["run_metrics"] is served_metrics
    transport = httpx.ASGITransport(app=captured["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://cp") as api:
        response = await api.get(
            "/v1/access/changes?wait_s=0", headers={"Authorization": "Bearer enforcer"}
        )
    assert response.status_code == 200


def test_an_exposed_registry_without_an_enforcer_token_is_refused(tmp_path, monkeypatch):
    write_config(
        tmp_path,
        {
            "run_store": str(tmp_path / "runs.db"),
            "control_host": "0.0.0.0",  # noqa: S104 — 거절되는지 보려는 설정
            "control_token": "control",
            "access_store": str(tmp_path / "access.db"),
        },
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    with pytest.raises(AaestroError) as exc_info:
        entrypoint._start(None)

    assert exc_info.value.code == ErrorCode.CFG_001
    assert exc_info.value.details["setting"] == "orchestrator.access_enforcer_token"


def test_a_registry_without_an_agent_url_is_refused(tmp_path, monkeypatch):
    """주소가 없으면 A2A 가 그래프 전체가 쥔 공유 키로 되돌아간다 — 기동하지 않는다 (#281)."""
    write_config(
        tmp_path,
        {
            "run_store": str(tmp_path / "runs.db"),
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": "enforcer",
        },
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    with pytest.raises(AaestroError) as exc_info:
        entrypoint._start(None)

    assert exc_info.value.code == ErrorCode.CFG_001
    assert exc_info.value.details["setting"] == "orchestrator.access_agent_url"


# --- 이그레스 프록시 (#293) ------------------------------------------------------------


EGRESS = {
    "connect_url": "http://aaestro-egress:8080",
    "providers_url": "http://aaestro-egress:8081",
}


@pytest.mark.parametrize("container", [None, "aaestro-egress-1"])
def test_the_egress_proxy_reaches_deployments(tmp_path, monkeypatch, container):
    """배포에 물리지 않으면 에이전트는 설정과 무관하게 모델 키를 받고 직접 나간다.

    프록시 컨테이너는 사이드카 네트워크에 붙일 대상이다 (#304) — 에이전트가 쓰는 이름을 별칭으로.
    """
    from aaestro.runtime.deployments import EgressEndpoints
    from aaestro.runtime.sidecars import ProxyAttachment

    orchestrator = {
        "access_store": str(tmp_path / "access.db"),
        "access_enforcer_token": "enforcer",
        "access_agent_url": "http://control-plane:8700",
    }
    original = write_config

    def with_runtime(directory, values):
        (directory / "local.yaml").write_text(
            yaml.safe_dump(
                {
                    "orchestrator": values,
                    "runtime": {"egress_proxy": {**EGRESS, "container": container}},
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setitem(globals(), "write_config", with_runtime)
    try:
        _, deployments, _ = _serve_with(tmp_path, monkeypatch, orchestrator)
    finally:
        globals()["write_config"] = original

    assert deployments.egress == EgressEndpoints(**EGRESS)
    expected = ProxyAttachment(container, "aaestro-egress") if container else None
    assert deployments.sidecar_proxy == expected


def test_an_egress_proxy_without_the_registry_is_refused(tmp_path, monkeypatch):
    (tmp_path / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "orchestrator": {"run_store": str(tmp_path / "runs.db")},
                "runtime": {"egress_proxy": EGRESS},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    with pytest.raises(AaestroError) as exc_info:
        entrypoint._start(None)

    assert exc_info.value.details["setting"] == "runtime.egress_proxy"


def test_agents_run_on_an_internal_network_exactly_when_an_egress_proxy_is_configured():
    """프록시가 유일한 출구여야 판정을 우회하지 못한다 — 없으면 게시 포트 그대로다 (#280)."""
    from aaestro.config import RuntimeConfig
    from tests.fixtures.fake_docker import FakeDockerClient

    isolated = entrypoint.agent_engine(
        RuntimeConfig(network="agents", egress_proxy=EGRESS), FakeDockerClient()
    )
    plain = entrypoint.agent_engine(RuntimeConfig(network="agents"), FakeDockerClient())

    assert (isolated.network, isolated.internal) == ("agents", True)
    assert plain.internal is False


def test_the_startup_grace_and_ready_timeout_reach_the_runtime(tmp_path, monkeypatch):
    """유예만 올리고 배포 대기를 안 올리면 배포가 먼저 끝난다 — 둘 다 설정에서 와야 한다 (#297)."""
    from aaestro.catalog import Catalog
    from aaestro.config import AaestroConfig, OrchestratorConfig

    monkeypatch.setattr(entrypoint, "agent_engine", lambda runtime, client: None)
    config = AaestroConfig.model_validate({"runtime": {"health_check": {"startup_grace_s": 120}}})
    orchestrator = OrchestratorConfig(
        deployment_store=str(tmp_path / "deployments.db"), deployment_ready_timeout_s=180
    )

    deployments = entrypoint._deployment_manager(  # noqa: SLF001 — 조립 함수를 직접 본다
        config, Catalog.under(tmp_path), store_root=tmp_path, orchestrator=orchestrator
    )

    assert deployments.ready_timeout_s == 180
    assert deployments.launcher.startup_grace_s == 120
    # 기본값도 같은 관계를 지킨다 — 유예가 배포 대기보다 길면 유예가 의미를 잃는다
    assert (
        OrchestratorConfig().deployment_ready_timeout_s
        > AaestroConfig().runtime.health_check.startup_grace_s
    )


@pytest.mark.parametrize(
    ("proxy", "host", "warned"),
    [
        (True, "0.0.0.0", True),  # noqa: S104 — 판정 입력이다, 바인드가 아니다
        (True, "10.0.0.5", True),
        (True, "127.0.0.1", False),
        (True, "::1", False),
        (False, "0.0.0.0", False),  # noqa: S104
    ],
)
def test_isolated_agents_reaching_the_control_plane_is_warned(proxy, host, warned):
    """격리돼도 브리지 게이트웨이는 호스트다 — 밖에 열린 control plane 을 알린다 (#303)."""
    from aaestro.config import RuntimeConfig

    runtime = RuntimeConfig(
        egress_proxy=(
            {"connect_url": "http://egress:8080", "providers_url": "http://egress:8081"}
            if proxy
            else None
        )
    )

    assert entrypoint.warn_if_agents_reach_the_host(runtime, host) is warned


def test_the_entrypoint_checks_host_reachability(tmp_path, monkeypatch):
    """경고 함수만 있고 진입점이 부르지 않으면 아무도 모른다 — 배선을 본다."""
    calls = []
    monkeypatch.setattr(
        entrypoint, "warn_if_agents_reach_the_host", lambda runtime, host: calls.append(host)
    )

    _serve_with(tmp_path, monkeypatch, {})

    assert calls == ["127.0.0.1"]


def test_the_served_run_service_resolves_node_models_from_the_catalog(tmp_path):
    """resolver 를 물리지 않으면 배포의 model_overrides 가 기록만 되고 태스크에 닿지 않는다."""
    from types import SimpleNamespace

    from aaestro.catalog import Catalog
    from aaestro.orchestrator.models import CatalogModels

    catalog = Catalog.under(tmp_path)
    config = SimpleNamespace(
        orchestrator=SimpleNamespace(
            max_concurrent_runs=1,
            max_service_runs=1,
            checkpointer="memory",
            checkpointer_url=None,
            node_timeout_s=30.0,
            state_artifact_root=None,
        )
    )

    runs = entrypoint._run_service(
        config, catalog, SimpleNamespace(launcher=None), store=SqliteRunStore()
    )

    assert runs.submitter.models == CatalogModels(catalog)
    assert runs.submitter.artifacts is None


def test_the_served_run_service_keeps_compaction_originals_under_the_configured_root(tmp_path):
    """#371 — 설정 키가 저장소로 이어지지 않으면 summarize 그래프는 운영에서 늘 거절된다."""
    from types import SimpleNamespace

    from aaestro.catalog import Catalog

    config = SimpleNamespace(
        orchestrator=SimpleNamespace(
            max_concurrent_runs=1,
            max_service_runs=1,
            checkpointer="memory",
            checkpointer_url=None,
            node_timeout_s=30.0,
            state_artifact_root=str(tmp_path / "originals"),
        )
    )

    runs = entrypoint._run_service(
        config, Catalog.under(tmp_path), SimpleNamespace(launcher=None), store=SqliteRunStore()
    )

    store = runs.submitter.artifacts
    assert store.root == tmp_path / "originals"
    # 에이전트의 artifact 스코프(local/group/global)가 아닌 오케스트레이터 전용 네임스페이스
    from aaestro.orchestrator.state_artifacts import STATE_ARTIFACT_SCOPE

    assert store.scope == STATE_ARTIFACT_SCOPE


# --- 프록시 upstream 대조 (#358) ---------------------------------------------------------


def test_proxy_providers_is_none_without_a_proxy_and_the_declared_set_with_one():
    from aaestro.config import RuntimeConfig

    plain = RuntimeConfig()
    proxied = RuntimeConfig(egress_proxy={**EGRESS, "providers": ["anthropic", "openai-compat"]})

    class Holder:
        def __init__(self, runtime):
            self.runtime = runtime

    assert entrypoint._proxy_providers(Holder(plain)) is None  # noqa: SLF001
    assert entrypoint._proxy_providers(Holder(proxied)) == frozenset(  # noqa: SLF001
        {"anthropic", "openai-compat"}
    )


def test_the_served_author_and_the_deployment_wiring_get_the_proxy_providers(tmp_path, monkeypatch):
    """한 곳이라도 빠지면 그 경로의 검증만 프록시 upstream 을 모른 채 통과한다."""
    (tmp_path / "local.yaml").write_text(
        yaml.safe_dump(
            {
                "orchestrator": {
                    "run_store": str(tmp_path / "runs.db"),
                    "control_port": 18999,
                    "access_store": str(tmp_path / "access.db"),
                    "access_enforcer_token": "enforcer",
                    "access_agent_url": "http://control-plane:8700",
                },
                "runtime": {
                    "egress_proxy": {**EGRESS, "providers": ["anthropic", "openai-compat"]}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AAESTRO_ENV", "local")
    monkeypatch.setenv("AAESTRO_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(entrypoint, "_setup_observability", lambda: None)

    class Deployments:
        author = None
        images = None
        access = None
        launcher = None

        def in_use(self, kind: str, name: str) -> bool:
            return False

    deployments = Deployments()
    monkeypatch.setattr(entrypoint, "_deployment_manager", lambda *a, **k: deployments)
    monkeypatch.setattr(entrypoint, "_run_service", lambda *a, **k: None)
    captured: dict = {}
    monkeypatch.setattr(
        entrypoint, "create_app", lambda store, **kw: captured.update(kw) or object()
    )
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **_kw: None)
    expected = frozenset({"anthropic", "openai-compat"})

    entrypoint.main()

    assert captured["author"].providers == expected
    assert deployments.author.providers == expected


def test_the_deployment_managers_own_author_gets_the_proxy_providers(tmp_path, monkeypatch):
    """배포 관리자가 안에서 만드는 Author — 진입점의 두 번째 조립 지점."""
    from aaestro.catalog import Catalog
    from aaestro.config import AaestroConfig, OrchestratorConfig

    monkeypatch.setattr(entrypoint, "agent_engine", lambda runtime, client: None)
    config = AaestroConfig.model_validate(
        {"runtime": {"egress_proxy": {**EGRESS, "providers": ["anthropic", "openai-compat"]}}}
    )
    orchestrator = OrchestratorConfig(deployment_store=str(tmp_path / "deployments.db"))

    manager = entrypoint._deployment_manager(  # noqa: SLF001
        config, Catalog.under(tmp_path), store_root=tmp_path, orchestrator=orchestrator
    )

    assert manager.author.providers == frozenset({"anthropic", "openai-compat"})
