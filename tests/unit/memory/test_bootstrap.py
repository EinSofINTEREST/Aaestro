"""Memory Service assembly.

`create_app` 은 **이미 조립된** 컴포넌트 넷을 요구한다 — 그것을 설정과 선언
으로 만드는 곳이 없어 서비스를 프로세스로 세울 수 없었다 (#181).

09 Access Enforcement 1 의 배치가 여기서 결정된다: 저장소 자격증명은 이
프로세스만 갖고, 에이전트는 불투명 토큰으로만 닿는다.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from aaestro.config import load_config
from aaestro.memory.bootstrap import build_deployment
from aaestro.memory.entry import MemoryEntry, MemorySource
from aaestro.modules.memoryset import MemoryKind

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def deployment(token_root):
    """저장소 선언 그대로 조립한 서비스 (run scope 그래프 제외)."""
    config = load_config("dev", config_dir=REPO_ROOT / "configs", environ={})
    return build_deployment(config, root=token_root)


def test_every_declared_agent_gets_a_token(deployment):
    """토큰을 못 받은 에이전트는 자기 space 에 닿을 방법이 없다."""
    declared = {path.parent.name for path in (REPO_ROOT / "agents").glob("*/manifest.yaml")}

    assert set(deployment.tokens) == declared


def test_tokens_are_opaque_and_distinct(deployment):
    """페이로드를 넘기면 space 목록과 mode 를 위조할 수 있다 (09)."""
    issued = list(deployment.tokens.values())

    assert len(set(issued)) == len(issued)
    for token in issued:
        assert "researcher" not in token
        assert "longterm" not in token


def test_the_app_refuses_an_unauthenticated_request(deployment):
    """무토큰 접근이 통하면 space 경계가 무의미해진다."""
    with TestClient(deployment.app) as client:
        assert client.get("/v1/spaces").status_code == 401


def test_an_agent_sees_exactly_its_declared_spaces(deployment):
    """선언되지 않은 space 가 보이면 그것은 이미 경계 밖이다."""
    token = deployment.tokens["researcher"]

    with TestClient(deployment.app) as client:
        spaces = client.get("/v1/spaces", headers={"Authorization": f"Bearer {token}"}).json()

    scopes = {entry["alias"]: entry["scope"] for entry in spaces}
    # manifest 의 local, 소속 그룹의 group, groups/global.yaml 의 global
    assert scopes["longterm"] == "local"
    assert scopes["knowledge"] == "group"
    assert scopes["org"] == "global"


def test_a_non_member_does_not_see_the_group_space(deployment):
    """그룹 소속이 곧 접근 경계다 (09 Scope Rules 3)."""
    token = deployment.tokens["echo"]

    with TestClient(deployment.app) as client:
        spaces = client.get("/v1/spaces", headers={"Authorization": f"Bearer {token}"}).json()

    assert "knowledge" not in {entry["alias"] for entry in spaces}


def test_a_global_space_rejects_a_write_from_a_non_writer(deployment):
    """`writers` 미지정이면 전 에이전트 read-only — 전사 지식은 아무나 쓰면 오염된다."""
    token = deployment.tokens["researcher"]
    payload = {
        "space": "org",
        "entry": {
            "space": "org",
            "kind": "fact",
            "content": "오염 시도",
            "source": {"agent": "researcher"},
        },
    }

    with TestClient(deployment.app) as client:
        response = client.post(
            "/v1/append", json=payload, headers={"Authorization": f"Bearer {token}"}
        )

    assert response.status_code == 401


def test_a_forged_token_is_refused(deployment):
    """토큰을 서비스가 기억하므로 지어내도 통하지 않는다."""
    with TestClient(deployment.app) as client:
        response = client.get("/v1/spaces", headers={"Authorization": "Bearer forged"})

    assert response.status_code == 401


# --- 불완전한 카탈로그로 서비스를 띄우지 않는다 (#247 리뷰) ---------------------------


def test_bootstrap_refuses_unreadable_declarations(tmp_path, monkeypatch):
    """깨진 에이전트는 토큰을 못 받고, 깨진 그룹은 멀쩡한 에이전트의 group space 를 지운다."""
    from aaestro.config import load_config
    from aaestro.core.errors import AaestroError, ErrorCode
    from aaestro.memory.bootstrap import build_deployment

    root = tmp_path
    (root / "agents" / "bad").mkdir(parents=True)
    (root / "agents" / "bad" / "manifest.yaml").write_text("kind: Agent\n", encoding="utf-8")
    for d in ("groups", "graphs", "modules"):
        (root / d).mkdir(exist_ok=True)

    with pytest.raises(AaestroError) as excinfo:
        build_deployment(load_config("dev", config_dir="configs"), root=root)

    assert excinfo.value.code == ErrorCode.CFG_001
    assert any(
        p["path"].endswith("agents/bad/manifest.yaml") for p in excinfo.value.details["problems"]
    )


# --- 재시작 뒤 인덱스 warm-up (#312) ---------------------------------------------------


def _memory_on_disk(tmp_path: Path):
    return load_config(
        "dev",
        config_dir=REPO_ROOT / "configs",
        environ={"AAESTRO_MEMORY__PATH": str(tmp_path / "memory.db")},
    )


def _as_researcher(deployment) -> dict[str, str]:
    return {"Authorization": f"Bearer {deployment.tokens['researcher']}"}


async def test_a_restarted_service_finds_what_was_stored_before(tmp_path, token_root):
    """인덱스는 프로세스 메모리다 — 저장소에서 되읽지 않으면 재시작 전 기억이 검색되지 않는다."""
    config = _memory_on_disk(tmp_path)
    fact = MemoryEntry(
        space="longterm",
        kind=MemoryKind.FACT,
        content="sidecar 이미지는 태그를 고정한다",
        source=MemorySource(agent="researcher"),
    )
    first = build_deployment(config, root=token_root)
    # SQLite 연결은 만든 스레드에 묶인다 — 앱을 같은 스레드에서 부른다 (uvicorn 과 같다)
    async with _client(first) as client:
        stored = await client.post(
            "/v1/append",
            headers=_as_researcher(first),
            json={"space": "longterm", "entry": fact.model_dump(mode="json")},
        )
        assert stored.status_code == 200, stored.text

    restarted = build_deployment(config, root=token_root)
    assert restarted.indexer.warming
    restarted.indexer.drain()
    async with _client(restarted) as client:
        response = await client.post(
            "/v1/search",
            headers=_as_researcher(restarted),
            json={"query": "sidecar 이미지 태그", "spaces": ["longterm"]},
        )

    assert [hit["entry"]["content"] for hit in response.json()] == [fact.content]
    assert not restarted.indexer.warming


def _client(deployment) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=deployment.app), base_url="http://m")


def test_an_empty_store_has_nothing_to_warm(tmp_path, token_root):
    deployment = build_deployment(_memory_on_disk(tmp_path), root=token_root)

    assert not deployment.indexer.warming
    assert deployment.indexer.queue.lag == 0


# --- run scope 는 레지스트리 모드 전용이다 (#469, #465 결정 3) -----------------------


GRAPH_WITH_SPACE = """\
apiVersion: aaestro/v1
kind: Graph
metadata: {name: %s, version: 1.0.0}
spec:
  goal: g
  state:
    schema: aaestro.graphs.schemas:ResearchState
  memory:
    spaces:
      - {ref: "memorysets/run-scratch@0.1.0", as: scratch}
  nodes:
    - {id: a, agent: "agents/echo@0.1.0"}
  edges:
    - {from: START, to: a}
    - {from: a, to: END}
