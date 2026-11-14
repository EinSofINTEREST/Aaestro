"""Bench Compare — compare pytest-benchmark JSON against a baseline and warn on slowdowns.

기준선 (main push 의 `make bench` 결과) 과 PR 의 결과를 비교해 평균이 임계 이상 느려진 벤치마크를
GitHub 경고 annotation 과 job summary 로 남긴다 (#565). 정보성이라 어떤 경우에도 종료 코드는 0 이다.
JSON 은 신뢰하지 않는 입력이다 — 숫자와 형식이 맞는 이름만 읽고, 그 밖의 문자열은 출력하지 않는다.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

# 공유 러너의 측정 소음 (수 ~ 십수 %) 을 넘는 변화만 회귀로 본다
THRESHOLD_RATIO = 1.25
# annotation 에 싣는 이름은 이 형식만 — 줄바꿈 · `::` 로 워크플로 명령을 주입하지 못하게
NAME_PATTERN = re.compile(r"[A-Za-z0-9_.\[\]-]{1,200}")


def load_means(path: Path) -> dict[str, float]:
    """Read `{name: stats.mean}` from a pytest-benchmark JSON, dropping malformed entries."""
    data: Any = json.loads(path.read_text("utf-8"))
    means: dict[str, float] = {}
    entries = data.get("benchmarks") if isinstance(data, dict) else None
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("stats"), dict):
            continue
        name, mean = entry.get("name"), entry["stats"].get("mean")
        if (
            isinstance(name, str)
            and NAME_PATTERN.fullmatch(name)
            and isinstance(mean, int | float)
            and not isinstance(mean, bool)
            and math.isfinite(mean)
            and mean > 0
        ):
            means[name] = float(mean)
    return means


def compare(baseline: dict[str, float], current: dict[str, float]) -> list[str]:
    """One summary line per benchmark in both files; regressions start with `REGRESSION`."""
    lines = []
    for name in sorted(baseline.keys() & current.keys()):
        ratio = current[name] / baseline[name]
        tag = "REGRESSION" if ratio >= THRESHOLD_RATIO else "ok"
        lines.append(f"{tag} {name}: {baseline[name]:.3e}s -> {current[name]:.3e}s (x{ratio:.2f})")
    return lines


def report(lines: list[str], note: str | None = None) -> None:
    out = [f"### Benchmark comparison (warn at x{THRESHOLD_RATIO:.2f} mean)", ""]
    if note:
        out.append(note)
    out += [f"- {line}" for line in lines]
    summary = "\n".join(out)
    print(summary)
    for line in lines:
        if line.startswith("REGRESSION"):
            print(f"::warning title=Benchmark regression::{line}")
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        # 경고를 이미 냈으므로 summary 쓰기 실패가 그것을 지우거나 job 을 실패시키지 않는다
        try:
            with Path(target).open("a", encoding="utf-8") as fh:
                fh.write(summary + "\n")
        except OSError:
            print("job summary not written (unwritable GITHUB_STEP_SUMMARY)", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    args = parser.parse_args(argv)

    if not args.baseline.is_file():
        report([], "No baseline yet (first run or no main baseline artifact) - comparison skipped.")
        return 0
    try:
        baseline, current = load_means(args.baseline), load_means(args.current)
    except Exception:  # 정보성 job: 어떤 입력도 실패시키지 않는다
        # 예외 문자열은 신뢰하지 않는 입력의 값을 실을 수 있어 출력하지 않는다
        report([], "Comparison skipped (unreadable input).")
        return 0
    lines = compare(baseline, current)
    report(lines, None if lines else "No benchmark in common - nothing to compare.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
