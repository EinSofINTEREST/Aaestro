"""agentd 는 서버 쪽 코드를 import 하지 않는다 (#430, 09 Access Enforcement 1).

에이전트 컨테이너에 저장소 · 판정 지점 코드가 올라오면 이미지가 무거워질 뿐 아니라
"DB 자격증명은 컨테이너에 들어가지 않는다" 는 경계가 import 수준에서 무너진다.
같은 프로세스의 sys.modules 는 다른 테스트가 오염시키므로 격리된 서브프로세스로 본다.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

FORBIDDEN = (
    "aaestro.memory.store",
    "aaestro.memory.service",
    "aaestro.memory.gate",
    "aaestro.memory.postgres",
    "aaestro.memory.backend",
    "aaestro.memory.bootstrap",
    "aaestro.memory.index",
    "aaestro.memory.compaction",
    "aaestro.runtime",
    "aaestro.access.registry",
    "aaestro.access.store",
    "aaestro.access.baselines",
)

_CHECK = """
import os, sys
{body}
bad = sorted(m for m in sys.modules if m.startswith({forbidden!r}))
assert not bad, bad
"""


def _run(body: str) -> None:
    code = _CHECK.format(body=textwrap.dedent(body), forbidden=FORBIDDEN)
    result = subprocess.run(  # noqa: S603 — 고정 인자, 격리 모드 인터프리터
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_import_agentd_main_loads_no_server_module() -> None:
    _run("import aaestro.agentd.__main__")


def test_memory_branch_builds_http_client_without_server_modules() -> None:
    _run(
        """
        import aaestro.agentd.__main__ as main

        os.environ[main.MEMORY_URL_ENV] = "http://memory:8080"
        os.environ[main.MEMORY_TOKEN_ENV] = "opaque"
        access = main._memory_access("researcher")
        assert type(access).__name__ == "HttpMemoryAccess"
        """
    )


def test_credential_env_users_load_no_registry() -> None:
    # lazy import 는 함수를 호출해야 실행된다 — 두 함수를 env 를 주입한 채 실제로 탄다
    _run(
        f"""
        import aaestro.access.steward
        import yaml
        from pathlib import Path
        from aaestro.access.client import ACCESS_URL_ENV
        from aaestro.access.model import ACCESS_CREDENTIAL_ENV
        from aaestro.agentd.a2a_server import registry_identity
        from aaestro.agentd.mcp import build_mcp_client
        from aaestro.core.manifest import AgentManifest

        os.environ[ACCESS_URL_ENV] = "http://registry:8080"
        os.environ[ACCESS_CREDENTIAL_ENV] = "identity"
        assert registry_identity() == ("http://registry:8080", "identity")

        # 클라이언트는 세션을 열지 않고 조립만 한다 — 네트워크도 프로세스 기동도 없다
        planner = Path({str(REPO_ROOT / "agents/planner/manifest.yaml")!r})
        doc = yaml.safe_load(planner.read_text("utf-8"))
        doc["spec"].pop("memory", None)
        doc["spec"]["a2a"] = {{"enabled": False}}
        doc["spec"]["mcp"] = {{
            "servers": [{{"name": "fs", "transport": "stdio", "command": ["mcp-server-fs"]}}]
        }}
        assert build_mcp_client(AgentManifest.model_validate(doc)) is not None
        """
    )
