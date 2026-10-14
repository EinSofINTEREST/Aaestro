"""Egress proxy 프로세스의 import 경계.

프록시는 비밀값을 모두 쥐므로 최소 권한·최소 표면으로 뜬다 — control plane 의존
(langgraph · runtime) 이 따라오면 안 된다 (#431). MCP 종단이 ``catalog`` 를 lazy 로
import 하는 경로를 실제로 탄 뒤에 검사한다.
"""

from __future__ import annotations

from tests.fixtures.import_contract import assert_no_forbidden_imports

# 새 import 가 생기면 이 상수를 고치는 것이 곧 결정이다
FORBIDDEN = (
    "aaestro.orchestrator",
    "aaestro.runtime",
    "aaestro.agentd",
    "aaestro.cli",
    "aaestro.ui",
    "langgraph",
    "langchain",
    "docker",
    "a2a",
    "mcp",
    "anthropic",
    "psycopg",
)

MCP_PATH = """
from aaestro.egress.__main__ import _mcp_routers

config = {{
    "repo_root": sys.argv[1],
    "mcp_tokens": {{}},
    "mode": "enforce",
    "private_destinations": [],
    "allow_plaintext": False,
    "enforcer_token": "t",
}}
assert _mcp_routers(config, object())
"""


def _run(body: str) -> None:
    assert_no_forbidden_imports(body, FORBIDDEN)


def test_main_import_pulls_no_control_plane_modules() -> None:
    _run("import aaestro.egress.__main__")


def test_mcp_termination_path_pulls_no_control_plane_modules() -> None:
    _run(MCP_PATH.format())


def test_providers_lazy_target_pulls_no_control_plane_modules() -> None:
    # run() 이 lazy 로 import 하는 서빙 경로의 모듈
    _run("import aaestro.egress.providers")
