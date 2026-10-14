"""The framework instructions quoted in the docs match the constant (#446).

문서의 문구 블록은 상수를 손으로 줄바꿈해 옮긴 것이다 — 상수만 바뀌면 문서는 옛 지시를 보여 준다.
공백 · 줄바꿈만 정규화하고 문장 전체가 들어 있는지 본다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaestro.modules.boundary import FRAMEWORK_INSTRUCTIONS

REPO_ROOT = Path(__file__).resolve().parents[3]
MODULE_DOCS = [REPO_ROOT / "docs" / lang / "modules.md" for lang in ("en", "ko")]


def normalized(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("doc", MODULE_DOCS, ids=lambda path: path.parent.name)
def test_modules_doc_quotes_the_framework_instructions_verbatim(doc: Path) -> None:
    assert normalized(FRAMEWORK_INSTRUCTIONS) in normalized(doc.read_text(encoding="utf-8"))
