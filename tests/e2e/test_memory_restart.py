"""Stored memories are searchable again after the Memory Service restarts (#312).

인덱스는 프로세스 메모리에만 있다 — 재시작한 서비스가 저장소에서 되읽지 않으면 재시작 전 기억이
검색되지 않는다. 실제 컨테이너를 재시작해 저장소(파일)만 넘어간 상태에서 같은 질의로 찾는다.

compose 의 Memory Service 는 in-memory 저장소라 재시작을 넘지 못한다 — 파일 저장소로 하나 더 세운다.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Iterator

import pytest

from tests.e2e.conftest import NETWORK, REPO_ROOT, until
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

MEMORY = "aaestro-e2e-memory-restart"
VOLUME = "aaestro-e2e-memory-restart-data"
PORT = 18091
FACT = "sidecar 이미지는 태그를 고정해야 재기동 뒤에도 도구 목록이 같다"


@pytest.fixture
def memory(stack) -> Iterator[None]:
    docker("rm", "-f", MEMORY, check=False)
    docker("volume", "rm", "-f", VOLUME, check=False)
    docker(
        "run", "-d", "--name", MEMORY, "--network", NETWORK,
        "--read-only", "--tmpfs", "/tmp:size=16m",  # noqa: S108 — 컨테이너 안 tmpfs
        "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "-v", f"{REPO_ROOT}:/repo:ro",
        # 토큰 모드는 run scope 를 선언한 그래프가 있으면 기동을 거부한다 (#469)
        "--tmpfs", "/repo/graphs:size=1m",
        # /tokens 는 이미지가 uid 1000 소유로 만든다 — named volume 이 그 소유권을 물려받아
        # 재시작을 넘는 쓰기 자리가 된다. 저장소 파일도 여기 둔다
        "-v", f"{VOLUME}:/tokens",
        "-e", "AAESTRO_EMBEDDING_BASE_URL=http://fake-provider:8000",
        "-e", "AAESTRO_MEMORY__PATH=/tokens/memory.db",
        "-e", "AAESTRO_MEMORY_TOKENS_PATH=/tokens/memory.json",
        "-p", f"127.0.0.1:{PORT}:8090",
        "aaestro/memory-service:0.1.0",
    )  # fmt: skip
    try:
        until(ready, what="file-backed memory service health")
        yield
    finally:
        docker("rm", "-f", MEMORY, check=False)
        docker("volume", "rm", "-f", VOLUME, check=False)


def ready() -> bool:
    return docker("inspect", "-f", "{{.State.Health.Status}}", MEMORY, check=False) == "healthy"


def token() -> str:
    """토큰은 기동마다 새로 발급된다 — 재시작 뒤에는 다시 읽는다."""
    tokens = json.loads(docker("exec", MEMORY, "cat", "/tokens/memory.json"))
    return str(tokens["researcher"])


def post(path: str, body: dict) -> list | dict:
    request = urllib.request.Request(  # noqa: S310 — 로컬 테스트 서비스
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {token()}", "content-type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
        return json.loads(response.read())


def found() -> list[str]:
    hits = post("/v1/search", {"query": "sidecar 이미지 태그 고정", "spaces": ["longterm"]})
    assert isinstance(hits, list)
    return [hit["entry"]["content"] for hit in hits]


def test_a_memory_survives_a_service_restart(memory):
    entry = {"kind": "fact", "content": FACT}
    post("/v1/append", {"space": "longterm", "entry": entry})
    until(lambda: FACT in found(), what="the fact indexed before the restart")

    docker("restart", MEMORY)
    until(ready, what="memory service health after restart")

    until(lambda: FACT in found(), what="the fact found again after the restart")
    logs = docker("logs", MEMORY)
    assert "memory index warm-up queued" in logs
    assert "memory index warm-up completed" in logs