"""


def _graph_text(name: str, *, with_space: bool) -> str:
    text = GRAPH_WITH_SPACE % name
    if with_space:
        return text
    return text.replace(
        '  memory:\n    spaces:\n      - {ref: "memorysets/run-scratch@0.1.0", as: scratch}\n', ""
    )


def _config():
    return load_config("dev", config_dir=REPO_ROOT / "configs", environ={})


def test_token_mode_refuses_a_graph_that_declares_run_scope_spaces(token_root):
    """토큰은 run 없이 발급돼 run space 가 조용히 빠진다 — 뜬 뒤에 알면 늦다."""
    from aaestro.core.errors import AaestroError, ErrorCode

    (token_root / "graphs" / "with-space.yaml").write_text(
        _graph_text("with-space", with_space=True), encoding="utf-8"
    )

    with pytest.raises(AaestroError) as excinfo:
        build_deployment(_config(), root=token_root)

    assert excinfo.value.code == ErrorCode.CFG_001
    assert excinfo.value.details["run_scope_spaces"] == [
        {"graph": "with-space", "alias": "scratch"}
    ]


def test_token_mode_accepts_a_graph_without_spaces(token_root):
    """space 없는 그래프까지 거절하면 과잉이다."""
    (token_root / "graphs" / "plain.yaml").write_text(
        _graph_text("plain", with_space=False), encoding="utf-8"
    )

    deployment = build_deployment(_config(), root=token_root)

    assert deployment.tokens


def test_token_mode_refuses_an_unreadable_graph_with_a_relative_location(token_root):
    """읽지 못한 그래프 안에 run scope 가 없다는 것을 증명할 수 없다 — 위치는 상대 경로로만."""
    from aaestro.core.errors import AaestroError, ErrorCode

    (token_root / "graphs" / "broken.yaml").write_text("kind: Graph\n", encoding="utf-8")

    with pytest.raises(AaestroError) as excinfo:
        build_deployment(_config(), root=token_root)

    assert excinfo.value.code == ErrorCode.CFG_001
    assert excinfo.value.details["run_scope_spaces"] == []
    assert excinfo.value.details["unreadable_graphs"] == ["graphs/broken.yaml"]


def test_registry_mode_ignores_run_scope_graphs():
    """run scope 는 레지스트리 모드의 몫이다 — 거부는 토큰 모드에서만."""
    deployment = build_deployment(_config(), root=REPO_ROOT, access=object())  # type: ignore[arg-type]

    assert deployment.tokens == {}


def test_token_mode_refuses_the_repository_graphs():
    """저장소가 싣는 그래프 (run-scratch 부착) 도 토큰 모드에서는 거부된다."""
    from aaestro.core.errors import AaestroError

    with pytest.raises(AaestroError):
        build_deployment(_config(), root=REPO_ROOT)


def test_token_mode_reads_graphs_from_the_configured_root(token_root):
    """control plane 과 같은 선언을 본다 — 기본 graphs/ 가 비어도 설정된 위치를 놓치지 않는다."""
    from aaestro.core.errors import AaestroError, ErrorCode

    configured = token_root / "custom-graphs"
    configured.mkdir()
    (configured / "with-space.yaml").write_text(
        _graph_text("with-space", with_space=True), encoding="utf-8"
    )
    (configured / "broken.yaml").write_text("kind: Graph\n", encoding="utf-8")
    config = _config()
    config = config.model_copy(
        update={
            "registry": config.registry.model_copy(
                update={
                    "roots": config.registry.roots.model_copy(update={"graphs": "custom-graphs"})
                }
            )
        }
    )

    with pytest.raises(AaestroError) as excinfo:
        build_deployment(config, root=token_root)

    assert excinfo.value.code == ErrorCode.CFG_001
    assert excinfo.value.details["run_scope_spaces"] == [
        {"graph": "with-space", "alias": "scratch"}
    ]
    assert excinfo.value.details["unreadable_graphs"] == ["custom-graphs/broken.yaml"]
