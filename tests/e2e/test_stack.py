"""End-to-end tests over the full compose stack.

compose 로 전체 스택을 띄우고 실제 경로를 검증한다. 모델은 결정적 fake
provider — E2E 에서도 실 LLM 을 호출하지 않는다 (06 Testing 3).

Docker 가 없으면 skip 한다. nightly CI 에서만 실행된다 (PR gate 아님).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "deployments" / "docker" / "compose.e2e.yaml"
ECHO_URL = "http://127.0.0.1:18081"
READY_TIMEOUT_S = 120.0
# compose 가 주입하는 값과 같아야 한다 — 어긋나면 전부 401 이다
# compose 의 ${VAR:-default} 는 **빈 값도** default 로 떨어진다 —
# 여기서 빈 문자열을 그대로 쓰면 테스트와 스택이 다른 토큰을 봐 전부 401 이다
AGENT_TOKEN = os.environ.get("AAESTRO_AGENT_TOKEN") or "e2e-token"
DOCKER_BIN = shutil.which("docker")


def docker(*args: str, check: bool = True, timeout: int = 600) -> str:
    """docker CLI 호출."""
    result = subprocess.run(  # noqa: S603
        [DOCKER_BIN or "docker", *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def docker_available() -> bool:
    if DOCKER_BIN is None:
        return False
    try:
        # timeout 이 없으면 응답 없는 daemon 에 무한 대기해 수집 자체가 멈춘다
        return (
            subprocess.run(  # noqa: S603
                [DOCKER_BIN, "info"], capture_output=True, check=False, timeout=15
            ).returncode
            == 0
        )
    except subprocess.TimeoutExpired:
        return False


requires_docker = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")


def fetch(
    url: str,
    body: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
    token: str | None = AGENT_TOKEN,
) -> dict:
    """Control API 호출 — 인증이 필요한 엔드포인트에 토큰을 싣는다.

    ``token=None`` 으로 무인증 요청을 만들어 401 을 검증할 수 있다.
    """
    data = json.dumps(body).encode() if body is not None else None
    headers = {"content-type": "application/json"}
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers)  # noqa: S310
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        result: dict = json.loads(response.read())
        return result


def wait_healthy(url: str, *, timeout: float = READY_TIMEOUT_S) -> bool:
    """Control API 가 healthy 를 보고할 때까지 기다린다."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if fetch(f"{url}/v1/health", timeout=3.0, token=None)["status"] == "healthy":
                return True
        except (urllib.error.URLError, OSError, KeyError):
            pass
        time.sleep(2)
    return False


def compose_up() -> None:
    """스택을 세운다 — **base 이미지를 먼저 굽는다.**

    compose 는 `agent-base` 를 굽지 않는다: 모든 에이전트 이미지가 그것을
    `FROM` 으로 삼는데, 다시 굽지 않으면 컨테이너가 **옛 프레임워크 코드**를
    실행한다. 그러면 E2E 가 지금 소스가 아니라 마지막으로 구운 이미지를
    검증하게 되고, 배선을 지워도 통과한다.
    """
    docker(
        "build",
        "-t",
        "aaestro/agent-base:0.1.0",
        "-f",
        str(COMPOSE_FILE.parent / "agent-base.Dockerfile"),
        str(REPO_ROOT),
        timeout=900,
    )
    docker("compose", "-f", str(COMPOSE_FILE), "up", "-d", "--build")


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    """전체 스택 — finalizer 가 컨테이너와 볼륨을 반드시 정리한다."""
    compose_up()
    try:
        yield
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


def direct_task(payload: dict[str, Any], task_id: str = "e2e-1") -> dict[str, Any]:
    """직렬화된 direct TaskRequest — trace 는 필수다."""
    return {
        "task_id": task_id,
        "run_id": "direct-e2e",
        "node_id": None,
        "input": payload,
        "trace": {"trace_id": "trace-e2e"},
    }


@requires_docker
def test_stack_becomes_healthy(stack):
    """compose 로 띄운 스택이 healthy 로 전환된다."""
    assert wait_healthy(ECHO_URL)


