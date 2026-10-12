"""``python -m aaestro.cli`` entry point.

패키지를 모듈로 실행하는 경로. ``aaestro.cli.main`` 을 직접 실행하면
runpy 가 이중 import 경고를 내므로, 패키지 레벨 진입점을 따로 둔다.
"""

from __future__ import annotations

from aaestro.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
