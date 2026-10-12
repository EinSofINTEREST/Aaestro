"""Targets that bring a stack up must build the base image first.

compose 파일의 에이전트 이미지는 `aaestro/agent-base` 를 `FROM` 으로 쓰는데,
compose 는 그것을 빌드하지 않는다 — `--build` 는 compose 가 아는 서비스만 굽는다.
그래서 `make e2e-up` 은 로컬에 남아 있는 **옛 base** 위에 스택을 올렸고,
프레임워크를 고쳐도 컨테이너 안은 그대로였다 (#222). E2E 가 초록이어도 방금 만든
변경을 검증한 것이 아니었다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

MAKEFILE = Path(__file__).resolve().parents[2] / "Makefile"
BASE_TARGET = "build-base"


def targets() -> dict[str, tuple[list[str], list[str]]]:
    """타깃 이름 → (선행 조건, 레시피 줄) 로 Makefile 을 읽는다."""
    parsed: dict[str, tuple[list[str], list[str]]] = {}
    current: str | None = None
    for line in MAKEFILE.read_text("utf-8").splitlines():
        if line.startswith("\t"):
            if current is not None:
                parsed[current][1].append(line.strip())
            continue
        match = re.match(r"^([A-Za-z][\w-]*):(.*)$", line)
        if match is None:
            current = None
            continue
        current = match.group(1)
        prereqs = match.group(2).split("##")[0].split()
        parsed[current] = (prereqs, [])
    return parsed


def stack_targets() -> list[str]:
    """`docker compose ... up` 을 실행하는 타깃들."""
    return [
        name
        for name, (_, recipe) in targets().items()
        if any("docker compose" in line and " up" in line for line in recipe)
    ]


def test_stack_targets_are_discovered():
    """하나도 못 찾으면 아래 테스트가 공허하게 통과한다."""
    assert stack_targets(), "docker compose 로 스택을 올리는 타깃을 찾지 못했다"


def test_the_base_image_target_exists():
    assert BASE_TARGET in targets(), f"{BASE_TARGET} 타깃이 없다"


@pytest.mark.parametrize("name", stack_targets())
def test_a_stack_target_builds_the_base_first(name: str):
    """#222 — 굽지 않고 올리면 옛 이미지를 검증한다."""
    prereqs, _ = targets()[name]

    assert BASE_TARGET in prereqs, f"{name} 이 {BASE_TARGET} 를 선행하지 않는다"


@pytest.mark.parametrize("name", stack_targets())
def test_a_stack_target_rebuilds_its_own_services(name: str):
    """base 만 새로 구워도 그 위 이미지를 다시 굽지 않으면 옛 코드가 남는다."""
    _, recipe = targets()[name]
    up_lines = [line for line in recipe if "docker compose" in line and " up" in line]

    assert all("--build" in line for line in up_lines), f"{name} 의 up 에 --build 가 없다"
