"""control plane 의존이 가벼운 프로세스 · core 로 새지 않는 정적 경계.

subprocess 검사 (``tests/unit/{memory,egress}/test_import_boundary.py``) 는 탄 경로만 본다.
요청 시점 · 서빙 경로의 함수 안 import 는 AST 로 전수 검사한다 (#431).
``TYPE_CHECKING`` 블록은 런타임 의존이 아니므로 제외하되, core 는 그것도 금지한다.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

SRC = Path(__file__).resolve().parents[2] / "src" / "aaestro"

CONTROL_PLANE = (
    "aaestro.runtime",
    "aaestro.orchestrator",
    "langgraph",
    "langchain",
)
LIGHT_PACKAGES = ("memory", "egress", "access", "modules", "observability")
LIGHT_MODULES = ("catalog.py", "config.py")


def _targets(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
        # `from aaestro import runtime` 형태도 잡는다
        return [node.module, *(f"{node.module}.{alias.name}" for alias in node.names)]
    return []


def _is_type_checking(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _imports(tree: ast.AST, *, include_type_checking: bool) -> list[tuple[int, str]]:
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST) -> None:
        if not include_type_checking and _is_type_checking(node):
            for child in node.orelse:  # type: ignore[attr-defined]
                visit(child)
            return
        found.extend((getattr(node, "lineno", 0), name) for name in _targets(node))
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return found


def _in_control_plane(name: str) -> bool:
    return any(name == f or name.startswith(f"{f}.") for f in CONTROL_PLANE)


def _outside_core(name: str) -> bool:
    """core 는 자기 자신 외의 모든 내부 패키지에 의존하지 않는다 (07 의존 방향)."""
    inside = name == "aaestro.core" or name.startswith("aaestro.core.")
    return name.startswith("aaestro.") and not inside


def _violations(
    files: list[Path], forbidden: Callable[[str], bool], *, type_checking: bool
) -> list[str]:
    bad: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, name in _imports(tree, include_type_checking=type_checking):
            if forbidden(name):
                bad.append(f"{path.relative_to(SRC)}:{lineno} imports {name}")
    return bad


def _files(packages: tuple[str, ...], modules: tuple[str, ...] = ()) -> list[Path]:
    files = [p for pkg in packages for p in sorted((SRC / pkg).rglob("*.py"))]
    return files + [SRC / m for m in modules]


@pytest.mark.parametrize("package", LIGHT_PACKAGES)
def test_light_process_packages_never_import_the_control_plane(package: str) -> None:
    assert _violations(_files((package,)), _in_control_plane, type_checking=False) == []


def test_catalog_and_config_never_import_the_control_plane() -> None:
    assert _violations(_files((), LIGHT_MODULES), _in_control_plane, type_checking=False) == []


def test_core_depends_on_nothing_outside_core() -> None:
    # TYPE_CHECKING 안의 참조도 막는다 — mypy 가 core 에서 바깥을 끌어오면 방향이 뒤집힌다
    assert _violations(_files(("core",)), _outside_core, type_checking=True) == []


def test_the_checker_sees_function_level_imports(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n    import langgraph\n"
        "def f():\n    from aaestro.runtime.memory import ServiceMemoryAccess\n",
        encoding="utf-8",
    )
    tree = ast.parse(sample.read_text(encoding="utf-8"))
    names = [n for _, n in _imports(tree, include_type_checking=False)]
    assert "aaestro.runtime.memory" in names and "langgraph" not in names
