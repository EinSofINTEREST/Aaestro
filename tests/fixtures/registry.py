"""Registry fixtures backed by real module directories.

실제 모듈 디렉토리를 사용하는 레지스트리 픽스처 — 스키마와 픽스처의 드리프트를 막는다.
"""

from __future__ import annotations

from pathlib import Path

from aaestro.modules.registry import ModuleRegistry

FIXTURE_ROOT = Path(__file__).parent / "modules"


def fixture_registry() -> ModuleRegistry:
    """Build a registry over the checked-in fixture modules.

    커밋된 픽스처 모듈을 가리키는 레지스트리를 만듭니다.
    """
    return ModuleRegistry.under(FIXTURE_ROOT)
