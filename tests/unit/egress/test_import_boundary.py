"""Egress proxy 프로세스의 import 경계.

프록시는 비밀값을 모두 쥐므로 최소 권한·최소 표면으로 뜬다 — control plane 의존
(langgraph · runtime) 이 따라오면 안 된다 (#431). MCP 종단이 ``catalog`` 를 lazy 로
import 하는 경로를 실제로 탄 뒤에 검사한다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]

FORBIDDEN = (
    "langgraph",
    "langchain",
    "aaestro.runtime",
    "aaestro.orchestrator.builder",
    "aaestro.orchestrator.checkpoint",
    "aaestro.orchestrator.run",
)

SCRIPT = """
import sys

{body}
bad = sorted(m for m in sys.modules if m.startswith({forbidden!r}))
assert not bad, bad
"""

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
    script = SCRIPT.format(body=body, forbidden=FORBIDDEN)
    # -I: cwd 와 PYTHONPATH 를 무시해 현재 프로세스가 아닌 깨끗한 import 그래프를 본다
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script, str(REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_main_import_pulls_no_control_plane_modules() -> None:
    _run("import aaestro.egress.__main__")


def test_mcp_termination_path_pulls_no_control_plane_modules() -> None:
    _run(MCP_PATH.format())


def test_providers_lazy_target_pulls_no_control_plane_modules() -> None:
    # run() 이 lazy 로 import 하는 서빙 경로의 모듈
    _run("import aaestro.egress.providers")
