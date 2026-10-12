"""Hot reload in a real agent container (#274).

단위 테스트는 조립과 교체를 본다. 여기서는 **떠 있는 컨테이너**의 모듈 선언을 바꾸고
``POST /v1/reload`` 를 부른 뒤, 재시작 없이 AgentCard 의 skill 목록이 바뀌는지 본다. card 는
실제로 로드된 도구에서 만들어지므로(03 AgentCard 1) 모델 호출 없이 리로드를 관찰할 수 있다.

모듈은 **디렉토리**로 건다 — 단일 파일 바인드는 원자적 교체를 반영하지 못한다(#275).
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
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_IMAGE = "aaestro/agent-base:0.1.0"
TOKEN = "reload-test-token"  # noqa: S105 — 테스트 전용
READY_TIMEOUT_S = 60.0


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True, check=False).returncode == 0  # noqa: S603, S607


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable"),
]


@pytest.fixture(scope="module")
def base_image() -> str:
    """지금 소스로 base 를 굽는다 — 옛 이미지면 고치기 전 agentd 를 검증하게 된다."""
    docker(
        "build",
        "-t",
        BASE_IMAGE,
        "-f",
        str(REPO_ROOT / "deployments" / "docker" / "agent-base.Dockerfile"),
        str(REPO_ROOT),
    )
    return BASE_IMAGE


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    """컨테이너의 /app — researcher 선언과 모듈 사본. 호스트에서 고칠 수 있다."""
    app = tmp_path / "app"
    shutil.copytree(
        REPO_ROOT / "modules", app / "modules", ignore=shutil.ignore_patterns("__pycache__")
    )
    manifest = yaml.safe_load(
        (REPO_ROOT / "agents" / "researcher" / "manifest.yaml").read_text("utf-8")
    )
    manifest["spec"].pop("memory", None)
    manifest["spec"]["a2a"] = {"enabled": False}
    (app / "manifest.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    for path in [app, *app.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return app


@pytest.fixture
def agent(base_image: str, app_dir: Path) -> Iterator[tuple[str, int]]:
    name = f"aaestro-reload-{uuid.uuid4().hex[:8]}"
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--pids-limit=256",
        "--tmpfs=/tmp",
        "--tmpfs=/workspace",
        "--mount",
        f"type=bind,src={app_dir},dst=/app,readonly",
        "-e",
        f"AAESTRO_AGENT_TOKEN={TOKEN}",
        "-e",
        "ANTHROPIC_API_KEY=test-key-not-used",
        "-e",
        "SEARCH_API_KEY=test-key-not-used",
        "-P",
        base_image,
    )
    try:
        port = int(docker("port", container, "8080/tcp").rsplit(":", 1)[-1])
        yield container, port
    finally:
        logs = docker("logs", container, check=False)
        docker("rm", "-f", container, check=False)
        print(logs[-2000:])


def call(port: int, method: str, path: str) -> dict:
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=b"" if method == "POST" else None,
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        result: dict = json.loads(response.read())
        return result


def wait_ready(port: int) -> None:
    deadline = time.monotonic() + READY_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            call(port, "GET", "/v1/health")
            return
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    raise AssertionError("agentd never answered /v1/health")


def skill_names(card: dict) -> set[str]:
    return {skill.get("name", "") for skill in card.get("skills", [])}


def test_reload_changes_the_loaded_tools_without_a_restart(agent, app_dir):
    container, port = agent
    wait_ready(port)
    started = docker("inspect", "-f", "{{.State.StartedAt}}", container)
    before = skill_names(call(port, "GET", "/v1/card"))
    assert not any("search_again" in name for name in before), before

    # 떠 있는 동안 스킬셋 선언에 skill 하나를 더한다 — 원자적 교체로
    declaration = app_dir / "modules" / "skillsets" / "web-search" / "0.2.0" / "skillset.yaml"
    document = yaml.safe_load(declaration.read_text(encoding="utf-8"))
    document["spec"]["skills"].append(
        {
            "name": "search_again",
            "entrypoint": "skills.search:search",
            "description": "리로드 확인용으로 더한 skill",
            "timeout_s": 30,
        }
    )
    staged = declaration.with_suffix(".staged")
    staged.write_text(yaml.safe_dump(document), encoding="utf-8")
    staged.chmod(0o644)
    staged.replace(declaration)

    acknowledged = call(port, "POST", "/v1/reload")
    after = skill_names(call(port, "GET", "/v1/card"))

    assert acknowledged["status"] == "reloaded"
    assert any("search_again" in name for name in after), after
    assert docker("inspect", "-f", "{{.State.StartedAt}}", container) == started, (
        "컨테이너가 재시작됐다"
    )
