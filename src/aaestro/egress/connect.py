"""HTTPS tunnels decided by destination host (D3).

TLS 를 가로채지 않는다 — CONNECT 목적지 ``host[:port]`` 까지만 보고 판정한다. 에이전트는
``Proxy-Authorization`` 으로 자기 신원을 내민다 (``HTTPS_PROXY=http://<agent>:<신원>@proxy:port``).

프록시는 외부 네트워크에 붙어 있으므로 에이전트가 직접 못 닿는 곳(메타데이터 주소, 호스트의 사설
서비스)에 닿을 수 있다. 그래서 목적지가 사설·루프백·링크 로컬 주소로 풀리면 운영자가 명시한 목적지가
아닌 한 거부하고, 확인한 주소로만 연결한다 — 판정한 이름이 연결 순간 다른 주소로 바뀌는 것
(DNS rebinding)을 막는다.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import structlog

from aaestro.access.baselines import egress_target
from aaestro.access.client import DecisionSource
from aaestro.access.model import ResourceKind

if TYPE_CHECKING:
    from aaestro.access.client import Verdict
    from aaestro.access.model import Mode

log = structlog.get_logger(__name__)

MAX_HEADER_BYTES = 16 * 1024
HEADER_TIMEOUT_S = 10.0
CONNECT_TIMEOUT_S = 10.0
PIPE_CHUNK_BYTES = 64 * 1024
UNKNOWN_IDENTITY = "unknown-identity"


class EgressMode(StrEnum):
    """Whether a denial blocks the call.

    ``record`` 는 전환 기간용이다 — 선언되지 않은 외부 호출을 거부 판정으로 기록하되 통과시킨다.
    신원을 모르는 호출은 기록 모드에서도 통과시키지 않는다: 드러낼 에이전트가 없다.
    """

    RECORD = "record"
    ENFORCE = "enforce"


class Decider(Protocol):
    async def decide(
        self, credential: str, kind: ResourceKind, target: str, mode: Mode | None = None
    ) -> Verdict: ...


Resolver = Callable[[str, int], Awaitable[list[str]]]
Opener = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


async def resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def is_public(address: str) -> bool:
    """전 세계에서 라우팅되는 주소인가 — 사설·루프백·링크 로컬·CGNAT·예약 대역은 아니다.

    ``is_private`` 만 보면 ``100.64.0.0/10``(CGNAT) 같은 대역이 빠진다 — ``is_global`` 이 기준이다.
    """
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast


@dataclass
class ConnectProxy:
    """One CONNECT listener.

    Attributes:
        access: 레지스트리 판정 클라이언트 — 캐시·변경 알림·장애 시 동작은 거기 있다.
        mode: ``enforce`` 면 거부를 막고, ``record`` 면 기록만 한다.
        private_destinations: 사설 주소로 풀려도 되는 목적지 이름 — 운영자가 명시한 것만.
    """

    access: Decider
    mode: EgressMode = EgressMode.ENFORCE
    private_destinations: Collection[str] = ()
    resolver: Resolver = resolve
    opener: Opener = asyncio.open_connection
    connect_timeout_s: float = CONNECT_TIMEOUT_S

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._handle(reader, writer)
        except (ConnectionError, OSError):
            pass  # 상대가 먼저 끊었다 — 알릴 곳이 없다
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                writer.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEADER_TIMEOUT_S)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            await _respond(writer, 400, "malformed proxy request")
            return
        if len(head) > MAX_HEADER_BYTES:
            await _respond(writer, 431, "request headers too large")
            return
        method, authority, headers = _parse(head)
        if method != "CONNECT":
            # 평문 HTTP 전달은 하지 않는다 — 판정은 CONNECT 목적지와 종단하는 provider 로만 (D3)
            await _respond(writer, 405, "only CONNECT is proxied")
            return
        try:
            host, port = _split_authority(authority)
        except ValueError:
            await _respond(writer, 400, "invalid CONNECT authority")
            return
        credential = _credential(headers)
        if not credential:
            await _respond(writer, 407, "proxy identity required", challenge=True)
            return

        target = egress_target(host, port)
        verdict = await self.access.decide(credential, ResourceKind.EGRESS, target)
        if not await self._admitted(writer, verdict, target):
            return
        await self._tunnel(reader, writer, verdict, host, port, target)

    async def _admitted(self, writer: asyncio.StreamWriter, verdict: Verdict, target: str) -> bool:
        fields = {
            "agent": verdict.agent or "",
            "resource": ResourceKind.EGRESS.value,
            "target": target,
            "decision": "allow" if verdict.allowed else "deny",
            "decision_source": verdict.source.value,
            "decided_by": verdict.decided_by,
        }
        if verdict.allowed:
            log.info("egress allowed", **fields)
            return True
        if verdict.source is DecisionSource.UNREACHABLE:
            log.warning("egress undecidable", error_code="ACC_002", **fields)
            await _respond(writer, 503, "access registry unreachable", code="ACC_002")
            return False
        if verdict.agent is None or verdict.decided_by == UNKNOWN_IDENTITY:
            log.warning("egress identity refused", **fields)
            await _respond(writer, 407, "proxy identity refused", challenge=True)
            return False
        if self.mode is EgressMode.RECORD:
            log.warning("egress denied but recorded only", mode_egress="record", **fields)
            return True
        log.info("egress denied", error_code="ACC_001", **fields)
        await _respond(writer, 403, "egress destination denied", code="ACC_001")
        return False

    async def _tunnel(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        verdict: Verdict,
        host: str,
        port: int,
        target: str,
    ) -> None:
        try:
            addresses = await asyncio.wait_for(self.resolver(host, port), self.connect_timeout_s)
        except (OSError, TimeoutError):
            await _respond(writer, 502, "destination did not resolve")
            return
        chosen = self._reachable(addresses, target)
        if chosen is None:
            log.warning(
                "egress to a private address refused",
                agent=verdict.agent or "",
                resource=ResourceKind.EGRESS.value,
                target=target,
                error_code="ACC_001",
            )
            await _respond(writer, 403, "destination resolves to a private address", code="ACC_001")
            return
        try:
            upstream_reader, upstream_writer = await asyncio.wait_for(
                self.opener(chosen, port), self.connect_timeout_s
            )
        except (OSError, TimeoutError):
            await _respond(writer, 502, "destination unreachable")
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        try:
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            with contextlib.suppress(ConnectionError, OSError):
                upstream_writer.close()

    def _reachable(self, addresses: list[str], target: str) -> str | None:
        """연결할 주소 — 사설 주소는 명시한 목적지만. 하나라도 사설이면 공개 주소만 고른다."""
        if target in self.private_destinations:
            return addresses[0] if addresses else None
        public = [a for a in addresses if is_public(a)]
        return public[0] if public else None


def _parse(head: bytes) -> tuple[str, str, dict[str, str]]:
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ")
    method, authority = (parts[0], parts[1]) if len(parts) >= 3 else ("", "")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return method.upper(), authority, headers


def _split_authority(authority: str) -> tuple[str, int]:
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest.removeprefix(":")
    else:
        host, _, port = authority.rpartition(":")
    number = int(port)
    if not host or not 0 < number < 65536:
        raise ValueError(authority)
    return host, number


def _credential(headers: dict[str, str]) -> str:
    value = headers.get("proxy-authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() == "bearer":
        return token.strip()
    if scheme.lower() == "basic":
        try:
            decoded = base64.b64decode(token.strip(), validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return ""
        return decoded.partition(":")[2]
    return ""


async def _respond(
    writer: asyncio.StreamWriter,
    status: int,
    message: str,
    *,
    code: str | None = None,
    challenge: bool = False,
) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode()
    reason = {400: "Bad Request", 403: "Forbidden", 405: "Method Not Allowed",
              407: "Proxy Authentication Required", 431: "Request Header Fields Too Large",
              502: "Bad Gateway", 503: "Service Unavailable"}.get(status, "Error")  # fmt: skip
    head = [f"HTTP/1.1 {status} {reason}", "content-type: application/json",
            f"content-length: {len(body)}", "connection: close"]  # fmt: skip
    if challenge:
        head.append('proxy-authenticate: Basic realm="aaestro-egress"')
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    with contextlib.suppress(ConnectionError, OSError):
        await writer.drain()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while data := await source.read(PIPE_CHUNK_BYTES):
            sink.write(data)
            await sink.drain()
    except (ConnectionError, OSError):
        return
    finally:
        with contextlib.suppress(ConnectionError, OSError, RuntimeError):
            if sink.can_write_eof():
                sink.write_eof()


__all__ = ["ConnectProxy", "Decider", "EgressMode", "is_public", "resolve"]
