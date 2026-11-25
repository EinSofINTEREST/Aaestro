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


# --- 지시문 해석 우회 (#690) ---------------------------------------------------------

_BASE = "FROM aaestro/agent-base:0.1.0\n"


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (_BASE + "ADD --chown=1000 \\\n    https://MARKER.example/x /app/x\n", 2),
        (
            _BASE + "RUN a\nADD --chown=1000 \\\n  # c\n\n  --chmod=644 \\\n  http://MARKER/x /x\n",
            3,
        ),
        (_BASE + 'ADD ["https://MARKER.example/x", "/app/x"]\n', 2),
        (_BASE + 'COPY --chown=1 ["git://MARKER/x", "/app/x"]\n', 2),
        (_BASE + 'ADD ["\\u0068ttps://MARKER.example/x", "/app/x"]\n', 2),
        (_BASE + 'COPY ["/MARKER", "/app/x"]\n', 2),
        (_BASE + "add https://MARKER/x /x\n", 2),
        (_BASE + "USER root:root\n", 2),
        (_BASE + "USER root:0\n", 2),
        (_BASE + "USER 0:root\n", 2),
        (_BASE + "USER 0:1000\n", 2),
        (_BASE + "USER ROOT\n", 2),
        (_BASE + "USER \\\n  root\n", 2),
        ("# escape=`\n" + _BASE, 1),
        ("# check=skip=x\n# Escape = `\n" + _BASE, 2),
    ],
)
def test_instruction_bypasses_are_refused_with_the_start_line(text, line):
    with pytest.raises(AaestroError) as exc_info:
        check_dockerfile(text)

    err = exc_info.value
    assert err.code == ErrorCode.VAL_002
    assert err.details == {"line": line}
    assert "marker" not in (err.message + str(err.details)).lower()


@pytest.mark.parametrize(
    "text",
    [
        _BASE + "USER agent:0\n",
        _BASE + "USER 1000:0\n",
        _BASE + "COPY --chown=1000:1000 \\\n  src/ \\\n  /app/src/\n",
        _BASE + 'COPY ["src/", "/app/src/"]\n',
        _BASE + "# ADD https://x/y /z\nRUN echo hi # USER root\n",
        "# check=skip=JSONArgsRecommended\n" + _BASE,
        "FROM aaestro/agent-base:0.1.0@sha256:" + "a" * 64 + "\n",
        "FROM aaestro/agent-base@sha256:" + "b" * 64 + "\n",
        "ARG T=1\nFROM aaestro/agent-base:${T}@sha256:" + "c" * 64 + "\n",
        _BASE.replace("\n", "\r\n") + "COPY src/ /app/src/\r\n",
    ],
)
def test_legitimate_forms_still_pass(text):
    check_dockerfile(text)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (_BASE + "FROM python:3.12\n", 2),
        ("FROM python:3.12 AS build\n" + _BASE.replace("\n", " AS a\n") + "FROM build\n", 3),
        ("FROM $MARKER\n", 1),
        (_BASE + "ARG U\nUSER $MARKER\n", 3),
        (_BASE + "USER root AS x\nUSER 1000\nUSER $MARKER\n", 4),
        (_BASE.replace("\n", " AS a\n") + "USER root\nFROM a\n", 2),
        ("FROM python:3.12 AS b\n" + _BASE + "COPY --from=MARKER/image:1 /x /y\n", 3),
        ("FROM python:3.12 AS b\n" + _BASE + "ADD --from=$MARKER /x /y\n", 3),
        ("FROM python:3.12 AS b\n" + _BASE + "COPY --from=1 /x /y\n", 3),
        (
            "FROM python:3.12 AS b\nCOPY --from=later /x /y\n" + _BASE.replace("\n", " AS later\n"),
            2,
        ),
        (_BASE.replace("\n", " AS a\n") + "COPY --from=a /x /y\n", 2),
    ],
)
def test_multi_stage_and_variable_bypasses_are_refused(text, line):
    with pytest.raises(AaestroError) as exc_info:
        check_dockerfile(text)

    err = exc_info.value
    assert err.code == ErrorCode.VAL_002
    assert err.details == {"line": line}
    assert "marker" not in (err.message + str(err.details)).lower()


