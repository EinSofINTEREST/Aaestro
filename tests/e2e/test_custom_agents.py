"""Custom agents on the real stack — materials, build, gate, run (#266).

재료가 있는 에이전트는 그 버전의 이미지가 `built` 여야 배포된다. 단위 테스트는 게이트의
판단을 대역 위에서 본다. 여기서는 **실제로 굽고, 실제로 거절당하고, 실제로 도는지** 본다:

- 빌드가 실패한 에이전트는 기동 전에 409 로 거절된다
- 매니페스트가 다른 이미지를 선언하면 400 으로 거절된다
- 저장소에서 지운 claude-code 가 시드 재료만으로 구워지고, 배포되고, run 을 완주한다

claude-code 실행기는 CLI 명령을 env 로 받는다. 여기서는 그 명령을 **파이썬 한 줄**로
바꿔 실 모델을 부르지 않는다 — 실행기 코드와 구운 이미지는 그대로 탄다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any

import pytest
import yaml

from tests.e2e.conftest import (
    CONTROL_TOKEN,
    REPO_ROOT,
    api,
    memory_tokens,
    plane_healthy,
    plane_url,
    start_plane,
    stop,
    until,
    write_config,
)
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

SEED = REPO_ROOT / "examples" / "materials" / "claude-code"
FAKE_RESULT = "written by the fake cli"
# 실행기는 이 명령 뒤에 프롬프트를 인자로 붙인다 — `python -c` 는 남는 인자를 무시한다
FAKE_CLI = f"python -c \"import json; print(json.dumps({{'result': '{FAKE_RESULT}'}}))\""
BUILD_DEADLINE_S = 900.0


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    """모듈 전용 control plane — claude-code 의 CLI 를 대역으로 바꾼 설정."""
    config_dir = write_config(tmp_path, agent_env={"AAESTRO_CLAUDE_COMMAND": FAKE_CLI})
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    process = start_plane(config_dir, tokens_path)
    try:
        until(plane_healthy, what="control plane health")
        yield {"process": process}
    finally:
        stop(process)
        for name in ("aaestro-claude-code-0", "aaestro-e2e-broken-0", "aaestro-planner-0"):
            docker("rm", "-f", name, check=False)


def cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "aaestro.cli", "--json", *args, "--control-url", plane_url()],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_DEADLINE_S,
        env={**os.environ, "AAESTRO_CONTROL_TOKEN": CONTROL_TOKEN},
    )


def single_node_graph(name: str, agent_ref: str) -> dict[str, Any]:
    return {
        "apiVersion": "aaestro/v1",
        "kind": "Graph",
        "metadata": {"name": name, "version": "0.1.0", "description": "custom agent e2e"},
        "spec": {
            "mode": "mission",
            "goal": "custom agent e2e",
            "state": {"schema": "aaestro.graphs.schemas:ResearchState"},
            "nodes": [
                {
                    # claude-code 프롬프트셋은 default 템플릿만 둔다 — 노드 id 가 템플릿 이름이다
                    "id": "default",
                    "agent": agent_ref,
                    "input_map": {"prompt": "state.query"},
                    "output_map": {"report": "output.result"},
                }
            ],
            "edges": [{"from": "START", "to": "default"}, {"from": "default", "to": "END"}],
        },
    }


def image_of(container: str) -> str:
    return docker("inspect", "--format", "{{.Config.Image}}", container)


def wait_for_build(agent: str) -> dict[str, Any]:
    def finished() -> dict[str, Any] | None:
        _, record = api("GET", f"/v1/agents/{agent}/image")
        return record if record["status"] != "building" else None

    return until(finished, what=f"{agent} build to finish", timeout_s=BUILD_DEADLINE_S)


def test_a_failed_build_and_a_mismatched_image_are_refused_before_anything_starts(plane):
    """굽기가 실패했으면 409, 매니페스트가 다른 이미지를 가리키면 400 — 둘 다 컨테이너 0개."""
    # 파일에서 읽는다 — GET 응답은 파이썬 필드명(api_version)이라 그대로 PUT 할 문서가 아니다
    manifest = yaml.safe_load(
        (REPO_ROOT / "agents" / "claude-code" / "manifest.yaml").read_text(encoding="utf-8")
    )
    manifest["metadata"]["name"] = "e2e-broken"
    manifest["spec"]["runtime"]["image"] = "aaestro/agent-e2e-broken:0.1.0"
    manifest["spec"]["runtime"]["volumes"] = []
    try:
        status, saved = api("PUT", "/v1/agents/e2e-broken", manifest)
        assert status == 200, saved
        status, saved = api(
            "PUT",
            "/v1/graphs/e2e-broken",
            single_node_graph("e2e-broken", "agents/e2e-broken@0.1.0"),
        )
        assert status == 200, saved

        # --- 규약은 지키지만 굽는 도중 실패하는 재료
        status, stored = api(
            "PUT",
            "/v1/agents/e2e-broken/materials",
            {
                "files": {
                    "Dockerfile": "FROM aaestro/agent-base:0.1.0\nRUN exit 3\n",
                    "src/agent.py": "MARK = 1\n",
                }
            },
        )
        assert status == 200, stored
        status, started = api("POST", "/v1/agents/e2e-broken/image")
        assert status == 202 and started["status"] == "building", started
        record = wait_for_build("e2e-broken")
        assert record["status"] == "failed", record

        status, refused = api("POST", "/v1/deployments", {"graph": "e2e-broken"})
        assert status == 409, refused
        assert refused["error"]["code"] == "RT_012"
        assert refused["error"]["details"]["build_status"] == "failed"
        assert docker("ps", "-aq", "--filter", "name=^aaestro-e2e-broken-") == ""

        # --- 레퍼런스 planner 는 base 이미지를 선언한다 — 재료를 주면 두 이미지가 된다
        status, stored = api(
            "PUT", "/v1/agents/planner/materials", {"files": {"src/agent.py": "MARK = 2\n"}}
        )
        assert status == 200, stored
        status, refused = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
        assert status == 400, refused
        assert refused["error"]["code"] == "VAL_002"
        assert refused["error"]["details"] == {
            "declared": "aaestro/agent-base:0.1.0",
            "built": "aaestro/agent-planner:0.4.0",
        }
        assert docker("ps", "-aq", "--filter", "name=^aaestro-planner-") == ""
    finally:
        api("DELETE", "/v1/agents/planner/materials")
        api("DELETE", "/v1/graphs/e2e-broken")
        api("DELETE", "/v1/agents/e2e-broken")
        # 삭제는 매니페스트만 지운다 (#258) — 테스트가 만든 빈 디렉토리는 테스트가 치운다
        leftover = REPO_ROOT / "agents" / "e2e-broken"
        if leftover.is_dir() and not any(leftover.iterdir()):
            leftover.rmdir()


def test_claude_code_bakes_from_its_seed_deploys_and_completes_a_run(plane):
    """#266 완료 조건 — 저장소에서 src/ 와 Dockerfile 이 사라져도 에이전트가 동작한다."""
    assert not (REPO_ROOT / "agents" / "claude-code" / "src").exists()

    pushed = cli("agent-push", "claude-code", str(SEED))
    assert pushed.returncode == 0, pushed.stdout + pushed.stderr
    assert json.loads(pushed.stdout)["files"] == ["Dockerfile", "src/agent.py"]

    deployment_id = None
    try:
        status, saved = api(
            "PUT", "/v1/graphs/e2e-code", single_node_graph("e2e-code", "agents/claude-code@0.1.0")
        )
        assert status == 200, saved

        # --- 재료만 있고 굽지 않았다 — 배포가 대신 굽지 않는다
        status, refused = api("POST", "/v1/deployments", {"graph": "e2e-code"})
        assert status == 409, refused
        assert refused["error"]["code"] == "RT_012"
        assert refused["error"]["details"]["build_status"] is None

        built = cli("agent-build", "claude-code", "--wait", "--timeout-s", str(BUILD_DEADLINE_S))
        assert built.returncode == 0, built.stdout + built.stderr
        assert json.loads(built.stdout)["image"] == "aaestro/agent-claude-code:0.1.0"

        # --- 구운 뒤에는 배포되고, 구운 이미지로 돈다
        status, deployment = api("POST", "/v1/deployments", {"graph": "e2e-code"})
        assert status == 201, deployment
        deployment_id = deployment["deployment_id"]
        assert deployment["status"] == "ready", deployment
        assert image_of("aaestro-claude-code-0") == "aaestro/agent-claude-code:0.1.0"
        loaded = docker(
            "exec",
            "aaestro-claude-code-0",
            "python",
            "-c",
            "import agent; print(agent.ClaudeCodeExecutor.__name__)",
        )
        assert loaded == "ClaudeCodeExecutor"

        # --- run 이 실행기를 거쳐 완주한다
        status, submitted = api(
            "POST", "/v1/runs", {"deployment_id": deployment_id, "input": {"query": "hello"}}
        )
        assert status == 202, submitted

        def finished() -> dict[str, Any] | None:
            _, current = api("GET", f"/v1/runs/{submitted['run_id']}")
            return current if current["status"] != "running" else None

        done = until(finished, what="the claude-code run to finish")
        assert done["status"] == "completed", done
        assert done["state"]["report"] == FAKE_RESULT, done
    finally:
        if deployment_id is not None:
            api("DELETE", f"/v1/deployments/{deployment_id}")
        api("DELETE", "/v1/graphs/e2e-code")
        # 구운 이미지를 남기면 다음 실행이 **굽지 않고도** 태그를 찾는다 — 매번 시드에서 굽는다
        docker("rmi", "-f", "aaestro/agent-claude-code:0.1.0", check=False)
