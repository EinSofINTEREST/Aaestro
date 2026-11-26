"""Promptset 게시 검증 — 템플릿 본문까지 읽어 배포 전에 거절한다 (#716).

선언만 읽으면 거절 구문 · 없는 필터 · 제거된 전역이 든 promptset 이 배포를 통과해 agentd 기동이나
렌더에서야 터진다. 게시 목록 (배포 검증의 ``resolvable_refs``) 이 그것을 걸러야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.catalog import Catalog
from aaestro.core.errors import ErrorCode

REF = "promptsets/solo@0.1.0"


def publish(root: Path, body: str, *, locale_body: str | None = None) -> Catalog:
    module = root / "modules" / "promptsets" / "solo" / "0.1.0"
    (module / "templates").mkdir(parents=True)
    document = {
        "apiVersion": "aaestro/v1",
        "kind": "Promptset",
        "metadata": {"name": "solo", "version": "0.1.0"},
        "spec": {
            "engine": "jinja2",
            "templates": {
                "system": {"file": "templates/system.j2"},
                "default": {
                    "file": "templates/default.j2",
                    "variables": {"query": {"type": "string"}},
                },
            },
        },
    }
    (module / "promptset.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    (module / "templates" / "system.j2").write_text("You help.", encoding="utf-8")
    (module / "templates" / "default.j2").write_text(body, encoding="utf-8")
    if locale_body is not None:
        (module / "locales" / "ko").mkdir(parents=True)
        (module / "locales" / "ko" / "default.j2").write_text(locale_body, encoding="utf-8")
    return Catalog.under(root)


def test_a_clean_template_is_published(tmp_path):
    catalog = publish(tmp_path, "{{ query }} {% for i in range(2) %}{{ i }}{% endfor %}")

    assert REF in catalog.module_refs()


@pytest.mark.parametrize(
    "body",
    [
        "{% include 'x' %}",  # 경계를 우회하는 구문
        "{% if query %}",  # 문법 오류
        "{{ query | nosuchfilter }}",  # 없는 필터 — 컴파일에서 드러난다
        "{{ lipsum() }}",  # 제거된 전역
        "{{ query | random }}",  # 제거된 필터
        "{{ stranger }}",  # 선언하지 않은 변수
    ],
)
def test_a_bad_template_body_is_not_published(tmp_path, body):
    catalog = publish(tmp_path, body)

    assert REF not in catalog.module_refs()
    problems = catalog.modules("promptsets").problems
    assert [p.code for p in problems] == [ErrorCode.MOD_003]


def test_a_bad_locale_override_is_not_published(tmp_path):
    catalog = publish(tmp_path, "{{ query }}", locale_body="{{ lipsum() }}")

    assert REF not in catalog.module_refs()


def test_the_rejection_never_echoes_the_template_text(tmp_path):
    catalog = publish(tmp_path, "{{ secret_marker_xyz }}")

    problem = catalog.modules("promptsets").problems[0]

    assert "secret_marker_xyz" not in repr(problem)


def test_every_promptset_in_the_repository_is_published():
    root = Path(__file__).resolve().parents[3]

    listed = Catalog.under(root).modules("promptsets")

    assert listed.problems == ()
    assert listed.items
