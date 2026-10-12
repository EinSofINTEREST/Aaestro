"""Declared memory permissions (#278)."""

from __future__ import annotations

from pathlib import Path

import pytest

from aaestro.access.baselines import MemoryBaseline
from aaestro.access.model import Mode
from aaestro.catalog import Catalog
from tests.fixtures.access import agent, group, write


def with_spaces(document: dict, *aliases: str) -> dict:
    document["spec"]["memory"] = {
        "spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": a} for a in aliases]
    }
    return document


@pytest.fixture
def baseline(tmp_path: Path) -> MemoryBaseline:
    write(
        tmp_path / "agents" / "worker" / "manifest.yaml",
        with_spaces(agent("worker", "research"), "longterm"),
    )
    write(tmp_path / "agents" / "outsider" / "manifest.yaml", agent("outsider"))
    reader = with_spaces(agent("reader"), "notes")
    reader["spec"]["memory"]["spaces"][0]["mode"] = "ro"
    write(tmp_path / "agents" / "reader" / "manifest.yaml", reader)
    write(tmp_path / "agents" / "librarian" / "manifest.yaml", agent("librarian"))
    write(
        tmp_path / "groups" / "research.yaml",
        group(
            "research",
            {
                "memory": {
                    "spaces": [
                        {
                            "ref": "memorysets/domain-knowledge@0.1.0",
                            "as": "knowledge",
                            "mode": "rw",
                        },
                        {"ref": "memorysets/domain-knowledge@0.1.0", "as": "archive", "mode": "ro"},
                    ]
                }
            },
        ),
    )
    write(
        tmp_path / "groups" / "global.yaml",
        group(
            "global",
            {
                "memory": {
                    "spaces": [
                        {"ref": "memorysets/org-facts@0.1.0", "as": "org", "writers": ["librarian"]}
                    ]
                }
            },
        ),
    )
    return MemoryBaseline(Catalog.under(tmp_path))


@pytest.mark.parametrize(
    ("who", "target", "mode", "allowed", "why"),
    [
        ("worker", "local:worker:longterm", Mode.RW, True, "자기 local space"),
        ("worker", "local:worker:diary", Mode.RO, False, "선언하지 않은 local space"),
        ("reader", "local:reader:notes", Mode.RO, True, "ro 로 선언한 local space 읽기"),
        ("reader", "local:reader:notes", Mode.RW, False, "ro 로 선언한 local space 쓰기"),
        ("outsider", "local:worker:longterm", Mode.RO, False, "남의 local space"),
        ("worker", "group:research:knowledge", Mode.RW, True, "그룹 rw space"),
        ("worker", "group:research:archive", Mode.RO, True, "그룹 ro space 읽기"),
        ("worker", "group:research:archive", Mode.RW, False, "그룹 ro space 쓰기"),
        ("outsider", "group:research:knowledge", Mode.RO, False, "비멤버는 읽기도 못 한다"),
        ("outsider", "global:global:org", Mode.RO, True, "전역은 모두 읽는다"),
        ("outsider", "global:global:org", Mode.RW, False, "writers 밖의 전역 쓰기"),
        ("librarian", "global:global:org", Mode.RW, True, "writers 의 전역 쓰기"),
        ("worker", "global:research:org", Mode.RO, False, "global 이 아닌 소유자"),
        ("worker", "run:r-1:scratch", Mode.RO, False, "run space 는 신원만으로 판정하지 않는다"),
        ("worker", "longterm", Mode.RO, False, "별칭은 대상이 아니다"),
        ("ghost", "global:global:org", Mode.RO, False, "없는 에이전트"),
    ],
)
def test_declarations_decide_memory(baseline, who, target, mode, allowed, why):
    assert baseline.allows(who, target, mode) is allowed, why


def test_a_declaration_change_is_read_on_the_next_decision(baseline, tmp_path):
    """재시작 없이 — 그룹 space 를 ro 로 강등하면 다음 판정부터 쓰기가 없다."""
    assert baseline.allows("worker", "group:research:knowledge", Mode.RW)

    write(
        tmp_path / "groups" / "research.yaml",
        group(
            "research",
            {
                "memory": {
                    "spaces": [
                        {
                            "ref": "memorysets/domain-knowledge@0.1.0",
                            "as": "knowledge",
                            "mode": "ro",
                        }
                    ]
                }
            },
        ),
    )

    assert not baseline.allows("worker", "group:research:knowledge", Mode.RW)
    assert baseline.allows("worker", "group:research:knowledge", Mode.RO)


# --- 이그레스 (#293) ------------------------------------------------------------------


def egress_workspace(tmp_path: Path) -> Catalog:
    researcher = agent("researcher")
    researcher["spec"]["runtime"] = {"egress": ["api.search.example.com", "feeds.example.com:8443"]}
    researcher["spec"]["mcp"] = {
        "servers": [
            {
                "name": "corp",
                "transport": "streamable-http",
                "url": "https://mcp.corp.example.com/s",
            },
            {"name": "lab", "transport": "streamable-http", "url": "http://lab.internal:9000/mcp"},
        ]
    }
    write(tmp_path / "agents" / "researcher" / "manifest.yaml", researcher)
    write(tmp_path / "agents" / "quiet" / "manifest.yaml", agent("quiet"))
    return Catalog.under(tmp_path)


