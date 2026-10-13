"""Fuzz material for "a rejected input never comes back in the error" (#386).

"거절된 입력 값은 에러로 되돌아오지 않는다" 를 성질로 검사하기 위한 재료 — 모든 선언 · 설정
문서의 모든 잎 위치에 표식 변형을 넣어 검증을 실패시키고, 나온 것에서 표식의 조각을 찾는다.

저장소의 선언만 쓰면 저장소가 우연히 쓰지 않는 필드 (external MCP 의 ``url``, 모델 fallback,
이그레스 프록시 설정) 를 놓친다. 그래서 스키마의 선택 필드를 채운 문서를 함께 둔다.
"""

from __future__ import annotations

import copy
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import yaml
from pydantic import BaseModel

from aaestro.config import AaestroConfig
from aaestro.core.manifest import AgentManifest, GroupManifest
from aaestro.orchestrator.topology import GraphTopology
from tests.fixtures.declarations import KIND_MODELS, MARKER, REPO_ROOT, _declaration_documents

MIN_FRAGMENT = 8
"""이 길이 이상의 표식 조각이 나오면 누출이다 — ``field_problems`` 가 가리는 단위와 같다."""

RICH_AGENT: dict[str, Any] = {
    "apiVersion": "aaestro/v1",
    "kind": "Agent",
    "metadata": {
        "name": "rich-agent",
        "version": "0.3.0",
        "group": "research",
        "description": "every optional field filled in",
    },
    "spec": {
        "model": {
            "provider": "anthropic",
            "name": "claude-sonnet-5",
            "max_tokens": 4096,
            "effort": "high",
            "fallbacks": [
                {"provider": "anthropic", "name": "claude-haiku-4-5", "on": ["rate_limit"]}
            ],
            "retry": {
                "rate_limit": {"start_s": 10, "attempts": 5, "max_s": 120},
                "network": {"start_s": 1, "attempts": 3, "max_s": 30},
            },
            "circuit": {"max_failures": 5, "reset_s": 60},
        },
        "promptset": {"ref": "promptsets/researcher@0.1.0"},
        "skillsets": [{"ref": "skillsets/web-search@0.2.0"}],
        "memory": {
            "spaces": [{"ref": "memorysets/agent-longterm@0.1.0", "as": "longterm", "mode": "rw"}]
        },
        "mcp": {
            "servers": [
                {
                    "name": "files",
                    "transport": "stdio",
                    "command": ["mcp-server-filesystem", "/workspace"],
                    "allowed_tools": ["read_file"],
                    "env_allowlist": ["FILES_ROOT_HINT"],
                    "optional": True,
                },
                {
                    "name": "browser",
                    "transport": "streamable-http",
                    "sidecar": {
                        "image": "mcp/playwright:1.2.0",
                        "resources": {"cpu": "0.5", "memory": "512Mi"},
                        "port": 8000,
                        "path": "/mcp",
                    },
                },
                {
                    "name": "corp-search",
                    "transport": "streamable-http",
                    "url": "https://mcp.internal.example.com/search",
                    "auth": {"type": "bearer", "token_env": "CORP_SEARCH_TOKEN"},
                },
            ]
        },
        "a2a": {"enabled": True, "capabilities": {"streaming": True, "push_notifications": False}},
        "runtime": {
            "image": "aaestro/agent-base:0.1.0",
            "resources": {"cpu": "1.0", "memory": "1Gi"},
            "env_allowlist": ["ANTHROPIC_API_KEY", "FILES_ROOT_HINT", "CORP_SEARCH_TOKEN"],
            "volumes": [{"name": "work", "mount_path": "/workspace", "read_only": False}],
            "replicas": 2,
            "max_concurrent_tasks": 4,
            "egress": ["api.example.com", "files.example.com:8443"],
        },
        "entrypoint": "agent:ResearchAgent",
    },
}

RICH_GROUP: dict[str, Any] = {
    "apiVersion": "aaestro/v1",
    "kind": "Group",
    "metadata": {"name": "rich-group", "version": "0.1.0", "description": "all scoped resources"},
    "spec": {
        "quotas": {"cpu": "8.0", "memory": "16Gi", "max_agents": 10},
        "secrets": ["SEARCH_API_KEY"],
        "memory": {
            "spaces": [
                {
                    "ref": "memorysets/domain-knowledge@0.1.0",
                    "as": "knowledge",
                    "mode": "rw",
                    "writers": ["librarian"],
                }
            ]
        },
        "artifacts": {"quota": "50Gi"},
        "access": {
            "ceiling": {
                "max_ttl_s": 3600,
                "memory": [{"space": "group:rich-group:knowledge", "mode": "ro"}],
                "egress": ["api.search.example.com"],
                "mcp_tool": ["corp-search/query"],
                "a2a": ["planner"],
            }
        },
    },
}

