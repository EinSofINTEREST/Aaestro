"""A slow-starting agent reaches Ready without being restarted (#297).

기동은 health 확인 창(`interval_s × unhealthy_threshold`)보다 길 수 있다 — MCP 서버 기동,
원격 initialize. 유예가 없으면 뜨는 중인 컨테이너가 재시작되고, 같은 시간이 다시 걸려 배포가
실패한다.

느린 기동은 **선언으로** 만든다: writer 에게 말하지 않는 stdio MCP 서버를 `optional` 로 붙이면
agentd 가 기동 예산(서버당 15초)을 다 쓰고 degraded 로 계속한다 — E2E 의 확인 창(3초 × 3)보다 길다.
"""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import (
    REPO_ROOT,
    api,
    deployed_containers,
    memory_tokens,
    plane_healthy,
    start_plane,
    stop,
    until,
    write_config,
)
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

WRITER = "aaestro-writer-0"
SLOW_SERVER = {
    "name": "slow",
    "transport": "stdio",
    # 이미지에 있는 실행 파일 (03 Security 5) — 말하지 않으므로 initialize 가 예산을 다 쓴다
    "command": ["sleep", "30"],
    "optional": True,
}


def workspace(tmp_path: Path) -> Path:
    """저장소 선언 사본 — writer 만 느리게 뜨게 한다."""
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest_path = root / "agents" / "writer" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["mcp"] = {"servers": [SLOW_SERVER]}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


def plane_with(tmp_path: Path, grace: float, ready_timeout: float) -> Iterator[dict[str, Any]]:
    """유예와 배포 대기는 함께 올린다 — 유예만 올리면 배포가 먼저 끝난다 (#297)."""
    config_dir = write_config(tmp_path, orchestrator={"deployment_ready_timeout_s": ready_timeout})
    config = yaml.safe_load((config_dir / "e2e.yaml").read_text(encoding="utf-8"))
    config["runtime"]["health_check"]["startup_grace_s"] = grace
    (config_dir / "e2e.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    process = start_plane(
        config_dir, tokens_path, env={"AAESTRO_REPO_ROOT": str(workspace(tmp_path))}
    )
    try:
        until(plane_healthy, what="control plane health")
        yield {"process": process}
    finally:
        stop(process)
        for name in deployed_containers():
            docker("rm", "-f", name, check=False)


@pytest.fixture
def patient(stack, tmp_path) -> Iterator[dict[str, Any]]:
    """기동 유예를 켠 control plane — 느린 기동을 받도록 배포 대기도 함께 올린다."""
    yield from plane_with(tmp_path, grace=90.0, ready_timeout=150.0)


@pytest.fixture
def impatient(stack, tmp_path) -> Iterator[dict[str, Any]]:
    """유예가 없는 control plane — #297 이전의 동작."""
    yield from plane_with(tmp_path, grace=0.0, ready_timeout=60.0)


def first_container() -> str:
    """배포가 도는 동안 **처음 선** writer 컨테이너의 id — 갈아 끼우면 id 가 바뀐다."""
    for _ in range(600):
        found = docker("inspect", "-f", "{{.Id}}", WRITER, check=False)
        if found and not found.startswith("Error"):
            return found
        time.sleep(0.2)
    raise AssertionError("writer container never appeared")


def test_a_slow_starting_agent_becomes_ready_without_being_restarted(patient):
    with ThreadPoolExecutor(max_workers=1) as pool:
        watching = pool.submit(first_container)
        status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
        started = watching.result(timeout=30)

    assert status == 201, record
    assert record["status"] == "ready", record
    # 배포 응답은 모든 레플리카가 Ready 가 된 순간의 관측이다 (#487)
    assert {a["state"] for a in record["agents"]} == {"ready"}, record
    # **처음 선 그 컨테이너**가 Ready 가 됐다 — 재시작이면 id 가 달라진다 (시간 비교로는 못 가른다)
    assert docker("inspect", "-f", "{{.Id}}", WRITER) == started, "기동 중에 컨테이너가 갈렸다"
    logs = docker("logs", "--tail", "60", WRITER, check=False)
    assert "agentd starting" in logs, logs[-600:]


def test_without_the_grace_the_same_agent_never_deploys(impatient):
    """유예가 없으면 뜨는 중에 재시작돼 배포가 RT_002 로 끝난다 — 이것이 #297 의 증상이다."""
    status, refused = api("POST", "/v1/deployments", {"graph": "research-pipeline"})

    assert status == 503, refused
    assert refused["error"]["code"] == "RT_002", refused
    until(lambda: not deployed_containers(), what="rolled back", timeout_s=60)