@pytest.mark.parametrize(
    ("who", "target", "allowed", "why"),
    [
        ("researcher", "api.search.example.com", True, "매니페스트에 선언한 목적지"),
        ("researcher", "feeds.example.com:8443", True, "포트까지 선언한 목적지"),
        ("researcher", "feeds.example.com", False, "선언한 포트가 아닌 443"),
        ("researcher", "api.anthropic.com", True, "모델 provider 는 선언에서 나온다"),
        ("researcher", "mcp.corp.example.com", True, "external MCP 서버 호스트"),
        ("researcher", "lab.internal:9000", True, "비표준 포트의 MCP 서버"),
        ("researcher", "evil.example.com", False, "선언 밖"),
        ("quiet", "api.search.example.com", False, "남의 선언은 내 권한이 아니다"),
        ("quiet", "api.anthropic.com", True, "모든 anthropic 에이전트는 모델 API 에 닿는다"),
        ("ghost", "api.anthropic.com", False, "없는 에이전트"),
    ],
)
def test_declarations_decide_egress(tmp_path, who, target, allowed, why):
    from aaestro.access.baselines import EgressBaseline

    assert EgressBaseline(egress_workspace(tmp_path)).allows(who, target, None) is allowed, why


def test_the_target_name_drops_only_the_https_port():
    from aaestro.access.baselines import egress_target

    assert egress_target("API.Example.com.", 443) == "api.example.com"
    assert egress_target("api.example.com", 8443) == "api.example.com:8443"


def test_a_broken_mcp_url_that_slipped_past_validation_does_not_break_decisions(tmp_path):
    """선언 검증을 거치지 않고 들어온 값에도 판정이 터지지 않는다 — 그 목적지가 없는 것으로."""
    from aaestro.access.baselines import EgressBaseline
    from aaestro.core.manifest import McpServerSpec

    write(tmp_path / "agents" / "researcher" / "manifest.yaml", agent("researcher"))
    manifest = Catalog.under(tmp_path).agent("researcher")
    broken = McpServerSpec.model_construct(
        name="corp", transport="streamable-http", url="https://x:99999/"
    )
    mcp = manifest.spec.mcp.model_copy(update={"servers": (broken,)})
    manifest = manifest.model_copy(update={"spec": manifest.spec.model_copy(update={"mcp": mcp})})

    declared = EgressBaseline.declared(manifest)

    assert "api.anthropic.com" in declared and not any(t.startswith("x") for t in declared)


# --- 원격 MCP 도구 (#282) ---------------------------------------------------------------


def mcp_workspace(tmp_path: Path) -> Catalog:
    researcher = agent("researcher")
    researcher["spec"]["mcp"] = {
        "servers": [
            {"name": "corp", "transport": "streamable-http", "url": "https://mcp.corp.example/s"},
            {
                "name": "lab",
                "transport": "streamable-http",
                "url": "http://lab.internal:9000/mcp",
                "allowed_tools": ["read"],
            },
            {"name": "fs", "transport": "stdio", "command": ["mcp-server-fs"]},
            {
                "name": "browser",
                "transport": "streamable-http",
                "sidecar": {"image": "mcp/playwright:1.2.0"},
                "allowed_tools": ["navigate"],
            },
        ]
    }
    write(tmp_path / "agents" / "researcher" / "manifest.yaml", researcher)
    write(tmp_path / "agents" / "quiet" / "manifest.yaml", agent("quiet"))
    return Catalog.under(tmp_path)


@pytest.mark.parametrize(
    ("who", "target", "allowed", "why"),
    [
        ("researcher", "corp/search", True, "allowed_tools 없는 원격 서버의 도구"),
        ("researcher", "lab/read", True, "allowed_tools 에 있는 도구"),
        ("researcher", "lab/write", False, "allowed_tools 밖의 도구"),
        ("researcher", "fs/read_file", False, "stdio 서버는 프록시가 보지 않는다"),
        ("researcher", "browser/navigate", True, "사이드카도 프록시가 종단한다 (#304)"),
        ("researcher", "browser/evaluate", False, "사이드카의 allowed_tools 밖"),
        ("researcher", "ghost/search", False, "선언하지 않은 서버"),
        ("researcher", "corp", False, "도구 이름이 없는 대상"),
        ("researcher", "/search", False, "서버 이름이 없는 대상"),
        ("quiet", "corp/search", False, "남의 선언"),
        ("ghost", "corp/search", False, "없는 에이전트"),
    ],
)
def test_declarations_decide_remote_mcp_tools(tmp_path, who, target, allowed, why):
    from aaestro.access.baselines import McpToolBaseline

    assert McpToolBaseline(mcp_workspace(tmp_path)).allows(who, target, None) is allowed, why