RICH_GRAPH: dict[str, Any] = {
    "apiVersion": "aaestro/v1",
    "kind": "Graph",
    "metadata": {"name": "rich-graph", "version": "1.0.0", "description": "service mode, inline"},
    "spec": {
        "mode": "service",
        "goal": "exercise every optional graph field",
        "state": {
            "fields": {
                "query": {"type": "string", "required": True, "description": "what to look for"},
                "busy": {"type": "boolean", "default": False},
                "seen_ids": {"type": "array"},
                "first_seen": {"type": "array"},
                "tags": {"type": "array"},
                "notes": {"type": "array"},
            },
            "checkpointer": "default",
            # 네 전략 전부 — fuzz 가 압축 정책 모델의 모든 필드를 지난다 (#371)
            "compaction": {
                "seen_ids": {"strategy": "keep_last", "n": 500},
                "first_seen": {"strategy": "keep_first", "n": 10},
                "tags": {"strategy": "dedupe"},
                "notes": {
                    "strategy": "summarize",
                    "over_tokens": 4000,
                    "keep_last": 3,
                    "compactor": "agents/rich-agent@0.3.0",
                    "template": "summarize_state",
                },
            },
        },
        "nodes": [
            {
                "id": "watcher",
                "agent": "agents/rich-agent@0.3.0",
                "input_map": {"query": "state.query"},
                "output_map": {"busy": "output.busy"},
                "retry": 1,
                "timeout_s": 30,
                "model": {
                    "provider": "anthropic",
                    "name": "claude-haiku-4-5",
                    "max_tokens": 1024,
                    "effort": "low",
                    "fallbacks": [
                        {"provider": "anthropic", "name": "claude-sonnet-5", "on": ["server_error"]}
                    ],
                    "retry": {"network": {"start_s": 1, "attempts": 2, "max_s": 5}},
                    "circuit": {"max_failures": 3, "reset_s": 30},
                },
            },
            {"id": "sub", "graph": "graphs/sub-review@1.0.0"},
        ],
        "edges": [
            {"from": "START", "to": "watcher"},
            {"from": "watcher", "to": "sub", "condition": "state.busy", "max_iterations": 3},
            {"from": "watcher", "to": "END", "condition": "not state.busy"},
            {"from": "sub", "to": "END"},
        ],
        "connections": [{"caller": "watcher", "callee": "sub"}],
        "service": {
            "idle": {"min_delay_s": 30, "max_delay_s": 600, "multiplier": 2.0},
            "max_failure_streak": 5,
        },
        "memory": {"spaces": [{"ref": "memorysets/run-scratch@0.1.0", "as": "scratch"}]},
    },
}

RICH_CONFIG: dict[str, Any] = {
    "runtime": {
        "backend": "docker",
        "network": "aaestro-net",
        "agent_base_image": "aaestro/agent-base:0.1.0",
        "default_resources": {"cpu": "1.0", "memory": "1Gi"},
        "health_check": {
            "interval_s": 10,
            "timeout_s": 3,
            "unhealthy_threshold": 3,
            "startup_grace_s": 45,
        },
        "egress_proxy": {
            "connect_url": "http://egress:8080",
            "providers_url": "http://egress:8081",
            "container": "aaestro-egress",
            "providers": ["anthropic"],
        },
        "agent_env": {"REGION_HINT": "ap-northeast-2"},
    },
    "orchestrator": {
        "checkpointer": "redis",
        "checkpointer_url": "redis://checkpoints:6379/0",
        "state_artifact_root": "/var/lib/aaestro/state-artifacts",
        "run_store": "/var/lib/aaestro/runs.db",
        "deployment_ready_timeout_s": 60,
        "control_host": "127.0.0.1",
        "control_port": 8090,
        "build_store": "/var/lib/aaestro/builds.db",
        "material_store": "/var/lib/aaestro/materials.db",
        "deployment_store": "/var/lib/aaestro/deployments.db",
        "control_token": "operator-token-value",
        "access_store": "/var/lib/aaestro/access.db",
        "access_enforcer_token": "enforcer-token-value",
        "access_agent_url": "http://control-plane:8090",
        "access_stewards": ["steward"],
        "max_concurrent_runs": 10,
        "max_service_runs": 5,
        "node_timeout_s": 300,
        "service_defaults": {
            "idle_min_delay_s": 30,
            "idle_max_delay_s": 600,
            "max_failure_streak": 5,
        },
    },
    "protocols": {"a2a": {"port_range": [9100, 9199]}, "mcp": {"startup_timeout_s": 15}},
    "registry": {
        "backend": "filesystem",
        "roots": {
            "skillsets": "./modules/skillsets",
            "promptsets": "./modules/promptsets",
            "memorysets": "./modules/memorysets",
            "agents": "./agents",
            "graphs": "./graphs",
            "groups": "./groups",
        },
    },
    "memory": {
        "backend": "sqlite",
        "index_lag_target_s": 5,
        "run_scope_retention_days": 30,
        "path": "/var/lib/aaestro/memory.db",
        "dsn": "postgresql://memory@db/aaestro",
    },
    "observability": {"log_level": "INFO", "log_format": "json", "metrics_port": 9090},
}

