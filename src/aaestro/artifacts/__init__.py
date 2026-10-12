"""Artifact storage — 대용량 산출물의 정식 경로.

02 Output Discipline 은 대용량 산출물을 output 에 직접 싣지 말고 참조로
전달하라고 규정한다. 그 참조가 가리키는 곳이 여기다.
"""

from aaestro.artifacts.store import (
    ArtifactRef,
    FilesystemArtifactStore,
    parse_ref,
)

__all__ = [
    "ArtifactRef",
    "FilesystemArtifactStore",
    "parse_ref",
]
