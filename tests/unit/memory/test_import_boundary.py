"""Memory Service 프로세스의 import 경계.

Memory Service 는 control plane 의존 (langgraph · runtime) 없이 뜬다 — 이미지에
그것들이 없어도, 느리게 뜨지도 않게 한다 (#431). ``catalog`` 를 lazy 로 import 하는
bootstrap 경로를 실제로 탄 뒤에 검사한다.
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
from pathlib import Path

from aaestro.config import load_config
from aaestro.memory.bootstrap import build_deployment

root = Path(sys.argv[1])
config = load_config("dev", config_dir=root / "configs", environ={{}})
{body}
bad = sorted(m for m in sys.modules if m.startswith({forbidden!r}))
assert not bad, bad
"""


def _run(body: str, root: Path = REPO_ROOT) -> None:
    script = SCRIPT.format(body=body, forbidden=FORBIDDEN)
    # -I: cwd 와 PYTHONPATH 를 무시해 현재 프로세스가 아닌 깨끗한 import 그래프를 본다
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-c", script, str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_main_import_pulls_no_control_plane_modules() -> None:
    script = "import aaestro.memory.__main__"
    _run(script)


def test_token_bootstrap_path_pulls_no_control_plane_modules(token_root: Path) -> None:
    _run("build_deployment(config, root=root)", token_root)


def test_registry_bootstrap_path_pulls_no_control_plane_modules() -> None:
    _run("build_deployment(config, root=root, access=object())")