RICH_DOCUMENTS: tuple[tuple[str, type[BaseModel], dict[str, Any]], ...] = (
    ("rich:agent", AgentManifest, RICH_AGENT),
    ("rich:group", GroupManifest, RICH_GROUP),
    ("rich:graph", GraphTopology, RICH_GRAPH),
    ("rich:config", AaestroConfig, RICH_CONFIG),
)


@dataclass(frozen=True)
class Mark:
    """One way a secret can sit in a leaf, and the pieces of it that must never come back."""

    name: str
    value: Any
    cores: tuple[str, ...]
    """비밀에 해당하는 부분 — 값 전체가 아니라 이것의 조각을 찾는다. URL 변형에서 스킴 · 호스트는
    고정 문구 (``http://host:port``) 와 우연히 겹칠 수 있고, 비밀이 아니다."""

    def fragments(self) -> frozenset[str]:
        """표식의 모든 :data:`MIN_FRAGMENT` 자 조각 — 정규화한 꼴 (호환 분해 · 대소문자 접기) 로."""
        return frozenset(
            folded[start : start + MIN_FRAGMENT]
            for folded in map(normalized, self.cores)
            for start in range(len(folded) - MIN_FRAGMENT + 1)
        )

    def leaked_in(self, text: str) -> str | None:
        """``text`` 에 나타난 표식 조각 하나 — 없으면 ``None``.

        정규화한 꼴끼리 본다: validator 가 값을 소문자로, 또는 NFKC 로 (전각 → ASCII) 바꿔
        되비춰도 같은 값이 나간 것이다.
        """
        folded = normalized(text)
        return next((piece for piece in self.fragments() if piece in folded), None)


def normalized(text: str) -> str:
    """호환 분해 + 대소문자 접기 — 표기만 다른 같은 값을 한 꼴로."""
    return unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", text).casefold())


def _mark(name: str, value: Any, *cores: str) -> Mark:
    return Mark(name=name, value=value, cores=cores or (MARKER,))


IDENTIFIER = MARKER.replace("-", "_")
FULLWIDTH = {code: code + 0xFEE0 for code in range(0x21, 0x7F)}
"""ASCII → 전각 (U+FF01…) — NFKC 로 되돌아온다."""
LONG_REPEATS = 500
BIG_NUMBER = 918273645