@pytest.mark.parametrize(
    "text",
    [
        "FROM python:3.12 AS build\nUSER root\nRUN x\n" + _BASE + "COPY --from=build /o /o\n",
        "FROM python:3.12 AS build\n" + _BASE.replace("\n", " AS a\n") + "FROM a AS b\nFROM b\n",
        _BASE.replace("\n", " AS a\n") + "USER root\nUSER 1000\nFROM a\n",
        "FROM aaestro/agent-base:0.1.0 AS a\nUSER root\n" + _BASE,
        "ARG T=1\nFROM aaestro/agent-base:${T}\n",
        "FROM python:3.12 AS b\n" + _BASE + "COPY --from=0 /o /o\n",
        "FROM python:3.12 AS B\n" + _BASE + "COPY --from=b --chown=1 /o /o\n",
    ],
)
def test_builder_stages_and_alias_chains_pass(text):
    check_dockerfile(text)


@pytest.mark.parametrize(
    ("text", "line"),
    [
        (_BASE + "USER ro\\\not\n", 2),  # 구분자 없는 이어 쓰기 = USER root (docker build 로 확인)
        (_BASE + "USER\troot\n", 2),  # 탭 구분 (docker build 로 확인)
        (_BASE + "USER\froot\n", 2),
        (_BASE + 'USER r"oo"t\n', 2),
        (_BASE + "USER r\\oot\n", 2),
        (_BASE + "USER +0\n", 2),
        (_BASE + "USER $MARKER\n", 2),
        ("\ufeff# escape=`\n" + _BASE + "RUN echo \\\nUSER root\n", 1),
        (_BASE + "FROM\tMARKER/image:1\n", 2),
        (_BASE + "FR\\\nOM MARKER/image:1\n", 2),
        ("FROM MARKER/image:1\nRUN <<EOF\nFROM aaestro/agent-base:0.1.0\nEOF\n", 2),
        (_BASE + "USER root\nRUN <<EOF\nUSER 1000\nEOF\n", 3),
        ("# syntax=MARKER/frontend:1\n" + _BASE, 1),
        (_BASE + "ADD\thttps://MARKER.example/x /app/x\n", 2),
        (_BASE + "AD\\\nD https://MARKER.example/x /app/x\n", 2),
        (_BASE + 'ADD h"ttps"://MARKER.example/x /app/x\n', 2),
        (_BASE + "ADD ht\\tps://MARKER.example/x /app/x\n", 2),
        (_BASE + "ARG U=https://MARKER.example/x\nADD $U /app/x\n", 3),
        (_BASE + "ADD ${U:-https://MARKER.example/x} /app/x\n", 2),
        (_BASE + "ADD ssh://git@MARKER.com/evil/x.git /app/x\n", 2),
        (_BASE + "ADD git@MARKER.com:evil/x.git /app/x\n", 2),
        (_BASE + "ADD ./../MARKER /app/x\n", 2),
        (_BASE + "ADD '\"/MARKER\"' /x\n", 2),
        (_BASE + 'COPY ["\\u002e\\u002e/MARKER", "/x"]\n', 2),
        (_BASE + "COPY src/../../MARKER /x\n", 2),
        ("FROM python:3.12 AS b\n" + _BASE + 'COPY --from="b" /x /y\n', 3),
        ("FROM python:3.12 AS b\n" + _BASE + "COPY --from=$MARKER /x /y\n", 3),
        ('FROM python:3.12 AS "b"\n' + _BASE + 'COPY --from="b" /x /y\n', 3),
        ("ARG X\nFROM aaestro/agent-base:0.1.0$MARKER\n", 2),
        ('FROM "aaestro/agent-base:0.1.0"\n', 1),
    ],
)
def test_ambiguous_notations_fail_closed(text, line):
    with pytest.raises(AaestroError) as exc_info:
        check_dockerfile(text)

    err = exc_info.value
    assert err.code == ErrorCode.VAL_002
    assert err.details == {"line": line}
    assert "marker" not in (err.message + str(err.details)).lower()


