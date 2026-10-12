"""Remote MCP termination — per-tool decisions, proxy-held credentials (#282), sidecars (#304)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import yaml

from aaestro.access.client import DecisionSource
from aaestro.catalog import Catalog
from aaestro.egress.connect import EgressMode
from aaestro.egress.mcp import McpTermination, McpUpstreams
from aaestro.egress.providers import create_provider_app
from tests.unit.egress.test_connect import Registry

TOKEN = "corp-mcp-token"  # noqa: S105 — 테스트 값
PUBLIC = "93.184.216.34"
SIDECAR_HOST = "aaestro-researcher--mcp-browser"


class Identities(Registry):
    """신원 조회까지 하는 판정 대역."""

    def __init__(self) -> None:
        super().__init__()
        self.allowed = {
            "mcp.corp.example",
            "mcp.corp.example:80",
            "corp/search",
            "corp/fetch",
            "lab.internal:9000",
        }

    async def identify(self, credential):
        if self.down:
            return None, DecisionSource.UNREACHABLE
        return self.identities.get(credential), DecisionSource.FRESH


class Server:
    """원격 MCP 서버 대역 — 받은 요청을 남기고 JSON 으로 답한다."""

    def __init__(self) -> None:
        self.seen: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}).encode()
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "mcp-session-id": "s-1"},
            stream=httpx.ByteStream(body),
        )


def workspace(tmp_path: Path, **corp) -> Catalog:
    servers = [
        {
            "name": "corp",
            "transport": "streamable-http",
            "url": "https://mcp.corp.example/mcp",
            "auth": {"type": "bearer", "token_env": "CORP_TOKEN"},
            **corp,
        },
        {"name": "lab", "transport": "streamable-http", "url": "http://lab.internal:9000/mcp"},
        {
            "name": "browser",
            "transport": "streamable-http",
            "sidecar": {"image": "mcp/playwright:1.2.0", "port": 3000},
        },
        {"name": "fs", "transport": "stdio", "command": ["mcp-server-fs"]},
    ]
    doc = {
        "apiVersion": "aaestro/v1",
        "kind": "Agent",
        "metadata": {"name": "researcher", "version": "0.1.0"},
        "spec": {
            "model": {"provider": "anthropic", "name": "claude-sonnet-5"},
            "promptset": {"ref": "promptsets/researcher@0.1.0"},
            "mcp": {"servers": servers},
            "runtime": {"env_allowlist": ["CORP_TOKEN"]},
        },
    }
    path = tmp_path / "agents" / "researcher" / "manifest.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return Catalog.under(tmp_path)


def termination(tmp_path, registry, server, **options) -> McpTermination:
    corp = options.pop("corp", {})
    tokens = options.pop("tokens", frozenset({"CORP_TOKEN"}))
    addresses = options.pop(
        "addresses",
        {
            "mcp.corp.example": PUBLIC,
            "lab.internal": "10.0.0.9",
            SIDECAR_HOST: "172.30.5.2",
        },
    )

    async def resolver(host, port):
        return [addresses[host]]

    return McpTermination(
        access=registry,
        upstreams=McpUpstreams(
            catalog=workspace(tmp_path, **corp),
            environ={"CORP_TOKEN": TOKEN, "ANTHROPIC_API_KEY": "sk-model-key"},
            tokens=tokens,
        ),
        http=httpx.AsyncClient(transport=httpx.MockTransport(server.handle)),
        resolver=resolver,
        **options,
    )


def client_for(target: McpTermination) -> httpx.AsyncClient:
    app = create_provider_app(target.access, {}, routers=[target.router()])
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://egress")


def tool_call(name: str, request_id: int = 7) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name}}


async def post(client, server="corp", body=None, credential="cred-researcher", **headers):
    if credential is not None:
        headers["authorization"] = f"Bearer {credential}"
    content = body if isinstance(body, bytes) else json.dumps(body or tool_call("search")).encode()
    return await client.post(
        f"/mcp/{server}",
        content=content,
        headers={"content-type": "application/json", **headers},
    )


async def test_an_allowed_tool_call_goes_to_the_declared_server_with_the_proxy_credential(
    tmp_path,
):
    server = Server()
    async with client_for(termination(tmp_path, Identities(), server)) as client:
        response = await post(client)

    assert response.status_code == 200
    [sent] = server.seen
    assert sent.headers["authorization"] == f"Bearer {TOKEN}", "프록시 자격이 붙지 않았다"
    assert "cred-researcher" not in str(sent.headers), "에이전트 신원이 서버로 새어 나갔다"
    assert (sent.url.host, sent.url.path) == (PUBLIC, "/mcp"), "확인한 주소로 붙지 않았다"
    assert sent.headers["host"] == "mcp.corp.example"
    assert sent.extensions["sni_hostname"] == "mcp.corp.example"


async def test_a_revoked_tool_is_refused_while_other_tools_of_the_server_still_work(tmp_path):
    registry, server = Identities(), Server()
    registry.allowed.discard("corp/search")
    async with client_for(termination(tmp_path, registry, server)) as client:
        denied = await post(client, body=tool_call("search", 11))
        allowed = await post(client, body=tool_call("fetch", 12))

    assert denied.status_code == 200
    assert denied.json()["id"] == 11 and denied.json()["error"]["message"].startswith("ACC_001")
    assert allowed.status_code == 200
    [sent] = server.seen
    assert json.loads(sent.content)["params"]["name"] == "fetch", "거부된 호출이 서버로 갔다"


async def test_record_mode_forwards_a_denied_tool(tmp_path):
    registry, server = Identities(), Server()
    registry.allowed.discard("corp/search")
    target = termination(tmp_path, registry, server, mode=EgressMode.RECORD)
    async with client_for(target) as client:
        response = await post(client)

    assert response.status_code == 200 and len(server.seen) == 1


async def test_other_messages_pass_after_the_server_host_is_decided(tmp_path):
    registry, server = Identities(), Server()
    async with client_for(termination(tmp_path, registry, server)) as client:
        listed = await post(client, body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        stream = await client.get("/mcp/corp", headers={"authorization": "Bearer cred-researcher"})

    assert listed.status_code == 200 and stream.status_code == 200
    assert [target for _, target in registry.asked] == ["mcp.corp.example", "mcp.corp.example"]


@pytest.mark.parametrize(
    ("change", "status", "why"),
    [
        (lambda r: r.allowed.discard("mcp.corp.example"), 403, "서버 호스트 이그레스 회수"),
        (lambda r: r.identities.clear(), 401, "모르는 신원"),
        (lambda r: setattr(r, "down", True), 503, "레지스트리 도달 불가"),
    ],
)
async def test_the_server_is_not_reached_without_a_decision(tmp_path, change, status, why):
    registry, server = Identities(), Server()
    change(registry)
    async with client_for(termination(tmp_path, registry, server)) as client:
        response = await post(client)

    assert response.status_code == status, why
    assert server.seen == [], why


@pytest.mark.parametrize(
    ("kwargs", "server_name", "body", "status", "why"),
    [
        ({}, "ghost", None, 404, "선언하지 않은 서버 — 주소를 요청이 고르지 못한다"),
        ({}, "corp", b"not json", 400, "JSON 이 아닌 요청"),
        ({}, "corp", {"jsonrpc": "2.0", "id": 1, "method": "tools/call"}, 400, "이름 없는 호출"),
        ({"tokens": frozenset()}, "corp", None, 502, "운영자가 허용하지 않은 자격 이름"),
        (
            {"corp": {"auth": {"type": "bearer", "token_env": "ANTHROPIC_API_KEY"}}},
            "corp",
            None,
            502,
            "선언이 모델 키를 자격으로 끌어내려 해도 보내지 않는다",
        ),
        ({"corp": {"url": "http://mcp.corp.example/mcp"}}, "corp", None, 502, "평문으로 자격을"),
        ({}, "lab", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, 502, "자격 없는 평문 서버도"),
        (
            {"allow_plaintext": True},
            "lab",
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            502,
            "스위치만 켜고 사설로 명시하지 않은 평문 서버",
        ),
        (
            {"addresses": {"mcp.corp.example": "10.0.0.5", "lab.internal": "10.0.0.9"}},
            "corp",
            None,
            403,
            "사설 주소로 풀리는 서버",
        ),
    ],
)
async def test_unsafe_or_unknown_requests_do_not_reach_any_server(
    tmp_path, kwargs, server_name, body, status, why
):
    registry, server = Identities(), Server()
    async with client_for(termination(tmp_path, registry, server, **kwargs)) as client:
        response = await post(client, server=server_name, body=body)

    assert response.status_code == status, why
    assert server.seen == [], why


async def test_plaintext_reaches_only_a_listed_private_server(tmp_path):
    """스위치를 켜도 공인 호스트로는 평문이 나가지 않는다 — 사설로 명시한 서버만 (#302)."""
    server = Server()
    target = termination(
        tmp_path,
        Identities(),
        server,
        corp={"url": "http://mcp.corp.example/mcp"},
        private_destinations=("lab.internal:9000",),
        allow_plaintext=True,
    )
    async with client_for(target) as client:
        corp = await post(client)
        lab = await post(client, server="lab", body={"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert (corp.status_code, lab.status_code) == (502, 200)
    assert [str(r.url) for r in server.seen] == ["http://10.0.0.9:9000/mcp"]
    assert "authorization" not in server.seen[0].headers, "자격 없는 서버에 자격을 붙였다"


async def test_a_batch_with_a_denied_tool_is_refused_whole(tmp_path):
    registry, server = Identities(), Server()
    registry.allowed.discard("corp/search")
    async with client_for(termination(tmp_path, registry, server)) as client:
        response = await post(client, body=[tool_call("fetch", 1), tool_call("search", 2)])

    assert response.status_code == 403 and server.seen == []


async def test_without_an_identity_nothing_is_asked(tmp_path):
    registry, server = Identities(), Server()
    async with client_for(termination(tmp_path, registry, server)) as client:
        response = await post(client, credential=None)

    assert response.status_code == 401 and registry.asked == [] and server.seen == []


async def test_a_session_is_usable_only_by_the_agent_it_was_issued_to(tmp_path):
    """upstream 은 모두에게서 같은 자격을 받는다 — 세션 id 는 발급받은 (에이전트, 서버) 만 쓴다."""
    registry, server = Identities(), Server()
    target = termination(
        tmp_path,
        registry,
        server,
        allow_plaintext=True,
        private_destinations=("lab.internal:9000",),
    )
    async with client_for(target) as client:
        opened = await post(client, body={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        sealed = opened.headers["mcp-session-id"]
        assert sealed != "s-1" and sealed.startswith("s-1."), "upstream 세션 id 를 그대로 돌려줬다"

        again = await post(client, **{"mcp-session-id": sealed})
        forged = await post(client, **{"mcp-session-id": "s-1"})
        tampered = await post(client, **{"mcp-session-id": "s-2." + sealed.split(".", 1)[1]})
        other_server = await post(
            client, server="lab", body={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            **{"mcp-session-id": sealed},
        )  # fmt: skip

    assert again.status_code == 200
    assert server.seen[1].headers["mcp-session-id"] == "s-1", "서명을 벗기지 않고 보냈다"
    assert (forged.status_code, tampered.status_code, other_server.status_code) == (404, 404, 404)
    assert len(server.seen) == 2


def test_a_seal_binds_agent_and_server():
    from aaestro.egress.mcp import SessionSeal

    seal = SessionSeal(key=b"k" * 32)
    sealed = seal.seal("researcher", "corp", "abc")

    assert seal.open("researcher", "corp", sealed) == "abc"
    assert seal.open("writer", "corp", sealed) is None
    assert seal.open("researcher", "lab", sealed) is None
    assert SessionSeal(key=b"x" * 32).open("researcher", "corp", sealed) is None
    assert seal.open("researcher", "corp", "abc") is None


async def test_a_listed_plaintext_server_that_resolves_publicly_is_refused(tmp_path):
    """사설로 적어 둔 이름이 공인 주소로 풀리면 평문으로 보내지 않는다 (#305 리뷰)."""
    server = Server()
    target = termination(
        tmp_path,
        Identities(),
        server,
        corp={"url": "http://mcp.corp.example/mcp"},
        private_destinations=("mcp.corp.example:80",),
        allow_plaintext=True,
    )
    async with client_for(target) as client:
        response = await post(client)

    assert response.status_code == 502
    assert server.seen == [], "공인 주소로 평문을 보냈다"


# --- 사이드카 (#304) ------------------------------------------------------------------


async def test_a_sidecar_tool_goes_to_the_agents_own_sidecar_decided_per_tool(tmp_path):
    """주소는 runtime 이 띄운 이름에서, 판정은 도구마다 — 바깥 목적지가 아니라 egress 판정 없음."""
    registry, server = Identities(), Server()
    registry.allowed.add("browser/navigate")
    target = termination(tmp_path, registry, server)

    async with client_for(target) as client:
        response = await post(client, server="browser", body=tool_call("navigate"))

    assert response.status_code == 200
    [sent] = server.seen
    assert str(sent.url) == "http://172.30.5.2:3000/mcp"
    assert sent.headers["host"] == f"{SIDECAR_HOST}:3000"
    assert "authorization" not in sent.headers, "사이드카에 자격을 실었다"
    assert registry.asked == [("cred-researcher", "browser/navigate")]


async def test_a_revoked_sidecar_tool_is_refused_and_the_others_still_work(tmp_path):
    registry, server = Identities(), Server()
    registry.allowed.add("browser/navigate")
    target = termination(tmp_path, registry, server)

    async with client_for(target) as client:
        denied = await post(client, server="browser", body=tool_call("evaluate"))
        allowed = await post(client, server="browser", body=tool_call("navigate"))

    assert "ACC_001" in denied.json()["error"]["message"]
    assert allowed.status_code == 200
    assert len(server.seen) == 1, "회수된 도구 호출이 사이드카에 닿았다"


async def test_a_sidecar_that_resolves_publicly_is_never_sent_to(tmp_path):
    """사이드카는 사이드카 네트워크의 사설 주소에만 있다 — 공인 주소로 풀리면 보내지 않는다."""
    registry, server = Identities(), Server()
    registry.allowed.add("browser/navigate")
    target = termination(tmp_path, registry, server, addresses={SIDECAR_HOST: PUBLIC})

    async with client_for(target) as client:
        response = await post(client, server="browser", body=tool_call("navigate"))

    assert response.status_code == 502
    assert server.seen == []


async def test_a_stdio_server_is_not_reachable_through_the_proxy(tmp_path):
    """stdio 서버는 컨테이너 안이다 — 같은 이름의 원격 서버를 만들어 내지 않는다."""
    registry, server = Identities(), Server()
    registry.allowed.add("fs/read_file")
    target = termination(tmp_path, registry, server)

    async with client_for(target) as client:
        response = await post(client, server="fs", body=tool_call("read_file"))

    assert response.status_code == 404
    assert server.seen == []
