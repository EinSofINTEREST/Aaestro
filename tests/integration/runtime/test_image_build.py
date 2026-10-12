"""Baking an agent image for real (#265).

단위 테스트는 Docker 대역 위에서 조립만 본다. 여기서는 **실제로 굽고 그 이미지를 띄운다** —
조립이 맞아도 굽히지 않거나, 구워져도 agentd 가 뜨지 않을 수 있다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from aaestro.catalog import Catalog
from aaestro.materials import InMemoryMaterialStore, Materials
from aaestro.runtime.docker.client import SdkDockerClient
from aaestro.runtime.images import BuildStatus, ImageBuilder, InMemoryBuildStore

REPO_ROOT = Path(__file__).resolve().parents[3]
DOCKER_BIN = shutil.which("docker")
BASE_IMAGE = "aaestro/agent-base:0.1.0"
READY_TIMEOUT_S = 90.0

pytestmark = pytest.mark.integration


def docker(*args: str, check: bool = True) -> str:
    result = subprocess.run(  # noqa: S603
        [DOCKER_BIN or "docker", *args], capture_output=True, text=True, check=False, timeout=600
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def base_image_present() -> bool:
    if DOCKER_BIN is None:
        return False
    try:
        return bool(docker("images", "-q", BASE_IMAGE, check=False))
    except (OSError, subprocess.SubprocessError):
        return False


requires_base = pytest.mark.skipif(
    not base_image_present(), reason=f"{BASE_IMAGE} is not built (make build-base)"
)


@contextmanager
def serve(image: str) -> Iterator[int]:
    """구운 이미지를 **기본 엔트리포인트로** 띄운다 — agentd 가 뜨는지는 이렇게만 보인다.

    배포가 거는 것과 같은 격리 플래그를 건다 (02 Security 3): read-only rootfs,
    cap-drop, non-root. 여기서 뜨지 않으면 배포에서도 뜨지 않는다.
    """
    name = f"aaestro-baked-{uuid.uuid4().hex[:8]}"
    container = docker(
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--pids-limit=256",
        "--tmpfs=/tmp",
        "--tmpfs=/workspace",
        "-e",
        "ANTHROPIC_API_KEY=test-key",
        "-P",
        image,
    )
    try:
        yield int(docker("port", container, "8080/tcp").rsplit(":", 1)[-1])
    finally:
        docker("logs", container, check=False)
        docker("rm", "-f", container, check=False)


def wait_for_health(port: int) -> dict:
    """`/v1/health` 가 응답할 때까지. 무인증이다 — Docker healthcheck 가 직접 부른다."""
    deadline = time.monotonic() + READY_TIMEOUT_S
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(  # noqa: S310 — 루프백 고정 URL
                f"http://127.0.0.1:{port}/v1/health", timeout=5
            ) as response:
                body: dict = json.loads(response.read())
                return body
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as err:
            last = err
            time.sleep(1)
    raise AssertionError(f"/v1/health never answered within {READY_TIMEOUT_S}s: {last}")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """실제 저장소의 선언을 빌려 쓴다 — 손으로 만든 최소 문서는 스키마와 어긋난다."""
    shutil.copytree(REPO_ROOT / "modules", tmp_path / "modules")
    (tmp_path / "agents" / "custom").mkdir(parents=True)
    manifest = yaml.safe_load((REPO_ROOT / "agents" / "planner" / "manifest.yaml").read_text())
    manifest["metadata"]["name"] = "custom"
    manifest["metadata"]["version"] = "0.1.0"
    manifest["metadata"].pop("group", None)
    (tmp_path / "agents" / "custom" / "manifest.yaml").write_text(
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    for name in ("graphs", "groups"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def builder(workspace: Path) -> ImageBuilder:
    materials = InMemoryMaterialStore()
    materials.put(
        Materials(
            agent="custom",
            version="0.1.0",
            files={"src/agent.py": 'MARK = "baked-from-the-store"\n'},
        )
    )
    return ImageBuilder(
        catalog=Catalog.under(workspace),
        materials=materials,
        builds=InMemoryBuildStore(),
        client=SdkDockerClient(),
        workspace=workspace / "work",
    )


@requires_base
async def test_a_stored_agent_bakes_into_a_running_image(builder, workspace):
    """스토어의 재료만으로 이미지가 구워지고, 그 이미지가 실제로 뜬다."""
    record = await builder.build("custom")
    try:
        assert record.status == BuildStatus.BUILT, record.error
        assert docker("images", "-q", record.image), "이미지가 만들어지지 않았다"

        # 작업 트리에 재료가 남지 않는 것이 이 단계의 존재 이유다
        assert list((workspace / "work").iterdir()) == []

        injected = docker(
            "run",
            "--rm",
            "--read-only",
            "--user",
            "1000:1000",
            "--entrypoint",
            "python",
            record.image,
            "-c",
            "import agent; print(agent.MARK)",
        )
        assert injected == "baked-from-the-store"

        manifest_in_image = docker(
            "run",
            "--rm",
            "--read-only",
            "--user",
            "1000:1000",
            "--entrypoint",
            "cat",
            record.image,
            "/app/manifest.yaml",
        )
        assert yaml.safe_load(manifest_in_image)["metadata"]["name"] == "custom"
    finally:
        docker("rmi", "-f", record.image, check=False)


@requires_base
async def test_the_baked_image_starts_agentd_and_answers_health(builder):
    """이슈 #265 완료 조건 — 구운 이미지가 **기동해** `/v1/health` 를 응답한다.

    재료가 제자리에 들어갔는지만 보면 agentd 가 뜨지 않는 조합도 초록이다. 스켈레톤이
    엔트리포인트나 `PYTHONPATH` 를 망가뜨리면 여기서만 드러난다.
    """
    record = await builder.build("custom")
    assert record.status == BuildStatus.BUILT, record.error
    try:
        with serve(record.image) as port:
            health = wait_for_health(port)

            assert health["status"] in {"healthy", "degraded", "unhealthy"}, health
            assert "checked_at" in health
    finally:
        docker("rmi", "-f", record.image, check=False)


@requires_base
async def test_a_broken_dockerfile_fails_with_its_log(builder, workspace):
    """실패 원인은 로그에만 있다 — 없으면 운영자가 손으로 다시 굽는다."""
    builder.materials.put(
        Materials(
            agent="custom",
            version="0.1.0",
            files={
                "Dockerfile": ("FROM aaestro/agent-base:0.1.0\nCOPY does-not-exist.txt /app/x\n"),
                "src/agent.py": "MARK = 1\n",
            },
        )
    )

    record = await builder.build("custom")

    assert record.status == BuildStatus.FAILED
    assert record.error
    assert list((workspace / "work").iterdir()) == [], "실패해도 임시 디렉토리는 남지 않는다"
    assert not docker("images", "-q", record.image), "실패한 빌드가 태그를 남겼다"
