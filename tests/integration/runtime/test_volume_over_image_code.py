"""A writable volume over the image code directory persists edits to agent code (#524).

배포 검증이 ``/app`` 과 겹치는 매니페스트 볼륨을 거절하는 근거를 실 Docker 로 고정한다: 처음
걸리는 이름 있는 볼륨은 이미지의 ``/app`` 내용을 복사받고, 읽기 전용 rootfs 여도 그 볼륨에
쓴 것이 다음 컨테이너에 남는다 — 침해된 에이전트가 자기 코드를 재시작 너머로 바꾸는 자리다.
"""

from __future__ import annotations

import subprocess
import uuid
from collections.abc import Iterator

import pytest

from aaestro.runtime.deployments import IMAGE_CODE_PATH, RUNTIME_OWNED_PATHS
from aaestro.runtime.spec import WRITABLE_TMPFS


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603
        ["docker", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


pytestmark = pytest.mark.integration


@pytest.fixture
def volume() -> Iterator[str]:
    name = f"aaestro-it524-{uuid.uuid4().hex[:8]}.code"
    yield name
    docker("volume", "rm", "-f", name, check=False)


def run_hardened(image: str, volume: str, script: str) -> str:
    tmpfs = [flag for path in WRITABLE_TMPFS for flag in ("--tmpfs", path)]
    return docker(
        "run", "--rm", "--read-only", *tmpfs,
        "-v", f"{volume}:{IMAGE_CODE_PATH}",
        "--entrypoint", "sh", image, "-c", script,
    )  # fmt: skip


def test_a_fresh_volume_over_app_copies_the_image_code_and_keeps_edits(echo_image, volume):
    # echo 이미지는 /app 에 에이전트 소유 파일 (manifest.yaml) 을 싣는다 — base 의 /app 은 비어 있다
    assert (IMAGE_CODE_PATH, "image") in RUNTIME_OWNED_PATHS

    first = run_hardened(
        echo_image, volume, f"echo tampered > {IMAGE_CODE_PATH}/manifest.yaml && echo ok"
    )
    again = run_hardened(echo_image, volume, f"cat {IMAGE_CODE_PATH}/manifest.yaml")

    assert first == "ok"
    assert again == "tampered"
