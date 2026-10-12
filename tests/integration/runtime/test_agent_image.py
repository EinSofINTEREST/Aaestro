"""Integration tests for the agent base image.

실제 컨테이너를 기동해 격리 계약을 검증한다 — non-root 실행, healthcheck,
Control API 응답. Docker 가 없으면 skip 한다 (게이트를 막지 않는다).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_DIR = REPO_ROOT / "deployments" / "docker"
ECHO_IMAGE = "aaestro/agent-echo:0.1.0"
NETWORK = "aaestro-net-test"
READY_TIMEOUT_S = 90.0


def docker(*args: str, check: bool = True) -> str:
    """docker CLI 호출."""
    result = subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def docker_available() -> bool:
    """Docker daemon 에 접속 가능한지."""
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(  # noqa: S603
            ["docker", "info"],  # noqa: S607
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


requires_docker = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")


@pytest.fixture(scope="module")
def echo_image() -> str:
    """echo 이미지를 빌드한다 — base 이미지도 함께 만들어진다."""
    docker(
        "build",
        "-t",
        "aaestro/agent-base:0.1.0",
        "-f",
        str(DOCKER_DIR / "agent-base.Dockerfile"),
        str(REPO_ROOT),
    )
    docker(
        "build",
        "-t",
        ECHO_IMAGE,
        "-f",
        str(DOCKER_DIR / "agent-echo.Dockerfile"),
        str(REPO_ROOT),
    )
    return ECHO_IMAGE


@pytest.fixture(scope="module")
def network() -> Iterator[str]:
    """전용 bridge 네트워크 — finalizer 가 반드시 정리한다."""
    docker("network", "create", "--driver", "bridge", NETWORK, check=False)
    try:
        yield NETWORK
    finally:
        docker("network", "rm", NETWORK, check=False)


@pytest.fixture
def echo_container(echo_image: str, network: str) -> Iterator[tuple[str, int]]:
    """echo 에이전트 컨테이너 — finalizer 가 반드시 제거한다."""
    name = f"aaestro-echo-{uuid.uuid4().hex[:8]}"
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--network",
        network,
        "--read-only",
        "--cap-drop=ALL",
        "--pids-limit=256",
        "--tmpfs=/tmp",
        "--tmpfs=/workspace",
        "-P",
        echo_image,
    )
    try:
        port = int(docker("port", container, "8080/tcp").rsplit(":", 1)[-1])
        yield container, port
    finally:
        docker("rm", "-f", container, check=False)


def request_json(port: int, path: str, body: dict | None = None) -> dict:
    """Control API 호출."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        result: dict = json.loads(response.read())
        return result


def direct_task(payload: dict) -> dict:
    """직렬화된 direct TaskRequest.

    ``node_id`` 부재가 direct 요청을 뜻한다. ``trace`` 는 필수다 — 모든 태스크는
    run 전체를 단일 trace 로 묶을 수 있어야 한다 (05 Run Tracing).
    """
    return {
        "task_id": "t-echo",
        "run_id": "direct-1",
        "node_id": None,
        "input": payload,
        "trace": {"trace_id": "trace-echo"},
    }


def wait_until_healthy(container: str, port: int) -> str:
    """Docker healthcheck 가 healthy 로 전환할 때까지 기다린다."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    state = "starting"
    while time.monotonic() < deadline:
        state = docker("inspect", "--format", "{{.State.Health.Status}}", container, check=False)
        if state in {"healthy", "unhealthy"}:
            return state
        time.sleep(1)
    return state


@requires_docker
def test_container_runs_as_non_root(echo_container: tuple[str, int]) -> None:
    """root 로 도는 에이전트는 격리 경계를 무력화한다 (02 Security 3)."""
    container, _ = echo_container

    uid = docker("exec", container, "id", "-u")

    assert uid == "1000"


@requires_docker
def test_healthcheck_reports_healthy(echo_container: tuple[str, int]) -> None:
    """Docker healthcheck 가 /v1/health 를 호출해 healthy 로 전환한다."""
    container, port = echo_container

    assert wait_until_healthy(container, port) == "healthy"


@requires_docker
def test_invoke_echoes_the_input(echo_container: tuple[str, int]) -> None:
    """기동 → ready → invoke → echo 응답의 왕복 검증."""
    container, port = echo_container
    assert wait_until_healthy(container, port) == "healthy"

    result = request_json(port, "/v1/invoke", direct_task({"msg": "hi"}))

    assert result["status"] == "completed"
    assert result["output"] == {"msg": "hi"}


@requires_docker
def test_invoke_rejects_a_task_without_trace(echo_container: tuple[str, int]) -> None:
    """trace 없는 태스크는 거부된다 — 모든 태스크는 단일 trace 로 묶여야 한다."""
    container, port = echo_container
    assert wait_until_healthy(container, port) == "healthy"

    untraceable = direct_task({"msg": "hi"})
    del untraceable["trace"]

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        request_json(port, "/v1/invoke", untraceable)

    assert exc_info.value.code == 422


@requires_docker
def test_stopped_container_stops_serving(echo_container: tuple[str, int]) -> None:
    """정지 후에는 Control API 가 응답하지 않는다 — 유령 컨테이너 방지."""
    container, port = echo_container
    assert wait_until_healthy(container, port) == "healthy"

    docker("stop", "-t", "5", container)

    with pytest.raises((urllib.error.URLError, OSError)):
        request_json(port, "/v1/health")
