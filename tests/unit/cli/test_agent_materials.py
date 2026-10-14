"""`agent-push` / `agent-build` — 재료를 스토어에 넣고 굽는 CLI (#266).

저장소에서 `agents/claude-code/src/` 를 지우면 재료를 스토어에 되돌리는 **반복 가능한**
경로가 필요하다. 손으로 JSON 을 만들어 curl 하는 것은 경로가 아니다.

실제 control plane 을 loopback 에 띄운다 — 재료 검증과 빌드 제출이 서버 쪽에 있으므로
대역으로 치환하면 CLI 가 무엇을 보내는지만 보고, 서버가 받아들이는지는 못 본다.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml

from aaestro.authoring import Author
from aaestro.catalog import Catalog
from aaestro.cli import main as cli
from aaestro.cli.main import main, read_materials
from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.materials import InMemoryMaterialStore
from aaestro.orchestrator.control import create_app
from aaestro.orchestrator.runstore import InMemoryRunStore
from aaestro.runtime.images import ImageBuilder, InMemoryBuildStore
from tests.fixtures.fake_docker import FakeDockerClient


def write(path: Path, document: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    write(
        root / "modules" / "promptsets" / "solo" / "0.1.0" / "promptset.yaml",
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
        root / "agents" / "custom" / "manifest.yaml",
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
        (root / name).mkdir()
    return root


@pytest.fixture
def docker() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def served(workspace, docker, monkeypatch):
    """재료 스토어와 빌더가 붙은 control plane — CLI 의 동기 httpx 가 실제로 닿는다."""
    monkeypatch.setattr(cli, "BUILD_POLL_S", 0.02)
    catalog = Catalog.under(workspace)
    author = Author(catalog=catalog, materials=InMemoryMaterialStore())
    builder = ImageBuilder(
        catalog=catalog,
        materials=author.materials,
        builds=InMemoryBuildStore(),
        client=docker,
        workspace=workspace.parent / "work",
    )
    app = create_app(InMemoryRunStore(), catalog=catalog, author=author, builder=builder)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started, "control plane did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield author, f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def materials(tmp_path: Path) -> Path:
    directory = tmp_path / "materials"
    (directory / "src").mkdir(parents=True)
    (directory / "src" / "agent.py").write_text("MARK = 'seeded'\n", encoding="utf-8")
    (directory / "src" / "__pycache__").mkdir()
    (directory / "src" / "__pycache__" / "agent.cpython-312.pyc").write_bytes(b"\x00\xff")
    return directory


def out_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


# --- 디렉토리 → 재료 -------------------------------------------------------------


def test_a_directory_becomes_context_relative_files(materials):
    """캐시는 싣지 않는다 — 재료는 소스다. 안 거르면 바이너리에서 실패한다."""
    assert read_materials(materials) == {"src/agent.py": "MARK = 'seeded'\n"}


def test_a_binary_file_is_refused_with_its_path(materials):
    (materials / "src" / "blob.bin").write_bytes(b"\x00\xff\xfe")

    with pytest.raises(AaestroError) as exc_info:
        read_materials(materials)

    assert exc_info.value.code == ErrorCode.VAL_002
    assert exc_info.value.details == {"path": "src/blob.bin"}


@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_a_symbolic_link_is_refused_instead_of_followed(materials, tmp_path, target_kind):
    """링크를 따라가면 디렉토리 밖의 자격증명이 재료로 올라간다 (CWE-59)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "credentials").write_text("SECRET=1\n", encoding="utf-8")
    target = outside / "credentials" if target_kind == "file" else outside
    (materials / "src" / "linked").symlink_to(target)

    with pytest.raises(AaestroError) as exc_info:
        read_materials(materials)

    assert exc_info.value.code == ErrorCode.VAL_002
    assert exc_info.value.details == {"path": "src/linked"}


# --- CLI 표면 -------------------------------------------------------------------


def test_push_stores_the_materials(served, materials, capsys):
    author, url = served

    code = main(["--json", "agent-push", "custom", str(materials), "--control-url", url])

    assert code == 0, capsys.readouterr()
    assert out_json(capsys) == {"agent": "custom", "version": "0.1.0", "files": ["src/agent.py"]}
    assert dict(author.read_materials("custom").files) == {"src/agent.py": "MARK = 'seeded'\n"}


def test_pushing_the_same_materials_twice_succeeds(served, materials, capsys):
    """시드는 여러 번 돈다 — 두 번째가 실패하면 스크립트가 멈춘다."""
    _, url = served
    argv = ["--json", "agent-push", "custom", str(materials), "--control-url", url]

    assert main(argv) == 0
    assert main(argv) == 0


def test_build_wait_reports_the_baked_image(served, materials, docker, capsys):
    _, url = served
    main(["agent-push", "custom", str(materials), "--control-url", url])
    capsys.readouterr()

    code = main(["--json", "agent-build", "custom", "--wait", "--control-url", url])

    assert code == 0
    body = out_json(capsys)
    assert body["status"] == "built"
    assert body["image"] == "aaestro/agent-custom:0.1.0"
    assert docker.contexts[0]["src/agent.py"] == "MARK = 'seeded'\n"


def test_a_failed_build_exits_nonzero_with_its_error(served, materials, capsys, monkeypatch):
    """배포 게이트가 막기 전에 여기서 알아야 한다 — 0 으로 끝나면 스크립트가 배포로 넘어간다."""
    _, url = served
    main(["agent-push", "custom", str(materials), "--control-url", url])
    capsys.readouterr()
    monkeypatch.setattr(FakeDockerClient, "build", _explode)

    code = main(["--json", "agent-build", "custom", "--wait", "--control-url", url])

    assert code == 1
    body = out_json(capsys)
    assert body["status"] == "failed"
    assert "no space left" in body["error"]


def test_building_without_materials_exits_nonzero(served, capsys):
    _, url = served

    code = main(["--json", "agent-build", "custom", "--control-url", url])

    assert code == 1
    assert out_json(capsys)["status"] == "failed"


def _explode(self, context: str, tag: str, *, buildargs=None) -> str:
    raise RuntimeError("no space left on device")


def test_a_refusal_keeps_the_command_status_and_the_server_code(served, capsys):
    """세부의 HTTP 상태가 `status: failed` 를 덮으면 스크립트가 실패를 못 읽는다.

    코드를 전부 한 값으로 뭉개면 "재료 없음(VAL_002)" 과 "이미 굽는 중(RT_011)" 이 같아
    보인다 — 둘의 조치는 정반대다.
    """
    _, url = served

    main(["--json", "agent-build", "custom", "--control-url", url])

    body = out_json(capsys)
    assert body["status"] == "failed"
    assert body["http_status"] == "400"
    assert body["error_code"] == ErrorCode.VAL_002
