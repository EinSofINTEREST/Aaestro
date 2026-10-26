"""진입점 import 계약 테스트의 공통 헬퍼 (#432).

진입점 프로세스가 금지된 모듈을 끌어오지 않는지 격리된 서브프로세스 (``python -I``) 의
``sys.modules`` 로 본다. 같은 프로세스의 sys.modules 는 다른 테스트가 오염시킨다.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

_SCRIPT = """
import os, sys
{body}
forbidden = {forbidden!r}
# 접두사가 아니라 패키지 경계로 맞춘다 — "aaestro.config" 가 "aaestro.configs" 를 잡지 않게
bad = sorted(
    m for m in sys.modules
    if any(m == f or m.startswith(f + ".") for f in forbidden)
)
assert not bad, bad
"""


def assert_no_forbidden_imports(
    body: str, forbidden: tuple[str, ...], *, repo_root: Path = REPO_ROOT
) -> None:
    """``body`` 를 실행한 깨끗한 인터프리터에 ``forbidden`` 모듈이 올라오지 않았음을 단언한다.

    ``sys.argv[1]`` 에 저장소 루트가 주어진다.
    """
    script = _SCRIPT.format(body=textwrap.dedent(body), forbidden=forbidden)
    # -I: cwd 와 PYTHONPATH 를 무시해 현재 프로세스가 아닌 깨끗한 import 그래프를 본다
    result = subprocess.run(  # noqa: S603 — 고정 인자, 격리 모드 인터프리터
        [sys.executable, "-I", "-c", script, str(repo_root)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
