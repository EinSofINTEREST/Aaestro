"""Web UI — 정적 파일. control plane 이 `/ui` 에서 서빙한다 (#245).

UI 는 REST(`/v1/*`)만 부른다 — 파일시스템·Docker 에 직접 닿지 않는다.
"""

from pathlib import Path

UI_ROOT = Path(__file__).parent
"""정적 자산 루트 — index.html / app.js / client.js / app.css."""

__all__ = ["UI_ROOT"]
