"""Artifact store wiring tests.

계약이 있어도 **주입되지 않으면** skill 은 `ctx.artifacts is None` 을 받는다 —
`executor.py` 의 두 SkillContext 생성 지점이 그랬다 (#139).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aaestro.agentd.executor import Executor, ExecutorServices
from aaestro.artifacts import FilesystemArtifactStore
from tests.fixtures.builders import make_task
from tests.fixtures.fake_model import FakeModel, FakeTools, calls, text

TOOL = "search"


class RecordingTools(FakeTools):
    """skill 이 실제로 받은 컨텍스트를 붙잡는다."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[object] = []

    async def call(self, name, arguments, ctx):  # type: ignore[override]
        self.seen.append(ctx.artifacts)
        return await super().call(name, arguments, ctx)


def executor_with(store, tools):
    return Executor(
        agent="researcher",
        model=FakeModel([calls(TOOL), text("done")]),
        tools=tools,
        render=lambda _task: "prompt",
        services=ExecutorServices(artifacts=store),
    )


async def test_a_skill_receives_the_injected_store(tmp_path):
    """#139 의 핵심 — 주입이 skill 까지 닿아야 한다."""
    store = FilesystemArtifactStore(root=tmp_path, scope="researcher")
    tools = RecordingTools()

    await executor_with(store, tools).execute(make_task())

    assert tools.seen
    assert tools.seen[0] is store


async def test_without_injection_a_skill_still_runs(tmp_path):
    """미주입이 기존 동작을 깨뜨리면 안 된다 — 지금까지의 상태다."""
    tools = RecordingTools()

    result = await executor_with(None, tools).execute(make_task())

    assert tools.seen == [None]
    assert result.status.value == "completed"


async def test_a_skill_can_store_and_reference_an_artifact(tmp_path):
    """02 Output Discipline — 산출물은 참조로 전달된다."""
    store = FilesystemArtifactStore(root=tmp_path, scope="researcher")
    tools = RecordingTools()

    await executor_with(store, tools).execute(make_task())
    received = tools.seen[0]

    ref = await received.put("findings.md", b"# findings")

    assert ref.startswith("artifact://researcher/")
    assert await store.get(ref) == b"# findings"


# --- agentd 조립부 ---------------------------------------------------------------


def test_the_store_is_built_when_a_root_is_injected(tmp_path, monkeypatch):
    from aaestro.agentd.__main__ import ARTIFACT_ROOT_ENV, _artifact_store
    from tests.fixtures.builders import make_manifest

    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path))

    built = _artifact_store(make_manifest())

    # 소속이 스코프를 정한다 — local 은 늘 있고 이름은 에이전트다 (#155)
    from aaestro.artifacts.scope import ArtifactScope, ScopedArtifacts

    assert isinstance(built, ScopedArtifacts)
    assert built.stores[ArtifactScope.LOCAL].scope == "test-agent"


def test_no_store_without_an_injected_root(monkeypatch):
    from aaestro.agentd.__main__ import ARTIFACT_ROOT_ENV, _artifact_store
    from tests.fixtures.builders import make_manifest

    monkeypatch.delenv(ARTIFACT_ROOT_ENV, raising=False)

    assert _artifact_store(make_manifest()) is None


@pytest.fixture
def echo_manifest():
    """실제 manifest — 조립부가 읽는 것과 같은 것을 쓴다.

    파일 I/O 는 동기 fixture 에 둔다: async 테스트 안에서 열면 린터가 막고,
    실제로도 이벤트 루프에서 blocking I/O 를 하는 셈이다.
    """
    import yaml

    from aaestro.core.manifest import AgentManifest

    declared = Path("agents/echo/manifest.yaml").read_text(encoding="utf-8")
    return AgentManifest.model_validate(yaml.safe_load(declared))


async def test_the_assembled_executor_carries_the_store(tmp_path, monkeypatch, echo_manifest):
    """`_artifact_store()` 만 검증하면 **조립부에서 안 넘겨도 통과한다.**

    실제로 그랬다: `artifacts=` 를 지우는 mutation 이 전부 통과했다. 조립된
    executor 를 들여다봐야 배선이 증명된다.
    """
    from aaestro.agentd.__main__ import ARTIFACT_ROOT_ENV, build_executor

    monkeypatch.setenv(ARTIFACT_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv("AAESTRO_EXECUTOR", "")
    # registry 루트는 컨테이너 기준(/app)이 기본이다 — 테스트는 저장소를 가리킨다
    monkeypatch.setenv("AAESTRO_ROOT", ".")

    built = await build_executor(echo_manifest)

    from aaestro.artifacts.scope import ArtifactScope, ScopedArtifacts

    store = built._services.artifacts
    assert isinstance(store, ScopedArtifacts)
    assert store.stores[ArtifactScope.LOCAL].scope == echo_manifest.name
