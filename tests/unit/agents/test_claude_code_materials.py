"""claude-code 의 시드 재료가 스토어 규칙을 통과하는지 (#266).

재료가 저장소의 `agents/` 를 떠나 스토어로 갔다. 프레임워크는 시드 디렉토리를 읽지 않으므로
규칙이 바뀌어도 아무것도 깨지지 않는다 — 운영자가 올릴 때 처음 알게 된다. 여기서 먼저 안다.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from aaestro.cli.main import read_materials
from aaestro.materials import DOCKERFILE, check_files
from aaestro.runtime.images import check_dockerfile, image_tag

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED = REPO_ROOT / "examples" / "materials" / "claude-code"


def test_the_seed_passes_the_material_rules():
    files = read_materials(SEED)

    assert check_files(files) == files
    assert set(files) == {DOCKERFILE, "src/agent.py"}


def test_the_seed_dockerfile_follows_the_layout():
    """base 에서 시작, non-root 로 끝남, 컨텍스트 밖을 복사하지 않음."""
    check_dockerfile((SEED / DOCKERFILE).read_text(encoding="utf-8"))


def test_the_manifest_names_the_tag_the_build_produces():
    """다르면 배포 게이트가 VAL_002 로 거절한다 — 시드를 올려 구워도 배포되지 않는다."""
    manifest = yaml.safe_load(
        (REPO_ROOT / "agents" / "claude-code" / "manifest.yaml").read_text(encoding="utf-8")
    )

    assert manifest["spec"]["runtime"]["image"] == image_tag(
        manifest["metadata"]["name"], manifest["metadata"]["version"]
    )


def test_no_build_inputs_remain_in_the_declarations():
    """선언 트리와 인프라 이미지 디렉토리에 에이전트 재료가 다시 생기면 두 출처가 된다."""
    assert not (REPO_ROOT / "agents" / "claude-code" / "src").exists()
    assert not (REPO_ROOT / "deployments" / "docker" / "agent-claude-code.Dockerfile").exists()