@pytest.mark.parametrize(
    ("url", "target"),
    [
        ("https://mcp.corp.example/s", "mcp.corp.example"),
        ("http://lab.internal:9000/mcp", "lab.internal:9000"),
        ("http://lab.internal/mcp", "lab.internal:80"),
        ("https://x:99999/", None),
        ("https:///nohost", None),
    ],
)
def test_url_target_names_a_remote_server_like_the_egress_declaration(url, target):
    from aaestro.access.baselines import url_target

    assert url_target(url) == target


# --- 선언 목록 (#283) — 화면이 보이는 기본 권한은 판정과 같아야 한다 ---------------------------


def test_the_declared_memory_list_is_exactly_what_the_declarations_allow(baseline):
    listed = baseline.declared_for("worker")

    assert listed == [
        ("local:worker:longterm", Mode.RW),
        ("group:research:knowledge", Mode.RW),
        ("group:research:archive", Mode.RO),
        ("global:global:org", Mode.RO),
    ]
    for target, mode in listed:
        assert baseline.allows("worker", target, mode), target
        if mode is Mode.RO:
            assert not baseline.allows("worker", target, Mode.RW), f"{target} 는 쓰기도 된다"
    assert ("global:global:org", Mode.RW) in baseline.declared_for("librarian")
    assert baseline.declared_for("ghost") == []


def test_the_declared_remote_tools_list_the_server_or_its_allowed_tools(tmp_path):
    from aaestro.access.baselines import McpToolBaseline

    declared = McpToolBaseline(mcp_workspace(tmp_path))

    assert declared.declared_for("researcher") == [
        ("corp/*", None),
        ("lab/read", None),
        ("browser/navigate", None),
    ]
    assert declared.allows("researcher", "lab/read", None)
    assert declared.declared_for("quiet") == [] and declared.declared_for("ghost") == []


def test_the_declared_egress_list_is_what_egress_decisions_allow(tmp_path):
    from aaestro.access.baselines import EgressBaseline

    declared = EgressBaseline(egress_workspace(tmp_path))
    listed = declared.declared_for("researcher")

    assert ("feeds.example.com:8443", None) in listed
    assert all(declared.allows("researcher", target, None) for target, _ in listed)
    assert declared.declared_for("ghost") == []


def test_the_declared_callees_are_the_deployed_connections_and_the_stewards():
    from aaestro.access.baselines import A2ABaseline
    from aaestro.access.store import InMemoryAccessStore
    from tests.unit.access.test_tickets import REPO_ROOT

    catalog = Catalog.under(REPO_ROOT)
    store = InMemoryAccessStore()
    declared = A2ABaseline(catalog, store, stewards=frozenset({"permission-agent"}))
    assert declared.declared_for("researcher") == [("permission-agent", None)]

    from aaestro.access.registry import AccessRegistry

    AccessRegistry(store=store, catalog=catalog).issue_identity(
        "researcher", "dep-1", graph="research-pipeline"
    )
    listed = declared.declared_for("researcher")

    assert listed == [("permission-agent", None), ("planner", None)]
    assert all(declared.allows("researcher", callee, None) for callee, _ in listed)
    assert declared.declared_for("permission-agent") == [], "권한 에이전트가 자신을 부른다"


# --- fallback provider 의 이그레스 선언 (#358) ------------------------------------------


def _with_fallback(name: str, provider: str) -> dict:
    document = agent(name)
    document["spec"]["model"]["fallbacks"] = [
        {"provider": provider, "name": "m", "on": ["server_error"]}
    ]
    return document


def test_a_fallback_providers_logical_host_is_declared(tmp_path, monkeypatch):
    """빠지면 전환한 호출을 프록시가 거부한다 — 선언 목록과 판정 둘 다 본다."""
    from aaestro.access import baselines
    from aaestro.access.baselines import EgressBaseline

    monkeypatch.setitem(baselines.PROVIDER_HOSTS, "openai-compat", "api.openai.com")
    write(
        tmp_path / "agents" / "planner" / "manifest.yaml",
        _with_fallback("planner", "openai-compat"),
    )
    write(tmp_path / "agents" / "plain" / "manifest.yaml", agent("plain"))
    catalog = Catalog.under(tmp_path)
    baseline = EgressBaseline(catalog)

    declared = EgressBaseline.declared(catalog.agent("planner"))

    assert {"api.anthropic.com", "api.openai.com"} <= declared
    assert baseline.allows("planner", "api.openai.com", None) is True
    # 남의 fallback 은 내 권한이 아니다
    assert baseline.allows("plain", "api.openai.com", None) is False


def test_an_unknown_fallback_provider_is_silently_left_out_of_the_declaration(tmp_path):
    """논리 호스트를 모르는 provider 는 선언에서 빠질 뿐 판정이 터지지 않는다 (검증은 배포가)."""
    from aaestro.access.baselines import EgressBaseline

    write(tmp_path / "agents" / "planner" / "manifest.yaml", _with_fallback("planner", "mystery"))
    catalog = Catalog.under(tmp_path)

    declared = EgressBaseline.declared(catalog.agent("planner"))

    assert declared == frozenset({"api.anthropic.com"})
