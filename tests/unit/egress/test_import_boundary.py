"""Egress proxy 프로세스의 import 경계.

프록시는 비밀값을 모두 쥐므로 최소 권한·최소 표면으로 뜬다 — control plane 의존
(langgraph · runtime) 이 따라오면 안 된다 (#431). MCP 종단이 ``catalog`` 를 lazy 로
import 하는 경로를 실제로 탄 뒤에 검사한다.
"""

from __future__ import annotations

from tests.fixtures.import_contract import (
    assert_dependencies_classified,
    assert_no_forbidden_imports,
    modules_of,
)

# 새 import 가 생기면 이 상수를 고치는 것이 곧 결정이다
FORBIDDEN_INTERNAL = (
    "aaestro.orchestrator",
    "aaestro.runtime",
    "aaestro.agentd",
    "aaestro.cli",
    "aaestro.ui",
    # 의존성 이름이 아닌 모듈 — pyproject 에 없어도 끌려오면 안 된다
    "langchain",
)

# 서드파티는 pyproject 의존성 이름으로 선언하고 모듈 이름은 유도한다 (#504)
FORBIDDEN_DEPENDENCIES = (
    "langgraph",
    "langgraph-checkpoint-postgres",
    "langgraph-checkpoint-redis",
    "langchain-core",
    "docker",
    "a2a-sdk",
    "mcp",
    "httpx2",
    "anthropic",
    "psycopg",
)
ALLOWED_DEPENDENCIES = {
    "pydantic": "core 계약",
    "structlog": "로깅",
    "prometheus-client": "메트릭",
    "tenacity": "재시도",
    "jinja2": "promptset 렌더 — 프록시 경로는 쓰지 않지만 core 와 함께 설치된다",
    "pyyaml": "설정 로드",
    "httpx": "업스트림 전달",
    "fastapi": "프록시 서버",
    "uvicorn": "프록시 서버",
}
FORBIDDEN = (*FORBIDDEN_INTERNAL, *modules_of(FORBIDDEN_DEPENDENCIES))

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


def test_every_runtime_dependency_is_classified() -> None:
    assert_dependencies_classified(FORBIDDEN_DEPENDENCIES, ALLOWED_DEPENDENCIES)


def _run(body: str) -> None:
    assert_no_forbidden_imports(body, FORBIDDEN)


def test_main_import_pulls_no_control_plane_modules() -> None:
    _run("import aaestro.egress.__main__")


def test_mcp_termination_path_pulls_no_control_plane_modules() -> None:
    _run(MCP_PATH.format())


def test_providers_lazy_target_pulls_no_control_plane_modules() -> None:
    # run() 이 lazy 로 import 하는 서빙 경로의 모듈
    _run("import aaestro.egress.providers")
