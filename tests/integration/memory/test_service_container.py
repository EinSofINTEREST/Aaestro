"""The Memory Service as a container.

09 Access Enforcement 1 은 에이전트가 **HTTP 로** Memory Service 에 붙는다고
규정한다 — 저장소 자격증명을 에이전트 컨테이너에 넣지 않기 위해서다. 그
배치를 세우려면 서비스가 프로세스여야 하는데, 띄우는 진입점이 없었다 (#181).

이미지가 실제로 뜨는지까지 봐야 한다: 진입점을 만들어도 이미지가 그것을
실행하지 못하면 배치는 여전히 세울 수 없다.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tests.e2e.test_stack import docker, requires_docker

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
BASE_IMAGE = "aaestro/agent-base:0.1.0"
IMAGE = "aaestro/memory-service:0.1.0"
CONTAINER = "aaestro-memory-itest"
PORT = 18093


def status_of(path: str, *, token: str | None = None) -> int:
    """서비스 응답 코드 — 거부도 결과이므로 예외로 감추지 않는다."""
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}")  # noqa: S310
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as err:
        return int(err.code)


@pytest.fixture(scope="module")
def service():
    """이미지를 빌드해 띄운다 — finalizer 가 반드시 정리한다.

    base 를 **먼저** 빌드한다: memory-service 는 `FROM aaestro/agent-base` 인데
    그 이미지는 공개 레지스트리에 없다 — 로컬에 없으면 Docker 가 pull 을
    시도해 `pull access denied` 로 죽는다. 개발 기계에는 이미 있어서 드러나지
    않고, CI 에서만 실패한다.
    """
    docker(
        "build",
        "-f",
        str(REPO_ROOT / "deployments/docker/agent-base.Dockerfile"),
        "-t",
        BASE_IMAGE,
        str(REPO_ROOT),
        timeout=900,
    )
    docker(
        "build",
        "-f",
        str(REPO_ROOT / "deployments/docker/memory-service.Dockerfile"),
        "-t",
        IMAGE,
        str(REPO_ROOT),
        timeout=900,
    )
    docker("rm", "-f", CONTAINER, check=False)
    docker(
        "run",
        "-d",
        "--name",
        CONTAINER,
        "-p",
        f"{PORT}:8090",
        "-v",
        f"{REPO_ROOT}:/repo:ro",
        IMAGE,
    )
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if docker("inspect", "-f", "{{.State.Health.Status}}", CONTAINER) == "healthy":
                break
            time.sleep(2)
        else:
            pytest.fail(f"memory service never became healthy: {docker('logs', CONTAINER)}")
        yield CONTAINER
    finally:
        docker("rm", "-f", CONTAINER, check=False)


@requires_docker
def test_the_service_starts_as_a_container(service):
    """#181 — 진입점이 없어 이 배치를 세울 수 없었다."""
    assert docker("inspect", "-f", "{{.State.Health.Status}}", service) == "healthy"


@requires_docker
def test_agents_reach_it_over_http(service):
    """09 는 HTTP 를 경로로 규정한다 — 붙을 상대가 실제로 있어야 한다."""
    assert status_of("/openapi.json") == 200


@requires_docker
def test_an_unauthenticated_request_is_refused(service):
    """토큰 없이 통하면 space 경계가 무의미해진다."""
    assert status_of("/v1/spaces") == 401


@requires_docker
def test_the_container_runs_as_non_root(service):
    """격리 계약은 프레임워크 컴포넌트에도 적용된다 (02)."""
    assert docker("exec", service, "id", "-u") == "1000"


@requires_docker
def test_no_store_credentials_reach_an_agent_image(service):
    """09 Access Enforcement 1 — 자격증명은 이 프로세스만 갖는다.

    에이전트 이미지가 DSN 을 들고 있으면 서비스를 우회할 수 있어, 이 배치의
    의미가 사라진다.
    """
    compose = (REPO_ROOT / "deployments/docker/compose.e2e.yaml").read_text("utf-8")

    assert "AAESTRO_MEMORY__DSN" not in compose


@requires_docker
def test_the_service_serves_the_declared_surface(service):
    """09 Retrieval API 의 창구가 전부 열려 있어야 에이전트가 쓸 수 있다."""
    with urllib.request.urlopen(  # noqa: S310
        f"http://127.0.0.1:{PORT}/openapi.json", timeout=10
    ) as response:
        paths = set(json.load(response)["paths"])

    assert {"/v1/append", "/v1/search", "/v1/read", "/v1/latest", "/v1/spaces"} <= paths
