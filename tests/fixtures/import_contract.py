"""진입점 import 계약 테스트의 공통 헬퍼 (#432).

진입점 프로세스가 금지된 모듈을 끌어오지 않는지 격리된 서브프로세스 (``python -I``) 의
``sys.modules`` 로 본다. 같은 프로세스의 sys.modules 는 다른 테스트가 오염시킨다.
"""

from __future__ import annotations

import re
import subprocess
import sys
import textwrap
import tomllib
from collections.abc import Iterable, Mapping
from importlib.metadata import packages_distributions
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _normalize(name: str) -> str:
    """PEP 503 정규화 — 대소문자와 ``-`` ``_`` ``.`` 를 같게 본다."""
    return re.sub(r"[-_.]+", "-", name).lower()


def runtime_dependencies(repo_root: Path = REPO_ROOT) -> frozenset[str]:
    """pyproject ``[project].dependencies`` 의 정규화된 배포판 이름 (extras · 버전 제외)."""
    data = tomllib.loads((repo_root / "pyproject.toml").read_text("utf-8"))
    names = set()
    for spec in data["project"]["dependencies"]:
        match = re.match(r"[A-Za-z0-9][A-Za-z0-9._-]*", spec)
        assert match, f"unparseable dependency: {spec!r}"
        names.add(_normalize(match.group()))
    return frozenset(names)


def modules_of(dependencies: Iterable[str]) -> tuple[str, ...]:
    """의존성 이름 (pyproject 표기) 에서 설치된 최상위 모듈 이름을 유도한다.

    배포판 하나가 여러 모듈을 줄 수 있고 (pyyaml → yaml, _yaml), 여러 배포판이 한 모듈을
    나눠 쓸 수도 있다 (langgraph-checkpoint-* → langgraph). 설치돼 있지 않아 모듈을
    찾지 못하면 실패시킨다 — 빈 금지 목록으로 조용히 통과하지 않게.
    """
    owners: dict[str, set[str]] = {}
    for module, dists in packages_distributions().items():
        for dist in dists:
            owners.setdefault(_normalize(dist), set()).add(module)
    modules: set[str] = set()
    for dep in dependencies:
        found = owners.get(_normalize(dep))
        assert found, f"dependency {dep!r} is not installed, cannot derive its modules"
        modules |= found
    return tuple(sorted(modules))


def assert_dependencies_classified(
    forbidden: Iterable[str], allowed: Mapping[str, str], *, repo_root: Path = REPO_ROOT
) -> None:
    """pyproject 런타임 의존성이 각각 정확히 한 번 "금지" 또는 "허용 (사유)" 로 분류됐음을 단언한다.

    분류 없이 들어온 새 의존성, pyproject 에서 사라진 낡은 항목, 양쪽에 걸친 항목,
    사유 없는 허용을 모두 실패로 본다.
    """
    declared = runtime_dependencies(repo_root)
    forbidden_set = {_normalize(d) for d in forbidden}
    allowed_set = {_normalize(d) for d in allowed}
    assert not forbidden_set & allowed_set, sorted(forbidden_set & allowed_set)
    classified = forbidden_set | allowed_set
    assert declared <= classified, f"unclassified dependencies: {sorted(declared - classified)}"
    assert classified <= declared, f"not in pyproject: {sorted(classified - declared)}"
    assert all(reason.strip() for reason in allowed.values()), "allowed needs a reason"


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
