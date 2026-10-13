"""Shared pytest configuration.

전역 테스트 설정 — fixture 는 tests/fixtures/ 에 두고 테스트가 직접 import 한다.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog

# 레포 루트를 import 루트로 노출해 `tests.fixtures` 패키지를 참조할 수 있게 한다
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def _reset_structlog() -> Iterator[None]:
    """전역 로깅 설정이 다음 테스트로 새지 않게 되돌린다.

    진입점(``main``)을 부르는 테스트는 로거 캐시를 켠 설정을 남긴다. 남으면 그 뒤에 처음 쓰인 모듈
    로거가 그 설정으로 굳어, 뒤의 ``capture_logs`` 가 이유 없이 비어 보인다.
    """
    yield
    structlog.reset_defaults()
