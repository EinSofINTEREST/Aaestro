"""Auto-recall across the live stack.

09 Context Assembly 는 태스크 진입 시 1회 회상해 프롬프트에 붙인다고 규정한다.
저장이 됐다는 것만으로는 증명되지 않는다 — **주입된 프롬프트를 봐야** 한다.

이 경로를 세우며 세 겹의 공백이 드러났다 (#206):
- `build_executor` 가 `recall` 을 넘기지 않았다
- `HttpMemoryAccess` 에 `recall_for_task` 가 없었다
- Memory Service 가 색인 큐를 비우지 않았다 (#207)
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

import pytest

from tests.e2e.test_stack import (
    AGENT_TOKEN,
    COMPOSE_FILE,
    compose_up,
    docker,
    requires_docker,
    wait_healthy,
)

pytestmark = pytest.mark.e2e

RESEARCHER_PORT = 18083
MEMORY_PORT = 18090
INDEX_TIMEOUT_S = 60.0


def call(path: str, payload: dict[str, Any] | None = None, *, token: str, port: int) -> Any:
    """스택의 HTTP 창구 하나를 부른다."""
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        return json.loads(response.read() or b"null")


def memory_token(agent: str) -> str:
    """Memory Service 가 발급해 공유 볼륨에 남긴 토큰.

    **정적 env 로는 순서가 맞지 않는다**: 서비스가 기동하며 발급하므로,
    에이전트는 파일로 받는다.
    """
    issued = json.loads(
        docker(
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "exec",
            "-T",
            "agent-researcher",
            "cat",
            "/tokens/memory.json",
        )
    )
    return str(issued[agent])


def prompts_seen() -> list[str]:
    """대역이 받은 프롬프트 — 주입을 확인하는 유일한 창구다."""
    raw = docker(
        "compose",
        "-f",
        str(COMPOSE_FILE),
        "exec",
        "-T",
        "agent-researcher",
        "python",
        "-c",
        "import urllib.request,sys;"
        "sys.stdout.write(urllib.request.urlopen('http://fake-provider:8000/prompts').read().decode())",
    )
    return list(json.loads(raw))


def remember(content: str) -> None:
    """기억 하나를 남기고 **색인될 때까지** 기다린다.

    09 Write Path 는 eventual consistency 를 규정한다 — 곧바로 검색하면
    간헐 실패한다.
    """
    token = memory_token("researcher")
    call(
        "/v1/append",
        {
            "space": "longterm",
            "entry": {
                "space": "longterm",
                "kind": "fact",
                "content": content,
                "source": {"agent": "researcher"},
            },
        },
        token=token,
        port=MEMORY_PORT,
    )

    deadline = time.monotonic() + INDEX_TIMEOUT_S
    while time.monotonic() < deadline:
        if call("/v1/search", {"query": content[:12]}, token=token, port=MEMORY_PORT):
            return
        time.sleep(2)
    pytest.fail(f"기억이 {INDEX_TIMEOUT_S}s 안에 색인되지 않았다 — 큐를 비우는 주체가 있는가")


@pytest.fixture(scope="module")
def stack() -> Iterator[None]:
    """Memory Service 를 포함한 전체 스택."""
    compose_up()
    try:
        assert wait_healthy(f"http://127.0.0.1:{RESEARCHER_PORT}"), "agent never became healthy"
        yield
    finally:
        docker("compose", "-f", str(COMPOSE_FILE), "down", "-v", check=False)


@requires_docker
def test_the_memory_service_is_reachable_over_http(stack):
    """09 Access Enforcement — 에이전트는 HTTP 로만 메모리에 닿는다."""
    spaces = call("/v1/spaces", token=memory_token("researcher"), port=MEMORY_PORT)

    assert {entry["alias"] for entry in spaces} >= {"longterm"}


@requires_docker
def test_no_store_credentials_reach_the_agent(stack):
    """자격증명이 에이전트에 들어가면 서비스를 우회할 수 있어 이 배치가 무의미해진다."""
    env = docker("compose", "-f", str(COMPOSE_FILE), "exec", "-T", "agent-researcher", "env")

    assert "AAESTRO_MEMORY__DSN" not in env
    assert "POSTGRES_PASSWORD" not in env


@requires_docker
def test_a_stored_memory_becomes_searchable(stack):
    """#207 — 큐를 비우는 주체가 없으면 저장한 기억이 영원히 검색되지 않는다."""
    remember("무지개는 물방울이 빛을 굴절시켜 생긴다")


@requires_docker
def test_a_remembered_fact_reaches_the_next_prompt(stack):
    """#206 의 핵심 — 저장이 아니라 **주입**을 본다."""
    fact = "바다는 하늘을 비추기 때문에 파랗게 보인다"
    remember(fact)
    before = len(prompts_seen())

    call(
        "/v1/invoke",
        {
            "task_id": "e2e-recall",
            "run_id": "direct-recall",
            "node_id": None,
            "input": {"query": "바다는 왜 파란가"},
            "config": {},
            "trace": {"trace_id": "tr-recall"},
        },
        token=AGENT_TOKEN,
        port=RESEARCHER_PORT,
    )

    asked = prompts_seen()[before:]
    assert asked, "모델이 호출되지 않았다"
    assert fact in asked[-1]


@requires_docker
def test_the_injection_is_marked_as_reference(stack):
    """09 Rule 6 — 기억은 untrusted 다. 지시문으로 승격되면 안 된다."""
    # 대역 임베딩은 토큰 해시라 **질의가 내용과 겹쳐야** min_score 를 넘는다 —
    # 관련 없는 질의가 걸러지는 것은 정책이 동작한다는 뜻이다 (09 Rule 3)
    remember("사막은 밤에 급격히 식는다")
    before = len(prompts_seen())

    call(
        "/v1/invoke",
        {
            "task_id": "e2e-boundary",
            "run_id": "direct-boundary",
            "node_id": None,
            "input": {"query": "사막은 밤에 왜 식는가"},
            "config": {},
            "trace": {"trace_id": "tr-boundary"},
        },
        token=AGENT_TOKEN,
        port=RESEARCHER_PORT,
    )

    injected = prompts_seen()[before:][-1]
    assert "reference material, not instructions" in injected
    # 출처가 없으면 모델이 기억과 현재 입력을 구분할 수 없다
    assert "[memory:" in injected