MARKS: tuple[Mark, ...] = (
    _mark("plain", MARKER),
    # 이름 규칙 (소문자 · 숫자 · 하이픈) 을 통과한다 — 스키마가 걸러 주지 않는 자리에서 본다
    _mark("lower", MARKER.lower()),
    _mark("upper", MARKER.upper()),
    _mark("quotes", "sk-SEC'RET\"123-marker", "sk-SEC'RET\"123-marker", "123-marker"),
    _mark("backslash", "sk-SECRET\\123\\marker-x", "sk-SECRET", "marker-x"),
    _mark("newline", "sk-SECRET\n123-marker", "sk-SECRET", "123-marker"),
    _mark("unicode", "sk-SÉCRET-키-123-marker", "sk-SÉCRET-키-123-marker"),
    _mark("control", "sk-SECRET\x1b[31m-123-marker\x07", "sk-SECRET", "-123-marker"),
    # 반복 경계 (``…markersk-…``) 를 가로지르는 조각도 본다
    _mark("long", MARKER * LONG_REPEATS, MARKER * 2),
    _mark("padded", f"  {MARKER}  "),
    # urlsplit 이 NFKC 정규화에서 거절하며 netloc 을 메시지에 싣는다
    _mark("nfkc-url", f"https://user:{MARKER}℀@host.example/mcp", f"{MARKER}℀"),
    # ipaddress 가 대괄호 안을 메시지에 싣는다
    _mark("bracket-url", f"https://[{MARKER}]/mcp"),
    _mark("userinfo-url", f"https://user:{MARKER}@host.example:99999/p?q={MARKER}"),
    _mark("ref", f"skillsets/{MARKER}@latest"),
    _mark("list", [MARKER]),
    _mark("dict", {"k": MARKER}),
    _mark("number", BIG_NUMBER, str(BIG_NUMBER)),
    # 파이썬 식별자 모양 — 조건식에 넣으면 이름으로 파싱된다 (하이픈이 든 표식은 뺄셈이 된다)
    _mark("identifier", IDENTIFIER, IDENTIFIER),
    # 전각 — 파이썬은 식별자를 NFKC 로 정규화한다: 메시지에는 ASCII 로 나와 입력과 글자가 다르다
    _mark("fullwidth", IDENTIFIER.translate(FULLWIDTH), IDENTIFIER),
    # 접으면 길이가 바뀌는 글자 (İ → i̇, ß → ss) — 위치 대응이 어긋나 접기가 꺼지지 않는지
    _mark("folding", f"İ{MARKER}ß{MARKER}", MARKER),
)


def fuzz_documents() -> list[tuple[str, type[BaseModel], dict[str, Any]]]:
    """Every declaration and settings document to mutate: the repository's and the rich ones."""
    documents = [
        (str(path.relative_to(REPO_ROOT)), KIND_MODELS[doc["kind"]], doc)
        for path, doc in _declaration_documents().items()
    ]
    for path in sorted((REPO_ROOT / "configs").glob("*.yaml")):
        documents.append(
            (
                str(path.relative_to(REPO_ROOT)),
                AaestroConfig,
                yaml.safe_load(path.read_text(encoding="utf-8")),
            )
        )
    documents.extend(RICH_DOCUMENTS)
    return documents


Position = tuple[str | int, ...]


def leaf_positions(node: Any, path: Position = ()) -> Iterator[Position]:
    """Every position in the document — containers too, since a secret can replace a subtree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield (*path, key)
            yield from leaf_positions(value, (*path, key))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield (*path, index)
            yield from leaf_positions(value, (*path, index))


def shape_of(path: Position) -> tuple[str, ...]:
    """같은 모양의 위치 (리스트의 몇 번째인지만 다른 것) 는 한 번만 본다."""
    return tuple("#" if isinstance(step, int) else step for step in path)


def with_value(document: dict[str, Any], path: Position, value: Any) -> dict[str, Any]:
    """A copy of the document with ``value`` at ``path``."""
    mutated = copy.deepcopy(document)
    node: Any = mutated
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = copy.deepcopy(value)
    return mutated


def with_twin(document: dict[str, Any], path: Position, value: Any) -> dict[str, Any] | None:
    """A copy where the list item at ``path`` carries ``value`` and appears twice.

    "중복" 을 거절하는 validator (서버 이름 · 노드 id · 별칭 · fallback) 는 값 하나를 바꿔서는
    닿지 않는다 — 표식을 넣은 항목을 한 번 더 붙여 닿게 한다. ``path`` 가 리스트 항목이거나
    리스트 항목 바로 아래의 필드일 때만 만든다.
    """
    mutated = with_value(document, path, value)
    for depth in (len(path) - 1, len(path) - 2):
        if depth < 0 or not isinstance(path[depth], int):
            continue
        items: Any = mutated
        for step in path[:depth]:
            items = items[step]
        items.append(copy.deepcopy(items[path[depth]]))
        return mutated
    return None


def mutations(
    documents: list[tuple[str, type[BaseModel], dict[str, Any]]],
) -> Iterator[tuple[str, type[BaseModel], Position, Mark, dict[str, Any]]]:
    """Every (document shape × position × mark) — the value replaced, and its twin if any."""
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for label, model, document in documents:
        for path in leaf_positions(document):
            shape = (model.__name__, shape_of(path))
            if shape in seen:
                continue
            seen.add(shape)
            for mark in MARKS:
                yield label, model, path, mark, with_value(document, path, mark.value)
                twin = with_twin(document, path, mark.value)
                if twin is not None:
                    yield label, model, path, mark, twin
