"""Shared scaffolding for the end-to-end suite.

컨트롤 플레인과 compose 스택을 띄우는 픽스처는 여러 E2E 모듈이 함께 쓴다 — 모듈 사이에서
픽스처를 import 하면 이름이 겹쳐 조용히 가려지므로 여기에 둔다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.test_stack import COMPOSE_FILE, compose_up, docker, fetch

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_PORT = 18702
CONTROL_TOKEN = "e2e-control-token"  # noqa: S105
METRICS_PORT = 19702
NETWORK = "aaestro-e2e-net"
GRAPH = "research-pipeline"
AGENTS = ("planner", "researcher", "writer")
CONTAINER_NAME = re.compile(rf"aaestro-({'|'.join(AGENTS)})-\d+")
DEADLINE_S = 120.0
HEADERS = {"Authorization": f"Bearer {CONTROL_TOKEN}"}


def until(predicate, *, what: str, timeout_s: float = DEADLINE_S):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(1.0)
    raise AssertionError(f"{what} did not happen within {timeout_s}s")


def memory_tokens() -> dict[str, str]:
    """Memory Service 가 기동하며 발급한 토큰 — compose 볼륨에서 읽는다."""
    raw = docker(
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "memory-service",
        "cat",
        "/tokens/memory.json",
    )
    return json.loads(raw)


def write_config(
    directory: Path,
    *,
    agent_env: dict[str, str] | None = None,
    orchestrator: dict[str, Any] | None = None,
) -> Path:
    """E2E control plane 설정.

    재료·빌드 스토어를 **항상** 켠다 (#266): 켜 두면 배포 게이트가 모든 배포 앞에 선다.
    재료가 없는 레퍼런스 에이전트가 그대로 배포되는 것이 곧 declarative 회귀 확인이다.
    """
    (directory / "e2e.yaml").write_text(
        yaml.safe_dump(
            {
                "runtime": {
                    # 대역 provider 와 Memory Service 가 사는 네트워크에 세운다
                    "network": NETWORK,
                    "agent_env": {
                        "ANTHROPIC_BASE_URL": "http://fake-provider:8000",
                        **(agent_env or {}),
                    },
                    "health_check": {"interval_s": 3, "timeout_s": 2},
                },
                "orchestrator": {
                    "run_store": str(directory / "runs.db"),
                    "deployment_store": str(directory / "deployments.db"),
                    "material_store": str(directory / "materials.db"),
                    "build_store": str(directory / "builds.db"),
                    "control_port": CONTROL_PORT,
                    "control_token": CONTROL_TOKEN,
                    **(orchestrator or {}),
                },
            }
        ),
        encoding="utf-8",
    )
    return directory


def start_plane(
    config_dir: Path, tokens_path: Path, *, env: dict[str, str] | None = None
) -> subprocess.Popen[str]:
    return subprocess.Popen(  # noqa: S603
        [sys.executable, "-m", "aaestro.orchestrator"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env={
            **os.environ,
            "AAESTRO_ENV": "e2e",
            "AAESTRO_CONFIG_DIR": str(config_dir),
            "AAESTRO_REPO_ROOT": str(REPO_ROOT),
            "AAESTRO_METRICS_PORT": str(METRICS_PORT),
            "AAESTRO_MEMORY_URL": "http://memory-service:8090",
            "AAESTRO_MEMORY_TOKENS_PATH": str(tokens_path),
            # secrets 의 값 원천은 control plane 의 환경이다 (02 Secrets Injection)
            "ANTHROPIC_API_KEY": "e2e-fake-key",
            "SEARCH_API_KEY": "e2e-search-key",
            **(env or {}),
        },
    )


def stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - 방어
            process.kill()


def plane_url() -> str:
    return f"http://127.0.0.1:{CONTROL_PORT}"


def plane_healthy() -> bool:
    try:
        return fetch(f"{plane_url()}/v1/health")["status"] == "ok"
    except Exception:  # noqa: BLE001 — 아직 안 떴다
        return False


def api(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
    """control plane 호출 — 4xx 도 본문과 함께 돌려준다 (거절 사유를 검증한다)."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310
        f"{plane_url()}{path}",
        data=data,
        method=method,
        headers={**HEADERS, "content-type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=DEADLINE_S) as response:  # noqa: S310
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None)  # 204 는 본문이 없다
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def deployed_containers() -> dict[str, str]:
    """이 그래프의 에이전트 자리(`aaestro-{agent}-{replica}`)에 서 있는 컨테이너 — 이름 → 상태.

    compose 의 `aaestro-e2e-*` 와 다른 테스트가 남긴 `aaestro-echo-*` 는 제외한다.
    """
    out = docker("ps", "-a", "--format", "{{.Names}}\t{{.Status}}", "--filter", "name=^aaestro-")
    return {
        name: status
        for line in out.splitlines()
        for name, status in [line.split("\t", 1)]
        if CONTAINER_NAME.fullmatch(name)
    }


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    compose_up()
    try:
        yield
    finally:
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    config_dir = write_config(tmp_path)
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    process = start_plane(config_dir, tokens_path)
    state = {"process": process, "config_dir": config_dir, "tokens_path": tokens_path}
    try:
        until(plane_healthy, what="control plane health")
        yield state
    finally:
        stop(state["process"])
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)
