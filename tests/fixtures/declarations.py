"""Shared helpers for the declaration-validation tests (#386).

선언 검증 테스트가 함께 쓰는 헬퍼 — 저장소 선언 수집, 저장소 사본, 편집."""

from __future__ import annotations

import copy
import shutil
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.core.topology import GraphTopology
from aaestro.modules.memoryset import MemorysetManifest
from aaestro.modules.promptset import PromptsetManifest
from aaestro.modules.skillpack import SkillpackManifest
from aaestro.modules.skillset import SkillsetManifest
from tests.fixtures.builders import manifest_dict

REPO_ROOT = Path(__file__).resolve().parents[2]
MARKER = "sk-SECRET-123-marker"


def _with(doc: dict[str, Any], path: list[str | int], key: str, value: Any) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    node: Any = out
    for step in path:
        node = node[step]
    node[key] = value
    return out


@cache
def _declaration_documents() -> dict[Path, dict[str, Any]]:
    """저장소의 선언 yaml 전부 — 읽지 못하는 파일은 조용히 건너뛰지 않고 실패한다."""
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z", "*.yaml", "*.yml"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    documents: dict[Path, dict[str, Any]] = {}
    for name in out.split("\0"):
        if not name:
            continue
        path = REPO_ROOT / name
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(doc, dict) and "apiVersion" in doc and "kind" in doc:
            documents[path] = doc
    return documents


def _declaration_files() -> list[Path]:
    return list(_declaration_documents())


KIND_MODELS: dict[str, type[BaseModel]] = {
    "Agent": AgentManifest,
    "Group": GroupManifest,
    "Graph": GraphTopology,
    "Skillset": SkillsetManifest,
    "Promptset": PromptsetManifest,
    "Memoryset": MemorysetManifest,
    "Skillpack": SkillpackManifest,
}


def _first_doc(kind: str) -> dict[str, Any]:
    for doc in _declaration_documents().values():
        if doc["kind"] == kind:
            # 캐시된 문서를 돌려주면 호출자의 변조가 다른 테스트로 새어 순서 의존이 된다
            return copy.deepcopy(doc)
    raise AssertionError(f"no {kind} declaration in the repository")


def _repo_copy(tmp_path: Path) -> Path:
    for name in ("agents", "graphs", "groups", "modules"):
        shutil.copytree(REPO_ROOT / name, tmp_path / name)
    return tmp_path


def _edit(path: Path, mutate: Any) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    mutate(doc)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")


def _secret_manifest(tmp_path: Path) -> Path:
    path = tmp_path / "manifest.yaml"
    doc = manifest_dict(spec={"runtime": {"env": {"API_KEY": MARKER}}})
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return path
