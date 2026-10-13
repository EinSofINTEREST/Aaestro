"""Baking an agent image from stored materials (#265).

조립이 맞는지, 규약 위반을 굽기 전에 잡는지, 그리고 **임시 디렉토리가 남지 않는지**.
마지막이 이 단계의 존재 이유다 — 작업 트리에 재료를 남기지 않으려고 만든 경로다.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
import yaml

from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError, ErrorCategory, ErrorCode
from aaestro.materials import InMemoryMaterialStore, Materials
from aaestro.runtime.docker.client import ImageBuildError
from aaestro.runtime.images import (
    SKELETON_DOCKERFILE,
    BuildRecord,
    BuildStatus,
    ImageBuilder,
    InMemoryBuildStore,
    SqliteBuildStore,
    check_dockerfile,
    image_tag,
)
from tests.fixtures.fake_docker import FakeDockerClient

REPO_ROOT = Path(__file__).resolve().parents[3]
VALID_DOCKERFILE = "FROM aaestro/agent-base:0.1.0\nCOPY manifest.yaml /app/manifest.yaml\n"


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    write(
        tmp_path / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Promptset",
            "metadata": {"name": "solo", "version": "0.1.0"},
            "spec": {
                "engine": "jinja2",
                "templates": {"default": {"file": "t.j2"}, "system": {"file": "t.j2"}},
            },
        },
    )
    write(
        tmp_path / "agents" / "custom" / "manifest.yaml",
        {
            "apiVersion": "aaestro/v1",
            "kind": "Agent",
            "metadata": {"name": "custom", "version": "0.1.0"},
            "spec": {
                "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
                "promptset": {"ref": "promptsets/solo@0.1.0"},
            },
        },
    )
    for name in ("graphs", "groups"):
        (tmp_path / name).mkdir()
    return tmp_path


@pytest.fixture
def docker() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def builder(workspace, docker) -> ImageBuilder:
    materials = InMemoryMaterialStore()
    materials.put(
        Materials(agent="custom", version="0.1.0", files={"src/agent.py": "MARK = 'custom'"})
    )
    return ImageBuilder(
        catalog=Catalog.under(workspace),
        materials=materials,
        builds=InMemoryBuildStore(),
        client=docker,
        workspace=workspace / "work",
    )


# --- Dockerfile 규약 (#263) ----------------------------------------------------------


def test_a_dockerfile_from_the_base_is_accepted():
    check_dockerfile(VALID_DOCKERFILE)


def test_the_skeleton_satisfies_its_own_rules():
    """기본으로 쓰는 것이 규약을 어기면 재료 없는 에이전트가 전부 막힌다."""
    check_dockerfile(SKELETON_DOCKERFILE)


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("COPY x /x\n", "FROM 이 없다"),
        ("FROM python:3.12-slim\n", "base 가 아니다 — agentd 가 없다"),
        ("FROM aaestro/agent-base:0.1.0\nUSER root\n", "root 로 끝난다"),
        ("FROM aaestro/agent-base:0.1.0\nUSER 0\n", "uid 0 으로 끝난다"),
    ],
)
def test_layout_violations_are_refused_before_building(text, why):
    with pytest.raises(AaestroError) as exc_info:
        check_dockerfile(text)

    assert exc_info.value.code == ErrorCode.VAL_002, why


def test_going_root_and_back_is_allowed():
    """설치 동안 root 로 올라가는 것은 흔하다 — 되돌리지 않고 끝나는 것이 문제다."""
    check_dockerfile(
        "FROM aaestro/agent-base:0.1.0\nUSER root\nRUN apt-get update\nUSER 1000:1000\n"
    )


# --- 조립과 빌드 --------------------------------------------------------------------


async def test_a_build_assembles_the_declared_layout(builder, docker):
    record = await builder.build("custom")

    assert record.status == BuildStatus.BUILT
    assert record.image == image_tag("custom", "0.1.0") == "aaestro/agent-custom:0.1.0"
    context = docker.contexts[0]
    assert context["Dockerfile"] == SKELETON_DOCKERFILE
    assert context["src/agent.py"] == "MARK = 'custom'"
    assert yaml.safe_load(context["manifest.yaml"])["metadata"]["name"] == "custom"
    assert "modules/promptsets/solo/0.1.0/promptset.yaml" in context


async def test_the_temp_directory_does_not_survive_the_build(builder, workspace):
    """작업 트리에 재료를 남기지 않는 것이 이 단계의 존재 이유다."""
    await builder.build("custom")

    assert list((workspace / "work").iterdir()) == []


async def test_the_temp_directory_does_not_survive_a_failure(builder, workspace, docker):
    docker._build_error = ImageBuildError("t", "step 2/3 failed", RuntimeError("boom"))  # noqa: SLF001

    record = await builder.build("custom")

    assert record.status == BuildStatus.FAILED
    assert list((workspace / "work").iterdir()) == []


async def test_a_failure_keeps_the_log(builder, docker):
    """실패 원인은 로그에만 있다 — 없으면 운영자가 손으로 다시 굽는다."""
    docker._build_error = ImageBuildError("t", "step 2/3: package not found", RuntimeError("x"))  # noqa: SLF001

    record = await builder.build("custom")

    assert record.status == BuildStatus.FAILED
    assert "package not found" in record.log
    assert record.error
    assert builder.record_of("custom", "0.1.0").status == BuildStatus.FAILED


async def test_a_user_dockerfile_replaces_the_skeleton(builder, docker):
    builder.materials.put(
        Materials(agent="custom", version="0.1.0", files={"Dockerfile": VALID_DOCKERFILE})
    )

    await builder.build("custom")

    assert docker.contexts[0]["Dockerfile"] == VALID_DOCKERFILE


async def test_a_violating_dockerfile_never_reaches_docker(builder, docker):
    builder.materials.put(
        Materials(agent="custom", version="0.1.0", files={"Dockerfile": "FROM alpine\n"})
    )

    with pytest.raises(AaestroError) as exc_info:
        await builder.build("custom")

    assert exc_info.value.code == ErrorCode.VAL_002
    assert docker.built == []


async def test_an_agent_without_materials_is_not_built(builder):
    """declarative agent 는 base 이미지로 돈다 — 굽게 만들면 모듈 시스템의 이점이 사라진다."""
    builder.materials.put(Materials(agent="custom", version="0.1.0", files={}))

    with pytest.raises(AaestroError) as exc_info:
        await builder.build("custom")

    assert exc_info.value.code == ErrorCode.VAL_002
    assert builder.needs_build("custom", "0.1.0") is False


async def test_an_unknown_agent_is_not_found(builder):
    with pytest.raises(AaestroError) as exc_info:
        await builder.build("nobody")

    assert exc_info.value.code == ErrorCode.NF_001


def test_needs_build_follows_the_materials(builder):
    assert builder.needs_build("custom", "0.1.0") is True
    assert builder.needs_build("custom", "9.9.9") is False


# --- 동시 빌드 (PR #269 리뷰) --------------------------------------------------------


class GatedDockerClient(FakeDockerClient):
    """빌드 도중에 멈춰 서는 대역 — 겹친 요청을 만들려면 첫 빌드가 끝나지 않아야 한다."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def build(self, context: str, tag: str, *, buildargs: dict[str, str] | None = None) -> str:
        self.entered.set()
        self.release.wait(timeout=5)
        return super().build(context, tag, buildargs=buildargs)


