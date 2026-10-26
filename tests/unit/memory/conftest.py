"""Shared fixtures for the Memory Service tests."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def token_root(tmp_path: Path) -> Path:
    """저장소 선언에서 그래프만 뺀 루트.

    토큰 모드는 run scope 를 선언한 그래프가 있으면 뜨지 않는다 (#469).
    """
    root = tmp_path / "token-root"
    root.mkdir()
    for name in ("agents", "groups", "modules", "configs"):
        (root / name).symlink_to(REPO_ROOT / name, target_is_directory=True)
    (root / "graphs").mkdir()
    return root
