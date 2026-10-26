"""Deploy validation report hygiene (#472).

깨진 promptset 하나가 finding 여럿으로 보고되지 않고, 실패한 검증이 passed 로그를 남기지
않으며, promptset · skillset · skillpack ref 의 타입이 스키마에서 강제되는지 고정한다.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from aaestro import authoring, deploy
from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode, field_problems
from tests.fixtures.builders import make_manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
PASSED = "graph and agent checks passed"


def repo_copy(tmp_path: Path) -> Path:
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def break_promptset(root: Path) -> None:
    path = root / "modules" / "promptsets" / "planner" / "0.3.0" / "promptset.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["spec"]["zz_unknown"] = 1
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def test_a_broken_promptset_is_reported_once(tmp_path):
    root = repo_copy(tmp_path)
    break_promptset(root)

    report = Author(Catalog.under(root)).validate()

    # 로드 실패 · node_templates 중복 없이 catalog + module_refs 만 (memoryset 선례)
    assert [f.check for f in report.findings] == ["catalog", "module_refs"]


class _Spy:
    """모듈 전역 로거를 갈아 끼운다 — 다른 테스트가 structlog 설정을 캐시해도 순서와 무관하다."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def info(self, event: str, **_: Any) -> None:
        self.events.append(event)

    warning = error = info


def test_a_failed_validation_does_not_log_a_pass(tmp_path, monkeypatch):
    root = repo_copy(tmp_path)
    # 아무도 쓰지 않는 깨진 모듈 — 그래프 · 에이전트 대조는 통과하고 카탈로그만 실패한다
    unused = root / "modules/skillsets/unused/0.1.0"
    shutil.copytree(root / "modules/skillsets/web-search/0.2.0", unused)
    path = unused / "skillset.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["metadata"].update({"name": "unused", "version": "0.1.0"})
    doc["spec"]["zz_unknown"] = 1
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")

    spy = _Spy()
    monkeypatch.setattr(authoring, "log", spy)
    monkeypatch.setattr(deploy, "log", spy)

    report = Author(Catalog.under(root)).validate()

    assert not report.ok
    assert PASSED not in spy.events
    assert "deployment validation failed" in spy.events


def test_a_passing_validation_still_logs_the_pass(tmp_path, monkeypatch):
    root = repo_copy(tmp_path)

    spy = _Spy()
    monkeypatch.setattr(authoring, "log", spy)
    monkeypatch.setattr(deploy, "log", spy)

    report = Author(Catalog.under(root)).validate()

    assert report.ok
    assert PASSED in spy.events


def spec_with(**overrides: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
        "promptset": {"ref": "promptsets/test@0.1.0"},
    }
    return {**spec, **overrides}


@pytest.mark.parametrize(
    ("field", "wrong", "right"),
    [
        ("promptset", {"ref": "skillsets/x@0.1.0"}, {"ref": "promptsets/x@0.1.0"}),
        ("skillsets", [{"ref": "promptsets/x@0.1.0"}], [{"ref": "skillsets/x@0.1.0"}]),
        ("skillpacks", [{"ref": "skillsets/x@0.1.0"}], [{"ref": "skillpacks/x@0.1.0"}]),
    ],
)
def test_a_module_ref_of_another_type_is_rejected_without_echoing_it(field, wrong, right):
    meta = {"name": "planner", "version": "0.1.0"}
    with pytest.raises(ValidationError) as exc_info:
        make_manifest(metadata=meta, spec=spec_with(**{field: wrong}))

    ref = wrong["ref"] if isinstance(wrong, dict) else wrong[0]["ref"]
    assert "must be of type" in str(field_problems(exc_info.value))
    assert ref not in str(field_problems(exc_info.value))
    assert make_manifest(metadata=meta, spec=spec_with(**{field: right}))


def test_a_failed_graph_check_logs_the_failure_exactly_once(tmp_path, monkeypatch):
    root = repo_copy(tmp_path)
    path = root / "graphs" / "research-pipeline.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    doc["spec"]["nodes"][0]["agent"] = "agents/nobody@0.1.0"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    spy = _Spy()
    monkeypatch.setattr(authoring, "log", spy)
    monkeypatch.setattr(deploy, "log", spy)

    report = Author(Catalog.under(root)).validate()

    assert any(f.check == "agent_refs" for f in report.findings)
    assert spy.events.count("deployment validation failed") == 1
    assert PASSED not in spy.events


def test_a_published_promptset_that_cannot_be_loaded_is_a_finding(tmp_path, monkeypatch):
    """게시됐는데 읽지 못한 선언은 노드 검사에서 빠진 것이지 통과한 것이 아니다."""
    root = repo_copy(tmp_path)

    def broken(self: Catalog, ref: str) -> object:
        raise AaestroError(
            category=ErrorCategory.MODULE, code=ErrorCode.MOD_003, message="unreadable"
        )

    monkeypatch.setattr(Catalog, "promptset", broken)

    report = Author(Catalog.under(root)).validate()

    found = [f for f in report.findings if f.check == "node_templates"]
    assert found and all("could not be loaded" in f.message for f in found)
    assert all(f.code == ErrorCode.MOD_003 for f in found)