@pytest.fixture
def gated(workspace) -> ImageBuilder:
    materials = InMemoryMaterialStore()
    materials.put(Materials(agent="custom", version="0.1.0", files={"src/agent.py": "MARK = 1"}))
    return ImageBuilder(
        catalog=Catalog.under(workspace),
        materials=materials,
        builds=InMemoryBuildStore(),
        client=GatedDockerClient(),
        workspace=workspace / "work",
    )


async def test_submitting_a_build_returns_before_it_finishes(gated):
    """제출은 굽기를 기다리지 않는다 — 진행 중 상태를 곧바로 돌려준다."""
    record = await gated.start("custom")

    assert record.status == BuildStatus.BUILDING
    assert gated.record_of("custom", "0.1.0").status == BuildStatus.BUILDING

    gated.client.release.set()
    assert (await gated.running[("custom", "0.1.0")]).status == BuildStatus.BUILT


async def test_a_second_build_of_the_same_version_is_refused(gated):
    """같은 태그를 두 번 구우면 결과가 늦게 끝난 쪽으로 뒤집힌다 — 겹치면 거절한다."""
    await gated.start("custom")
    await asyncio.to_thread(gated.client.entered.wait, 5)

    with pytest.raises(AaestroError) as exc_info:
        await gated.start("custom")

    assert exc_info.value.code == ErrorCode.RT_011
    assert exc_info.value.category == ErrorCategory.RUNTIME

    gated.client.release.set()
    await gated.running[("custom", "0.1.0")]
    assert len(gated.client.built) == 1, "거절된 요청은 Docker 까지 가지 않는다"


async def test_a_build_can_be_repeated_once_the_previous_one_ends(gated):
    """거절은 겹칠 때만이다 — 끝난 뒤에는 다시 구울 수 있어야 재빌드가 가능하다."""
    gated.client.release.set()
    await gated.build("custom")

    assert (await gated.build("custom")).status == BuildStatus.BUILT
    assert len(gated.client.built) == 2


# --- 재시작 지속성 (PR #269 리뷰) ----------------------------------------------------


def test_the_sqlite_store_survives_a_reopen(tmp_path):
    """기록이 프로세스를 넘지 못하면 재시작 후 모든 에이전트가 '빌드된 적 없음' 이 된다."""
    record = BuildRecord(
        agent="custom",
        version="0.1.0",
        status=BuildStatus.BUILT,
        image="aaestro/agent-custom:0.1.0",
        log="Step 1/2",
        updated_at="2026-09-13T00:00:00+00:00",
    )
    SqliteBuildStore(path=tmp_path / "builds.db").upsert(record)

    reopened = SqliteBuildStore(path=tmp_path / "builds.db")

    assert reopened.get("custom", "0.1.0") == record
    assert list(reopened.list()) == [record]


def test_the_sqlite_store_overwrites_an_earlier_result(tmp_path):
    """실패한 뒤 성공하면 마지막 결과가 남아야 한다 — 배포 게이트가 이것을 본다 (#266)."""
    store = SqliteBuildStore(path=tmp_path / "builds.db")
    store.upsert(
        BuildRecord(
            agent="custom", version="0.1.0", status=BuildStatus.FAILED, image="i", error="x"
        )
    )
    store.upsert(BuildRecord(agent="custom", version="0.1.0", status=BuildStatus.BUILT, image="i"))

    found = SqliteBuildStore(path=tmp_path / "builds.db").get("custom", "0.1.0")

    assert found.status == BuildStatus.BUILT
    assert found.error is None