@requires_docker
def test_fake_provider_is_healthy(stack):
    """모델은 결정적 fake — 실 LLM 을 호출하지 않는다.

    `assert state` 처럼 출력 존재만 확인하면 컨테이너가 죽어 있어도 통과한다.
    실제 health state 를 파싱한다.
    """
    assert wait_healthy(ECHO_URL)

    raw = docker("compose", "-f", str(COMPOSE_FILE), "ps", "--format", "json", "fake-provider")
    # compose 버전에 따라 JSON 객체 하나 또는 줄 단위 스트림으로 나온다
    entries = [json.loads(line) for line in raw.splitlines() if line.strip()]

    assert entries, "fake-provider is not running"
    assert any("healthy" in str(entry.get("Health", "")).lower() for entry in entries)


@requires_docker
def test_direct_request_reaches_a_running_agent(stack):
    """상주 스택에 붙은 에이전트가 그래프 run 과 무관한 직접 호출에 응답한다."""
    assert wait_healthy(ECHO_URL)

    result = fetch(f"{ECHO_URL}/v1/invoke", direct_task({"msg": "hello"}))

    assert result["status"] == "completed"
    assert result["output"] == {"msg": "hello"}


@requires_docker
def test_direct_request_does_not_need_a_graph_run(stack):
    """direct 태스크는 node_id 가 없고 어떤 run 의 state 도 건드리지 않는다."""
    assert wait_healthy(ECHO_URL)

    result = fetch(f"{ECHO_URL}/v1/invoke", direct_task({"a": 1}, task_id="e2e-direct"))

    assert result["task_id"] == "e2e-direct"


@requires_docker
def test_streaming_emits_events(stack):
    """스트리밍 경로가 SSE 로 이벤트를 흘린다."""
    assert wait_healthy(ECHO_URL)

    # SSE 라 fetch() 헬퍼(JSON 파싱)를 못 쓴다 — 토큰은 그것과 같은 방식으로 싣는다.
    # 02 API Rules 3 상 /health 외 전 엔드포인트가 인증을 요구한다
    request = urllib.request.Request(  # noqa: S310
        f"{ECHO_URL}/v1/stream",
        data=json.dumps(direct_task({"msg": "stream"})).encode(),
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {AGENT_TOKEN}",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        body = response.read().decode()

    assert body.count("data: ") >= 2
    assert '"type":"done"' in body.replace(" ", "")


@requires_docker
def test_untraceable_task_is_rejected(stack):
    """모든 태스크는 단일 trace 로 묶여야 한다."""
    assert wait_healthy(ECHO_URL)
    payload = direct_task({"msg": "x"})
    del payload["trace"]

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fetch(f"{ECHO_URL}/v1/invoke", payload)

    assert exc_info.value.code == 400  # 본문 검증 실패 — VAL_001, 값은 싣지 않는다 (#448)


@requires_docker
def test_agent_runs_as_non_root_in_the_stack(stack):
    """격리 계약은 compose 스택에서도 유지된다."""
    assert wait_healthy(ECHO_URL)

    uid = docker("compose", "-f", str(COMPOSE_FILE), "exec", "-T", "agent-echo", "id", "-u")

    assert uid == "1000"


@requires_docker
def test_card_is_served(stack):
    """A2A AgentCard 가 Control API 로 제공된다."""
    assert wait_healthy(ECHO_URL)

    card = fetch(f"{ECHO_URL}/v1/card")

    assert card["name"] == "echo"


@requires_docker
def test_control_api_requires_a_token(stack):
    """무인증 스택은 인증 회귀를 통과시킨다 — E2E 도 인증을 켠 채 검증한다."""
    assert wait_healthy(ECHO_URL)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fetch(f"{ECHO_URL}/v1/invoke", direct_task({"msg": "no-token"}), token=None)

    assert exc_info.value.code == 401


@requires_docker
def test_wrong_token_is_rejected(stack):
    assert wait_healthy(ECHO_URL)

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        fetch(f"{ECHO_URL}/v1/invoke", direct_task({"msg": "x"}), token="not-the-token")

    assert exc_info.value.code == 401


@requires_docker
def test_health_stays_unauthenticated(stack):
    """Docker healthcheck 가 토큰 없이 호출하므로 열려 있어야 한다."""
    assert fetch(f"{ECHO_URL}/v1/health", token=None)["status"] == "healthy"
