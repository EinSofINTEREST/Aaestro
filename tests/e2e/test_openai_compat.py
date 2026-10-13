"""An ``openai-compat`` agent reaching its provider through the egress proxy (#361).

떠 있는 에이전트 컨테이너가 프록시 컨테이너를 거쳐 Chat Completions 대역과 도구 호출을 왕복한다.

- 에이전트 env 에는 실 키가 없다 — Bearer 자리에는 신원이 있고, 대역은 프록시가 붙인 키를 받는다
- 대화는 ``tool_calls`` → ``role: tool`` → 최종 텍스트로 끝난다
- 같은 신원으로도 선언 밖 경로(``v1/files``)는 프록시가 ``ACC_001`` 로 막는다

실 LLM 은 부르지 않는다 (06) — upstream 은 스택의 fake-provider 다.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
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
from tests.e2e.test_a2a_access import FORWARDER, run_in
from tests.e2e.test_egress import (
    AGENTS,
    ENFORCER_TOKEN,
    PROXY,
    PROXY_KEY,
    RESEARCHER,
    agents_network,
    build_proxy_image,
    container_env,
    proxy_ready,
    start_forwarder,
    start_proxy,
)
from tests.e2e.test_memory_access import MEMORY, MEMORY_ALIAS, memory_ready, start_memory
from tests.e2e.test_stack import docker, requires_docker

pytestmark = [pytest.mark.e2e, requires_docker]

COMPAT_KEY = "e2e-compat-proxy-held-key"
COMPAT_MODEL = "fake-compat-model"
UPSTREAM = "http://fake-provider:8000"

# 컨테이너 안에서 자기 Control API 로 direct 태스크를 보낸다 — 상태(또는 에러 코드)만 출력
INVOKE = """
import json, os, sys, urllib.request
body = {"task_id": sys.argv[1], "run_id": "direct-" + sys.argv[1], "node_id": None,
        "input": {"query": sys.argv[2]}, "config": {}, "trace": {"trace_id": sys.argv[1]}}
request = urllib.request.Request(
    "http://127.0.0.1:8080/v1/invoke", data=json.dumps(body).encode(),
    headers={"Authorization": "Bearer " + os.environ["AAESTRO_AGENT_TOKEN"],
             "content-type": "application/json"})
result = json.loads(urllib.request.urlopen(request, timeout=90).read())
print((result.get("error") or {}).get("code") or result["status"])
"""

# 컨테이너 안에서 프록시의 openai-compat 창구에 선언 밖 경로를 직접 두드린다 — 상태와 코드만 출력
FORBIDDEN_PATH = """
import json, os, urllib.error, urllib.request
request = urllib.request.Request(
    os.environ["OPENAI_COMPAT_BASE_URL"] + "/v1/files", data=b"{}",
    headers={"Authorization": "Bearer " + os.environ["OPENAI_COMPAT_API_KEY"],
             "content-type": "application/json"})
try:
    print(urllib.request.urlopen(request, timeout=15).status)
except urllib.error.HTTPError as err:
    print(err.code, json.loads(err.read())["error"]["code"])