def test_the_shipped_material_dockerfiles_still_pass():
    for path in Path("examples/materials").glob("*/Dockerfile"):
        check_dockerfile(path.read_text(encoding="utf-8"))


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


# --- 값 없는 거절 (#412) -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("FROM MARKER/image:1\r\nRUN a \\\r\n  b \\\r\nFROM aaestro/agent-base:0.1.0\r\n", 1),
        (_BASE.replace("\n", "\r\n") + "USER root\r\nRUN a \\\r\n  b \\\r\nUSER 1000\r\n", 2),
        ("FROM MARKER/image:1\nRUN a \\\n  b \\\r\nFROM aaestro/agent-base:0.1.0\n", 1),
        (_BASE + "\u00a0USER root\n", 2),
        (_BASE + "\u0085USER root\n", 2),
        (_BASE + "\u2028USER root\n", 2),
        (_BASE + "\u3000FROM MARKER/image:1\n", 2),
        ("FROM MARKER/image:1\nRUN true \\\n\u00a0\nFROM aaestro/agent-base:0.1.0\n", 3),
        (_BASE + "USER root\nRUN true \\\n\u00a0# c\nUSER 1000\n", 4),
        (_BASE + "USER -0\n", 2),
        (_BASE + "USER +0\n", 2),
        (_BASE + "USER agent:-0\n", 2),
        ("\n# syntax=MARKER/frontend:1\n" + _BASE, 2),
        ("# check=skip=all\n# Syntax = MARKER/f:1\n" + _BASE, 2),
        (_BASE + "# escape=`\nRUN x\n", 2),
        ("//syntax=MARKER/f:1\n" + _BASE, 1),
        ('{"syntax": "MARKER/f:1"}\n' + _BASE, 1),
        (_BASE + '{"Syntax":"MARKER"}\n', 2),
        (_BASE + "RUN true \\\n# syntax=MARKER\n  && true\n", 3),
        ("FROM aaestro/agent-base:0.1.0@sha256:" + "a" * 63 + "\n", 1),
        ("FROM aaestro/agent-base:0.1.0@sha256:" + "G" * 64 + "\n", 1),
    ],
)
def test_second_round_bypasses_are_refused(text, line):
    with pytest.raises(AaestroError) as exc_info:
        check_dockerfile(text)

    err = exc_info.value
    assert err.code == ErrorCode.VAL_002
    assert err.details == {"line": line}
    assert "marker" not in (err.message + str(err.details)).lower()


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("FROM MARKER-image:1\n", 1),
        ("# FROM MARKER-comment\nFROM MARKER-image:1\n", 2),
        ("FROM aaestro/agent-base:0.1.0\nRUN echo hi\nUSER root\n", 3),
        ("FROM aaestro/agent-base:0.1.0\nUSER marker-user\nUSER root\n", 3),
        ("FROM aaestro/agent-base:0.1.0\nRUN mkdir /root/x\nUSER 0\n", 3),
        ("FROM aaestro/agent-base:0.1.0\nRUN echo \x0c\nUSER root\n", 3),
        ("FROM aaestro/agent-base:0.1.0\nRUN x\nADD https://MARKER.example/x /x\n", 3),
        ("FROM aaestro/agent-base:0.1.0\nCOPY a /a\nCOPY /MARKER-path /x\n", 3),
    ],
)
def test_refusals_carry_the_exact_line_not_the_offending_fragment(text, line):
    with pytest.raises(AaestroError) as exc_info:
        check_dockerfile(text)

    err = exc_info.value
    assert "marker" not in (err.message + str(err.details)).lower()
    assert err.details["line"] == line
