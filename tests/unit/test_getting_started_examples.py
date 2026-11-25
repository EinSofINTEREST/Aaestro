"""getting-started 의 yaml 예시가 실제 스키마로 로드된다 (#412) — 문서 예시의 드리프트 방지."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.core.topology import GraphTopology

DOCS = Path(__file__).resolve().parents[2] / "docs"
SCHEMAS = {"Agent": AgentManifest, "Graph": GraphTopology, "Group": GroupManifest}


def _examples(lang: str) -> list[dict]:
    text = (DOCS / lang / "getting-started.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)```", text, flags=re.DOTALL)
    return [yaml.safe_load(block) for block in blocks]


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_getting_started_examples_load_with_the_real_schemas(lang: str) -> None:
    examples = _examples(lang)

    assert {doc["kind"] for doc in examples} == set(SCHEMAS)
    for doc in examples:
        SCHEMAS[doc["kind"]].model_validate(doc)