"""

CHATS = (
    "import urllib.request,sys; "
    "sys.stdout.write(urllib.request.urlopen('http://fake-provider:8000/chats').read().decode())"
)


def provider_chats() -> list[dict[str, Any]]:
    """대역이 받은 Chat Completions 요청 — 스택 네트워크의 컨테이너에서 읽는다."""
    return json.loads(docker("exec", MEMORY, "python", "-c", CHATS))


def workspace(tmp_path: Path) -> Path:
    """저장소 선언을 복사하고 researcher 의 모델만 ``openai-compat`` 으로 바꾼다."""
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    manifest_path = root / "agents" / "researcher" / "manifest.yaml"
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["model"] = {"provider": "openai-compat", "name": COMPAT_MODEL}
    manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    return root


@pytest.fixture
def plane(stack, tmp_path) -> Iterator[dict[str, Any]]:
    build_proxy_image()
    config_dir = write_config(
        tmp_path,
        orchestrator={
            "control_host": "0.0.0.0",  # noqa: S104 — 프록시·Memory Service 컨테이너가 닿아야 한다
            "access_store": str(tmp_path / "access.db"),
            "access_enforcer_token": ENFORCER_TOKEN,
            "access_agent_url": "http://control-plane:8700",
        },
    )
    config = yaml.safe_load((config_dir / "e2e.yaml").read_text(encoding="utf-8"))
    config["runtime"]["network"] = AGENTS
    config["runtime"]["egress_proxy"] = {
        "connect_url": "http://aaestro-egress:8080",
        "providers_url": "http://aaestro-egress:8081",
        # 배포 검증이 이 목록과 선언을 대조한다 — 빠지면 VAL_002 로 배포가 거절된다
        "providers": ["anthropic", "openai-compat"],
    }
    (config_dir / "e2e.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    tokens_path = tmp_path / "memory.json"
    tokens_path.write_text(json.dumps(memory_tokens()), encoding="utf-8")
    root = workspace(tmp_path)
    env = {"AAESTRO_MEMORY_URL": f"http://{MEMORY_ALIAS}:8090", "AAESTRO_REPO_ROOT": str(root)}
    agents_network()
    process = start_plane(config_dir, tokens_path, env=env)
    start_memory(also=(AGENTS,))
    start_forwarder()
    start_proxy(
        root,
        extra_env={
            "AAESTRO_EGRESS_OPENAI_COMPAT_UPSTREAM": UPSTREAM,
            "OPENAI_COMPAT_API_KEY": COMPAT_KEY,
        },
    )
    try:
        until(plane_healthy, what="control plane health")
        until(memory_ready, what="registry-mode memory service health")
        until(proxy_ready, what="egress proxy health")
        yield {"process": process, "root": root}
    finally:
        stop(process)
        for name in (PROXY, MEMORY, FORWARDER, *deployed_containers()):
            docker("rm", "-f", name, check=False)
        docker("network", "rm", AGENTS, check=False)


def test_openai_compat_agent_round_trips_a_tool_call_through_the_proxy(plane):
    status, record = api("POST", "/v1/deployments", {"graph": "research-pipeline"})
    assert status == 201, record

    # --- 에이전트 컨테이너: 주소는 프록시, 키 자리는 신원 — 실 키는 없다
    env = container_env(RESEARCHER)
    identity = env["AAESTRO_ACCESS_CREDENTIAL"]
    assert env["OPENAI_COMPAT_BASE_URL"] == "http://aaestro-egress:8081/openai-compat"
    assert env["OPENAI_COMPAT_API_KEY"] == identity
    assert COMPAT_KEY not in env.values() and PROXY_KEY not in env.values()

    # --- 도구 호출 왕복: 대역이 memory_search 를 부르게 하고, 그 결과를 받아 끝낸다
    before = len(provider_chats())
    outcome = run_in(
        RESEARCHER, INVOKE, "e2e-openai-compat", 'use tool memory_search with {"query": "e2e"}'
    )
    assert outcome == "completed", (outcome, getattr(run_in, "last_stderr", ""))

    first, second = provider_chats()[before:]
    assert first["model"] == second["model"] == COMPAT_MODEL
    assert first["roles"][-1] == "user"
    assert second["roles"][-2:] == ["assistant", "tool"], (
        "도구 결과가 role: tool 로 돌아오지 않았다"
    )
    # --- 프록시가 신원을 떼고 진짜 키를 Bearer 로 붙였다
    assert {first["authorization"], second["authorization"]} == {f"Bearer {COMPAT_KEY}"}
    assert identity not in json.dumps(provider_chats()), "에이전트 신원이 provider 로 새어 나갔다"

    # --- 같은 신원이라도 선언 밖 경로는 프록시가 막는다
    assert run_in(RESEARCHER, FORBIDDEN_PATH) == "403 ACC_001"
