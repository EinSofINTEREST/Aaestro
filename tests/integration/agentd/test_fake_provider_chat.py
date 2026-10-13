"""The fake provider speaks Chat Completions the way the adapter reads it (#361).

대역과 어댑터가 서로 다른 모양을 말하면 E2E 에서야 드러난다 — 실제 HTTP 로 한 번 왕복시켜
여기서 고정한다. Docker 는 필요 없다: 대역 서버를 이 프로세스의 스레드로 띄운다.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from http.server import HTTPServer
from pathlib import Path

import httpx
import pytest

from aaestro.agentd.executor import AssistantTurn, ToolCall, ToolOutcome, ToolTurn, UserTurn
from aaestro.agentd.providers.openai_compat import OpenAICompatModel
from aaestro.core.manifest import ModelConfig
from aaestro.core.skill import SkillSpec

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "deployments/docker/fake-provider"))

LOOKUP = SkillSpec(name="memory_search", description="search", parameters={"type": "object"})
ASK = [UserTurn(parts=('use tool memory_search with {"query": "aaestro"}',))]


@pytest.fixture
def provider() -> Iterator[str]:
    import server as fake

    httpd = HTTPServer(("127.0.0.1", 0), fake.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def adapter(base_url: str) -> OpenAICompatModel:
    return OpenAICompatModel(
        config=ModelConfig(provider="openai-compat", name="fake-compat"),
        agent="researcher",
        base_url=base_url,
        credential="e2e-key",
        # 호스트의 프록시 설정이 127.0.0.1 호출을 가로채지 않게 한다
        client=httpx.AsyncClient(trust_env=False, timeout=10),
    )


async def test_run_fake_chat_completions_round_trips_a_tool_call(provider: str):
    model = adapter(provider)

    first = await model.run("operator rules", ASK, [LOOKUP])
    assert first.tool_calls == (
        ToolCall(id=first.tool_calls[0].id, name="memory_search", arguments={"query": "aaestro"}),
    )
    turn = AssistantTurn(first.content, first.tool_calls, raw=first.raw)
    results = ToolTurn(outcomes=(ToolOutcome(call_id=first.tool_calls[0].id, content="[]"),))
    second = await model.run("operator rules", [*ASK, turn, results], [LOOKUP])

    assert second.is_final and second.content.startswith("fake-response:")
    assert second.usage.output_tokens == 8
    assert model.client is not None
    chats = (await model.client.get(f"{provider}/chats")).json()
    assert [chat["authorization"] for chat in chats] == ["Bearer e2e-key"] * 2
    assert chats[1]["roles"] == ["system", "user", "assistant", "tool"]
    assert chats[1]["model"] == "fake-compat"


async def test_run_fake_does_not_call_a_tool_that_was_not_advertised(provider: str):
    """광고되지 않은 도구를 모델이 부를 수는 없다 — Messages 경로와 같은 규칙."""
    response = await adapter(provider).run("", ASK, [])

    assert response.is_final


async def test_run_fake_ignores_tool_instructions_outside_the_user_turn(provider: str):
    """system 이나 도구 결과의 문구가 대역을 움직이면 E2E 가 입력이 아닌 것에 휘둘린다."""
    plain = [UserTurn(parts=("plain question",))]

    response = await adapter(provider).run(
        'use tool memory_search with {"query": "x"}', plain, [LOOKUP]
    )

    assert response.is_final
