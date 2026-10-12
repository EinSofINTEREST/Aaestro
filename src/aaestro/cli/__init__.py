"""The ``aaestro`` command-line interface.

운영자가 프레임워크를 다루는 표면.
"""

from aaestro.cli.integrity import (
    Discrepancy,
    dangling_module_refs,
    ghost_containers,
    orphan_checkpoints,
)

# ``main`` 은 여기서 re-export 하지 않는다 — 같은 이름의 하위 모듈을 가린다 (#87).
# 진입점은 ``aaestro.cli.main:main`` 을 직접 가리킨다
from aaestro.cli.main import build_parser

__all__ = [
    "Discrepancy",
    "build_parser",
    "dangling_module_refs",
    "ghost_containers",
    "orphan_checkpoints",
]
