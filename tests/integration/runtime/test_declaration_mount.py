"""Declaration mounts follow atomic replacement (#275).

배포는 선언을 읽기 전용으로 컨테이너에 건다. 저작 경로는 매니페스트를 임시 파일에 쓰고
rename 으로 교체한다. **파일 하나**를 바인드하면 그 교체가 떠 있는 컨테이너에 보이지 않는다 —
바인드는 inode 에 묶인다. 배포가 실제로 만드는 마운트로 컨테이너를 띄우고, 실제 저작 경로로
교체한 뒤 컨테이너 안에서 새 내용이 보이는지 본다.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.core.manifest import AgentManifest
from aaestro.runtime.deployments import (
    MANIFEST_MOUNT_PATH,
    DeploymentManager,
    InMemoryDeploymentStore,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
IMAGE = "aaestro/agent-base:0.1.0"
FOREIGN_UID = "4242:4242"
"""control plane 과 다른 uid — 같은 uid 면 0600 으로 교체된 파일도 읽혀 회귀를 못 본다."""


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


def image_present() -> bool:
    if shutil.which("docker") is None:
        return False
    return bool(docker("images", "-q", IMAGE, check=False))


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not image_present(), reason=f"{IMAGE} is not built (make build-base)"),
]


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for path in [root, *root.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    return root


@pytest.fixture
def container(workspace: Path) -> Iterator[str]:
    """배포가 계산하는 마운트 그대로 — 손으로 쓴 마운트는 배포와 어긋날 수 있다."""
    catalog = Catalog.under(workspace)
    manager = DeploymentManager(
        catalog=catalog,
        author=Author(catalog=catalog),
        launcher=None,  # type: ignore[arg-type] — 마운트 계산만 쓴다
        store=InMemoryDeploymentStore(),
    )
    flags: list[str] = []
    for mount in manager._mounts("claude-code"):  # noqa: SLF001 — 배포의 실제 마운트 계산
        flags += ["--mount", f"type=bind,src={mount['name']},dst={mount['mount_path']},readonly"]
    name = f"aaestro-mount-{uuid.uuid4().hex[:8]}"
    docker(
        "run",
        "-d",
        "--name",
        name,
        "--read-only",
        "--cap-drop=ALL",
        "--user",
        FOREIGN_UID,
        *flags,
        "--entrypoint",
        "sleep",
        IMAGE,
        "infinity",
    )
    try:
        yield name
    finally:
        docker("rm", "-f", name, check=False)


def test_an_atomically_replaced_manifest_is_visible_in_the_running_container(workspace, container):
    catalog = Catalog.under(workspace)
    before = yaml.safe_load(docker("exec", container, "cat", MANIFEST_MOUNT_PATH))

    # 실제 저작 경로 — 임시 파일에 쓰고 rename 으로 교체한다
    document = catalog.agent("claude-code").model_dump(
        mode="json", by_alias=True, exclude_none=True
    )
    document["metadata"]["version"] = "9.9.9"
    document["metadata"]["description"] = "replaced while the container runs"
    Author(catalog=catalog).save_agent("claude-code", AgentManifest.model_validate(document))

    after = yaml.safe_load(docker("exec", container, "cat", MANIFEST_MOUNT_PATH))

    assert before["metadata"]["version"] != "9.9.9"
    assert after["metadata"]["version"] == "9.9.9"
    assert after["metadata"]["description"] == "replaced while the container runs"
