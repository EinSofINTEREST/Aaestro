"""통합 테스트 공용 fixture.

같은 태그의 이미지를 모듈마다 다시 굽지 않도록 세션당 한 번만 빌드한다 (#571).
"""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKER_DIR = REPO_ROOT / "deployments" / "docker"
BASE_IMAGE = "aaestro/agent-base:0.1.0"
ECHO_IMAGE = "aaestro/agent-echo:0.1.0"
BUILD_TIMEOUT_S = 900

# 태그별 빌드 횟수 — 중복 빌드가 돌아오면 요약에서 드러난다
BUILD_COUNTS: Counter[str] = Counter()


def build_image(tag: str, dockerfile: str, *extra: str) -> None:
    BUILD_COUNTS[tag] += 1
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "docker",
            "build",
            "-t",
            tag,
            *extra,
            "-f",
            str(DOCKER_DIR / dockerfile),
            str(REPO_ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=BUILD_TIMEOUT_S,
    )
    if result.returncode != 0:
        raise AssertionError(f"docker build {tag} failed: {result.stderr}")


@pytest.fixture(scope="session")
def base_image() -> str:
    """지금 소스로 base 를 굽는다 — 옛 이미지면 고치기 전 agentd 를 검증하게 된다."""
    build_image(BASE_IMAGE, "agent-base.Dockerfile")
    return BASE_IMAGE


@pytest.fixture(scope="session")
def echo_image(base_image: str) -> str:
    """echo 이미지 — base 위에 굽는다."""
    build_image(ECHO_IMAGE, "agent-echo.Dockerfile", "--build-arg", "BASE_TAG=0.1.0")
    return ECHO_IMAGE


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    if BUILD_COUNTS:
        terminalreporter.write_line(f"image builds: {dict(BUILD_COUNTS)}")
