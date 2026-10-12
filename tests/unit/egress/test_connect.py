"""CONNECT tunnels decided per destination (#293)."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json

import pytest

from aaestro.access.client import DecisionSource, Verdict
from aaestro.egress.connect import ConnectProxy, EgressMode, is_public

PUBLIC = "93.184.216.34"


class Registry:
    """판정 대역 — 받은 (신원, 목적지) 를 기록한다."""

    def __init__(self) -> None:
        self.allowed: set[str] = {"api.search.example.com"}
        self.identities = {"cred-researcher": "researcher"}
        self.down = False
        self.asked: list[tuple[str, str]] = []

    async def decide(self, credential, kind, target, mode=None) -> Verdict:
        self.asked.append((credential, target))
        if self.down:
            return Verdict(None, False, "unreachable", DecisionSource.UNREACHABLE)
        agent = self.identities.get(credential)
        if agent is None:
            return Verdict(None, False, "unknown-identity", DecisionSource.FRESH)
        return Verdict(agent, target in self.allowed, "declaration", DecisionSource.FRESH)


@contextlib.asynccontextmanager
async def running(proxy: ConnectProxy):
    server = await asyncio.start_server(proxy.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port


@contextlib.asynccontextmanager
async def echo_upstream():
    async def echo(reader, writer):
        while data := await reader.read(1024):
            writer.write(data.upper())
            await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    async with server:
        yield server.sockets[0].getsockname()[1]


def basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


async def connect(port: int, authority: str, auth: str | None = None, method: str = "CONNECT"):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    lines = [f"{method} {authority} HTTP/1.1", f"Host: {authority}"]
    if auth:
        lines.append(f"Proxy-Authorization: {auth}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
    await writer.drain()
    head = await reader.readuntil(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    text = head.decode()
    if status != 200:
        length = next(
            int(line.split(":", 1)[1])
            for line in text.split("\r\n")
            if line.lower().startswith("content-length")
        )
        text += (await reader.readexactly(length)).decode()
    return status, text, reader, writer


def proxy_for(registry: Registry, upstream_port: int, **options) -> tuple[ConnectProxy, list]:
    opened: list[tuple[str, int]] = []
    addresses = options.pop("addresses", [PUBLIC])

    async def resolver(host, port):
        return addresses

    async def opener(host, port):
        opened.append((host, port))
        return await asyncio.open_connection("127.0.0.1", upstream_port)

    return ConnectProxy(access=registry, resolver=resolver, opener=opener, **options), opened


# --- 판정 ----------------------------------------------------------------------------


async def test_an_allowed_destination_gets_a_tunnel_to_the_checked_address():
    registry = Registry()
    async with echo_upstream() as upstream:
        proxy, opened = proxy_for(registry, upstream)
        async with running(proxy) as port:
            status, _, reader, writer = await connect(
                port, "api.search.example.com:443", basic("researcher", "cred-researcher")
            )
            writer.write(b"hello")
            await writer.drain()
            echoed = await reader.readexactly(5)
            writer.close()

    assert status == 200 and echoed == b"HELLO"
    assert registry.asked == [("cred-researcher", "api.search.example.com")], "443 은 이름에서 뺀다"
    assert opened == [(PUBLIC, 443)], "판정한 이름을 다시 풀지 않고 확인한 주소로 연결해야 한다"


async def test_a_denied_destination_is_refused_with_acc_001():
    registry = Registry()
    async with echo_upstream() as upstream:
        proxy, opened = proxy_for(registry, upstream)
        async with running(proxy) as port:
            status, head, _, writer = await connect(
                port, "evil.example.com:443", basic("researcher", "cred-researcher")
            )
            writer.close()

    assert status == 403 and json.loads(head.split("\r\n\r\n", 1)[1])["error"]["code"] == "ACC_001"
    assert opened == [], "거부했는데 목적지에 연결했다"


async def test_record_mode_lets_a_denied_destination_through():
    registry = Registry()
    async with echo_upstream() as upstream:
        proxy, opened = proxy_for(registry, upstream, mode=EgressMode.RECORD)
        async with running(proxy) as port:
            status, _, _, writer = await connect(
                port, "evil.example.com:8443", basic("researcher", "cred-researcher")
            )
            writer.close()

    assert status == 200 and opened == [(PUBLIC, 8443)]
    assert registry.asked[-1][1] == "evil.example.com:8443"


@pytest.mark.parametrize("mode", list(EgressMode))
async def test_an_unknown_identity_is_refused_even_in_record_mode(mode):
    registry = Registry()
    async with echo_upstream() as upstream:
        proxy, opened = proxy_for(registry, upstream, mode=mode)
        async with running(proxy) as port:
            status, head, _, writer = await connect(
                port, "api.search.example.com:443", basic("researcher", "stolen")
            )
            writer.close()

    assert status == 407 and "proxy-authenticate" in head.lower()
    assert opened == []


async def test_an_undecidable_destination_is_refused_as_retryable():
    registry = Registry()
    registry.down = True
    async with echo_upstream() as upstream:
        proxy, _ = proxy_for(registry, upstream, mode=EgressMode.RECORD)
        async with running(proxy) as port:
            status, head, _, writer = await connect(
                port, "api.search.example.com:443", basic("researcher", "cred-researcher")
            )
            writer.close()

    assert status == 503 and json.loads(head.split("\r\n\r\n", 1)[1])["error"]["code"] == "ACC_002"


@pytest.mark.parametrize(
    ("auth", "method", "authority", "expected"),
    [
        (None, "CONNECT", "api.search.example.com:443", 407),
        ("Bearer cred-researcher", "CONNECT", "api.search.example.com:443", 200),
        ("Basic !!!", "CONNECT", "api.search.example.com:443", 407),
        (basic("researcher", "cred-researcher"), "GET", "http://api.search.example.com/", 405),
        (basic("researcher", "cred-researcher"), "CONNECT", "no-port", 400),
    ],
)
async def test_requests_the_proxy_does_not_serve(auth, method, authority, expected):
    registry = Registry()
    async with echo_upstream() as upstream:
        proxy, _ = proxy_for(registry, upstream)
        async with running(proxy) as port:
            status, _, _, writer = await connect(port, authority, auth, method)
            writer.close()

    assert status == expected


# --- 사설 주소 --------------------------------------------------------------------------


@pytest.mark.parametrize("address", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "::1", "fd00::1"])
async def test_an_allowed_name_that_resolves_to_a_private_address_is_refused(address):
    """허용된 이름이 사설 주소로 풀리면 프록시가 에이전트 대신 내부망에 닿는다 (DNS rebinding)."""
    registry = Registry()
    async with echo_upstream() as upstream:
        proxy, opened = proxy_for(registry, upstream, addresses=[address])
        async with running(proxy) as port:
            status, _, _, writer = await connect(
                port, "api.search.example.com:443", basic("researcher", "cred-researcher")
            )
            writer.close()

    assert status == 403 and opened == []


async def test_a_listed_private_destination_is_reachable():
    registry = Registry()
    registry.allowed.add("fake-provider:8000")
    async with echo_upstream() as upstream:
        proxy, opened = proxy_for(
            registry,
            upstream,
            addresses=["172.18.0.4"],
            private_destinations={"fake-provider:8000"},
        )
        async with running(proxy) as port:
            status, _, _, writer = await connect(
                port, "fake-provider:8000", basic("researcher", "cred-researcher")
            )
            writer.close()

    assert status == 200 and opened == [("172.18.0.4", 8000)]


def test_public_addresses_are_told_apart():
    assert is_public(PUBLIC) and is_public("2606:4700::1111")
    assert not any(is_public(a) for a in ["10.1.2.3", "192.168.0.1", "100.64.0.1", "0.0.0.0"])  # noqa: S104
