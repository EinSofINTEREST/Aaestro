"""Memory Service 프로세스의 import 경계.

Memory Service 는 control plane 의존 (langgraph · runtime) 없이 뜬다 — 이미지에
그것들이 없어도, 느리게 뜨지도 않게 한다 (#431). ``catalog`` 를 lazy 로 import 하는
bootstrap 경로를 실제로 탄 뒤에 검사한다.
"""

from __future__ import annotations

from pathlib import Path

from tests.fixtures.import_contract import REPO_ROOT, assert_no_forbidden_imports

# 새 import 가 생기면 이 상수를 고치는 것이 곧 결정이다
FORBIDDEN = (
    "aaestro.orchestrator",
    "aaestro.runtime",
    "aaestro.agentd",
    "aaestro.cli",
    "aaestro.ui",
    "langgraph",
    "langchain",
    "langchain_core",
    "docker",
    "a2a",
    "mcp",
    "anthropic",
)

BOOTSTRAP = """
import sys
from pathlib import Path

from aaestro.config import load_config
from aaestro.memory.bootstrap import build_deployment

root = Path(sys.argv[1])
config = load_config("dev", config_dir=root / "configs", environ={{}})
{body}
"""


def _run(body: str, root: Path = REPO_ROOT) -> None:
    assert_no_forbidden_imports(BOOTSTRAP.format(body=body), FORBIDDEN, repo_root=root)


def test_main_import_pulls_no_control_plane_modules() -> None:
    assert_no_forbidden_imports("import aaestro.memory.__main__", FORBIDDEN)


def test_token_bootstrap_path_pulls_no_control_plane_modules(token_root: Path) -> None:
    _run("build_deployment(config, root=root)", token_root)


def test_registry_bootstrap_path_pulls_no_control_plane_modules() -> None:
    _run("build_deployment(config, root=root, access=object())")
