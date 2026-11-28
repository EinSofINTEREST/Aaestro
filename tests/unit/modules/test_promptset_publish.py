"""Promptset 게시 검증 — 템플릿 본문까지 읽어 배포 전에 거절한다 (#716).

선언만 읽으면 거절 구문 · 없는 필터 · 제거된 전역이 든 promptset 이 배포를 통과해 agentd 기동이나
렌더에서야 터진다. 게시 목록 (배포 검증의 ``resolvable_refs``) 이 그것을 걸러야 한다.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCode

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
        "{% if query is nosuchtest %}x{% endif %}",  # 조건 자리의 없는 test
        "{{ 'a' if query is nosuch else 'b' }}",  # 삼항 자리의 없는 test
        "{{ stranger is defined }}",  # is defined 로 감싸도 선언 밖 이름이다
        "{{ " + "(" * 80 + "1" + ")" * 80 + " }}",  # 깊은 중첩 — RecursionError
    ],
)
def test_a_bad_template_body_is_not_published(tmp_path, body):
    catalog = publish(tmp_path, body)

    assert REF not in catalog.module_refs()
    problems = catalog.modules("promptsets").problems
    assert [p.code for p in problems] == [ErrorCode.MOD_003]
    # 단건 조회도 같은 검증을 지난다 — 목록에 없는 것이 200 이 되지 않는다
    with pytest.raises(AaestroError) as excinfo:
        catalog.module("promptsets", "solo", "0.1.0")
    assert excinfo.value.code == ErrorCode.MOD_003
    assert excinfo.value.__cause__ is None or not isinstance(
        excinfo.value.__cause__, RecursionError
    )


def test_a_deep_nesting_only_drops_that_promptset(tmp_path):
    publish(tmp_path, "{{ " + "(" * 80 + "1" + ")" * 80 + " }}")
    good = tmp_path / "modules" / "promptsets" / "good" / "0.1.0"
    good.mkdir(parents=True)
    (good / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Promptset",
                "metadata": {"name": "good", "version": "0.1.0"},
                "spec": {"engine": "jinja2", "templates": {"system": {"file": "s.j2"}}},
            }
        ),
        encoding="utf-8",
    )
    (good / "s.j2").write_text("ok", encoding="utf-8")

    listed = Catalog.under(tmp_path).modules("promptsets")

    assert listed.items == {"good": ("0.1.0",)}
    assert [p.code for p in listed.problems] == [ErrorCode.MOD_003]


def test_the_rejection_names_the_undeclared_names_and_the_locale(tmp_path):
    publish(tmp_path, "{{ query }}", locale_body="{{ zeta }} {{ alpha }}")

    with pytest.raises(AaestroError) as excinfo:
        Catalog.under(tmp_path).module("promptsets", "solo", "0.1.0")

    assert excinfo.value.details["names"] == ["alpha", "zeta"]
    assert excinfo.value.details["locale"] == "ko"


def test_a_bad_locale_override_is_not_published(tmp_path):
    catalog = publish(tmp_path, "{{ query }}", locale_body="{{ lipsum() }}")

    assert REF not in catalog.module_refs()


def test_the_rejection_never_echoes_the_template_text(tmp_path):
    catalog = publish(tmp_path, "{{ secret_marker_xyz }}")

    problem = catalog.modules("promptsets").problems[0]

    assert "secret_marker_xyz" not in repr(problem)


@pytest.mark.parametrize("where", ["default", "locale"])
def test_a_non_utf8_template_only_drops_that_promptset(tmp_path, where):
    catalog = publish(tmp_path, "{{ query }}", locale_body="{{ query }}")
    module = tmp_path / "modules" / "promptsets" / "solo" / "0.1.0"
    bad = (
        module / "templates" / "default.j2"
        if where == "default"
        else (module / "locales" / "ko" / "default.j2")
    )
    bad.write_bytes(b"\xff\xfe secret_marker_xyz")
    good = tmp_path / "modules" / "promptsets" / "good" / "0.1.0"
    good.mkdir(parents=True)
    (good / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Promptset",
                "metadata": {"name": "good", "version": "0.1.0"},
                "spec": {"engine": "jinja2", "templates": {"system": {"file": "s.j2"}}},
            }
        ),
        encoding="utf-8",
    )
    (good / "s.j2").write_text("ok", encoding="utf-8")

    listed = catalog.modules("promptsets")

    assert listed.items == {"good": ("0.1.0",)}
    assert [p.code for p in listed.problems] == [ErrorCode.MOD_003]
    assert "secret_marker_xyz" not in repr(listed.problems[0])
    with pytest.raises(AaestroError) as excinfo:
        catalog.module("promptsets", "solo", "0.1.0")
    assert excinfo.value.code == ErrorCode.MOD_003
    assert excinfo.value.details["cause"] == "UnicodeDecodeError"
    assert excinfo.value.__cause__ is None


def _loop_nest(depth: int) -> str:
    return "{% for i in [1] %}" * depth + "x" + "{% endfor %}" * depth


def test_a_template_too_deep_for_the_compiler_only_drops_that_promptset(tmp_path):
    # 정적 중첩 21 겹은 파싱을 지나 컴파일러의 SyntaxError 가 된다 — 20 겹까지는 게시된다
    catalog = publish(tmp_path, _loop_nest(21))
    good = tmp_path / "modules" / "promptsets" / "good" / "0.1.0"
    good.mkdir(parents=True)
    (good / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Promptset",
                "metadata": {"name": "good", "version": "0.1.0"},
                "spec": {"engine": "jinja2", "templates": {"system": {"file": "s.j2"}}},
            }
        ),
        encoding="utf-8",
    )
    (good / "s.j2").write_text(_loop_nest(20), encoding="utf-8")

    listed = catalog.modules("promptsets")

    assert listed.items == {"good": ("0.1.0",)}
    assert [p.code for p in listed.problems] == [ErrorCode.MOD_003]
    with pytest.raises(AaestroError) as excinfo:
        catalog.module("promptsets", "solo", "0.1.0")
    assert excinfo.value.code == ErrorCode.MOD_003
    assert excinfo.value.__cause__ is None


def test_an_unexpected_compiler_failure_only_drops_that_promptset(tmp_path):
    # 정수 리터럴 한도 (4300 자리) 의 ValueError — 부류로 가둔다
    catalog = publish(tmp_path, "{{ 1" + "0" * 5000 + " }}")
    good = tmp_path / "modules" / "promptsets" / "good" / "0.1.0"
    good.mkdir(parents=True)
    (good / "promptset.yaml").write_text(
        yaml.safe_dump(
            {
                "apiVersion": "aaestro/v1",
                "kind": "Promptset",
                "metadata": {"name": "good", "version": "0.1.0"},
                "spec": {"engine": "jinja2", "templates": {"system": {"file": "s.j2"}}},
            }
        ),
        encoding="utf-8",
    )
    (good / "s.j2").write_text("ok", encoding="utf-8")

    listed = catalog.modules("promptsets")

    assert listed.items == {"good": ("0.1.0",)}
    assert [p.code for p in listed.problems] == [ErrorCode.MOD_003]
    with pytest.raises(AaestroError) as excinfo:
        catalog.module("promptsets", "solo", "0.1.0")
    assert excinfo.value.details["cause"] == "ValueError"
    assert excinfo.value.__cause__ is None


def test_a_refused_syntax_in_a_locale_names_the_locale_and_drops_the_cause(tmp_path):
    catalog = publish(tmp_path, "{{ query }}", locale_body="{% include 'x' %}")

    with pytest.raises(AaestroError) as excinfo:
        catalog.module("promptsets", "solo", "0.1.0")

    assert excinfo.value.code == ErrorCode.MOD_003
    assert excinfo.value.details["locale"] == "ko"
    assert excinfo.value.details["node"] == "Include"
    assert excinfo.value.__cause__ is None


def test_a_control_character_in_a_template_name_never_reaches_the_message(tmp_path):
    catalog = publish(tmp_path, "{{ stranger }}")
    module = tmp_path / "modules" / "promptsets" / "solo" / "0.1.0"
    path = module / "promptset.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    templates = document["spec"]["templates"]
    templates["bad\x1b[31mname"] = templates.pop("default")
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(AaestroError) as excinfo:
        catalog.module("promptsets", "solo", "0.1.0")

    assert excinfo.value.code == ErrorCode.MOD_003
    assert "\x1b" not in excinfo.value.message
    assert "\x1b" not in repr(excinfo.value.details)


def test_every_promptset_in_the_repository_is_published():
    root = Path(__file__).resolve().parents[3]

    listed = Catalog.under(root).modules("promptsets")

    assert listed.problems == ()
    assert listed.items
