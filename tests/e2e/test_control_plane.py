"""Operating a run from outside the process that drives it.

`create_app` 은 표면을 갖고 있었지만 **그 앱을 만드는 곳이 테스트뿐이었다** —
`aaestro run-list` / `run-drain` / `run-resume` 가 붙을 서버가 없었다 (#221).

여기서는 프로세스를 **실제로 셋** 띄운다: 상주 run 을 구동하는 프로세스, 그것을
서빙하는 control plane, 그리고 조작하는 CLI. 같은 프로세스 안에서 객체만 새로
만들면 이 이슈가 묻는 경계를 건너지 않는다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.e2e.test_stack import (
    AGENT_TOKEN,
    COMPOSE_FILE,
    compose_up,
    docker,
    requires_docker,
    wait_healthy,
)

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
# `--agent` 는 **에이전트 이름**으로 키를 잡는다 (노드 id 가 아니다) —
# feed-monitor 의 watcher/classifier/notifier 노드가 각각 이들을 쓴다
AGENT_PORTS = {"researcher": 18083, "planner": 18082, "writer": 18084}
CONTROL_PORT = 18701
CONTROL_TOKEN = "e2e-control-token"  # noqa: S105
METRICS_PORT = 19701
DEADLINE_S = 90.0


@pytest.fixture(scope="module")
def service_stack() -> Iterator[dict[str, int]]:
    compose_up()
    try:
        for port in AGENT_PORTS.values():
            assert wait_healthy(f"http://127.0.0.1:{port}"), f"agent on {port} never became healthy"
        yield AGENT_PORTS
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


def write_config(directory: Path, store_path: Path) -> Path:
    """CLI 와 control plane 이 **같은** 저장소를 보게 하는 설정."""
    (directory / "e2e.yaml").write_text(
        yaml.safe_dump(
            {
                "orchestrator": {
                    "run_store": str(store_path),
                    "control_port": CONTROL_PORT,
                    # 인증을 켠 채로 돈다 — 무인증으로 통과하면 인증 회귀를 못 잡는다 (#241)
                    "control_token": CONTROL_TOKEN,
                    "service_defaults": {"idle_min_delay_s": 0.1, "idle_max_delay_s": 0.1},
                }
            }
        ),
        encoding="utf-8",
    )
    return directory


def cli(*argv: str, config_dir: Path) -> subprocess.CompletedProcess[str]:
    """CLI 를 **별도 프로세스로** 실행한다."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "aaestro.cli", *argv],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
        env={
            **os.environ,
            "AAESTRO_ENV": "e2e",
            "AAESTRO_CONFIG_DIR": str(config_dir),
            "AAESTRO_CONTROL_TOKEN": CONTROL_TOKEN,
        },
    )


def until(predicate, *, what: str, timeout_s: float = DEADLINE_S):
    """조건이 설 때까지 기다린다 — 서지 않으면 그 사실로 실패한다."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {timeout_s}s")


@pytest.fixture
def deployment(service_stack: dict[str, int], tmp_path) -> Iterator[dict]:
    """상주 run 을 구동하는 프로세스와, 그것을 서빙하는 control plane."""
    config_dir = write_config(tmp_path, tmp_path / "runs.db")
    run_id = f"svc-e2e-{os.getpid()}"
    agents = [f"--agent={name}=http://127.0.0.1:{port}" for name, port in service_stack.items()]

    driver = subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            "-m",
            "aaestro.cli",
            "run",
            str(REPO_ROOT / "graphs" / "feed-monitor.yaml"),
            "--service",
            "--run-id",
            run_id,
            "--env",
            "e2e",
            "--config-dir",
            str(config_dir),
            *agents,
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={**os.environ, "AAESTRO_AGENT_TOKEN": AGENT_TOKEN},
    )
    plane = subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "aaestro.orchestrator"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            **os.environ,
            "AAESTRO_ENV": "e2e",
            "AAESTRO_CONFIG_DIR": str(config_dir),
            "AAESTRO_METRICS_PORT": str(METRICS_PORT),
        },
    )
    try:
        yield {"run_id": run_id, "config_dir": config_dir, "driver": driver, "plane": plane}
    finally:
        for process in (plane, driver):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:  # pragma: no cover - 방어
                    process.kill()


def listed(config_dir: Path, run_id: str) -> dict | None:
    """control plane 이 보고하는 run — 아직 없으면 None."""
    result = cli(
        "--json",
        "run-list",
        "--mode",
        "service",
        f"--control-url=http://127.0.0.1:{CONTROL_PORT}",
        config_dir=config_dir,
    )
    if result.returncode != 0:
        return None
    try:
        runs = json.loads(result.stdout).get("runs", [])
    except json.JSONDecodeError:  # pragma: no cover - 서버가 아직 안 떴다
        return None
    return next((run for run in runs if run["run_id"] == run_id), None)


@requires_docker
def test_a_running_service_is_visible_from_another_process(deployment):
    """#221 — 진입점이 없어 `run-list` 가 붙을 곳이 없었다."""
    run = until(
        lambda: listed(deployment["config_dir"], deployment["run_id"]),
        what="the service run appearing in the control plane",
    )

    assert run["graph"] == "feed-monitor"
    assert run["mode"] == "service"


@requires_docker
def test_a_drain_request_stops_the_driving_process(deployment):
    """조회만 되고 조작이 안 되면 운영 표면이 아니다.

    drain 은 **요청만 남기고** 즉시 반환한다 — 실제 정지는 구동 프로세스가
    iteration 경계에서 수행한다. 그 프로세스가 끝나는 것으로 확인한다.
    """
    until(
        lambda: listed(deployment["config_dir"], deployment["run_id"]),
        what="the service run appearing in the control plane",
    )

    drained = cli(
        "run-drain",
        deployment["run_id"],
        f"--control-url=http://127.0.0.1:{CONTROL_PORT}",
        config_dir=deployment["config_dir"],
    )
    assert drained.returncode == 0, drained.stdout + drained.stderr

    driver = deployment["driver"]
    until(lambda: driver.poll() is not None, what="the driving process stopping")
    assert "stopped" in driver.stdout.read()


@requires_docker
def test_resume_is_refused_rather_than_silently_accepted(deployment):
    """이 control plane 은 run 을 구동하지 않는다 — 조용히 성공하면 오해한다."""
    until(
        lambda: listed(deployment["config_dir"], deployment["run_id"]),
        what="the service run appearing in the control plane",
    )

    refused = cli(
        "run-resume",
        deployment["run_id"],
        f"--control-url=http://127.0.0.1:{CONTROL_PORT}",
        config_dir=deployment["config_dir"],
    )

    assert refused.returncode != 0, "재개되지 않았는데 성공으로 보고했다"
    assert "does not drive the run" in refused.stdout


@requires_docker
def test_an_unknown_run_is_reported_as_missing(deployment):
    """조작이 먹히지 않았음을 404 로 말해야 한다."""
    until(
        lambda: listed(deployment["config_dir"], deployment["run_id"]),
        what="the service run appearing in the control plane",
    )

    missing = cli(
        "run-drain",
        "no-such-run",
        f"--control-url=http://127.0.0.1:{CONTROL_PORT}",
        config_dir=deployment["config_dir"],
    )

    assert missing.returncode != 0
    assert "NF_001" in missing.stdout
