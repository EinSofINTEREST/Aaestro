"""Tests for scripts/ci/bench_compare.py — warn-only benchmark regression report (#565)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.ci import bench_compare


def write(path: Path, means: dict[str, object]) -> Path:
    benchmarks = [{"name": name, "stats": {"mean": mean}} for name, mean in means.items()]
    path.write_text(json.dumps({"benchmarks": benchmarks}), "utf-8")
    return path


def run(baseline: Path, current: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = bench_compare.main([str(baseline), str(current)])
    return code, capsys.readouterr().out


def test_slowdown_over_threshold_warns_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    base = write(tmp_path / "b.json", {"test_a": 1.0})
    cur = write(tmp_path / "c.json", {"test_a": 1.5})

    code, out = run(base, cur, capsys)

    assert code == 0
    assert "::warning title=Benchmark regression::REGRESSION test_a" in out


def test_within_threshold_is_silent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = write(tmp_path / "b.json", {"test_a": 1.0})
    cur = write(tmp_path / "c.json", {"test_a": 1.2})

    code, out = run(base, cur, capsys)

    assert code == 0
    assert "::warning" not in out


def test_missing_baseline_skips_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cur = write(tmp_path / "c.json", {"test_a": 1.0})

    code, out = run(tmp_path / "none.json", cur, capsys)

    assert code == 0
    assert "No baseline yet" in out


def test_malformed_entries_and_names_are_ignored(tmp_path: Path) -> None:
    path = write(
        tmp_path / "b.json",
        {"ok": 1.0, "evil\n::error::x": 1.0, "neg": -1.0, "nan": float("nan"), "s": "1"},
    )

    assert bench_compare.load_means(path) == {"ok": 1.0}


def test_unreadable_json_does_not_fail(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    bad = tmp_path / "b.json"
    bad.write_text("{not json", "utf-8")
    cur = write(tmp_path / "c.json", {"test_a": 1.0})

    code, out = run(bad, cur, capsys)

    assert code == 0
    assert "unreadable" in out


@pytest.mark.parametrize(
    "payload",
    [
        {"benchmarks": None},
        {"benchmarks": [{"name": "test_a", "stats": None}]},
        {"benchmarks": [None, 3, {"name": "test_a"}]},
        [],
        42,
    ],
)
def test_wrong_shape_json_is_skipped_with_rc_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], payload: object
) -> None:
    bad = tmp_path / "b.json"
    bad.write_text(json.dumps(payload), "utf-8")
    cur = write(tmp_path / "c.json", {"test_a": 1.0})

    code, out = run(bad, cur, capsys)

    assert code == 0
    assert "::warning" not in out


def test_unwritable_summary_still_emits_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path))  # 디렉토리라 열 수 없다
    base = write(tmp_path / "b.json", {"test_a": 1.0})
    cur = write(tmp_path / "c.json", {"test_a": 2.0})

    code, out = run(base, cur, capsys)

    assert code == 0
    assert "::warning title=Benchmark regression::REGRESSION test_a" in out


def test_summary_is_written_to_the_given_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    base = write(tmp_path / "b.json", {"test_a": 1.0})
    cur = write(tmp_path / "c.json", {"test_a": 1.0})

    run(base, cur, capsys)

    assert "ok test_a" in summary.read_text("utf-8")
