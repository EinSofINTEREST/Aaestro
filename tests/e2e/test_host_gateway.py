"""Isolated agents cannot reach host services through the bridge gateway (#303).

`--internal` 네트워크는 외부로 나가지 못하지만 게이트웨이는 호스트 자신이다. 이 테스트는
``deployments/docker/isolate-agent-network.sh`` 를 **그대로** 적용해 다음을 본다:

- 적용 전: 에이전트 네트워크의 컨테이너가 게이트웨이의 호스트 서비스에 닿는다 (막을 이유가 있다)
- 적용 후: 새로 여는 연결은 실패한다
- 적용 후에도 호스트가 먼저 여는 연결(control plane → 에이전트)은 된다
- 제거하면 원래대로 돌아간다

**호스트 방화벽(INPUT 체인)을 바꾼다** — 기본 E2E 에서는 건너뛰고 ``AAESTRO_E2E_HOST_FIREWALL=1``
일 때만 돈다. 규칙은 이 테스트가 만든 네트워크의 브리지에만 걸고, 끝나면 반드시 지운다. iptables 는
``--net=host --cap-add NET_ADMIN`` 컨테이너로 실행하므로 호스트에 root 셸이 없어도 된다.
"""

from __future__ import annotations

import http.server
import os
import socket
import subprocess
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.e2e.test_stack import REPO_ROOT, docker, requires_docker

pytestmark = [
    pytest.mark.e2e,
    requires_docker,
    pytest.mark.skipif(
        os.environ.get("AAESTRO_E2E_HOST_FIREWALL") != "1",
        reason="changes the host firewall — set AAESTRO_E2E_HOST_FIREWALL=1 to run",
    ),
]

NETWORK = "aaestro-e2e-isolate"
AGENT = "aaestro-e2e-isolate-agent"
IPTABLES_IMAGE = "aaestro/e2e-iptables:0.1.0"
ADDRESS_FORMAT = '{{(index .NetworkSettings.Networks "' + NETWORK + '").IPAddress}}'
SCRIPT = REPO_ROOT / "deployments" / "docker" / "isolate-agent-network.sh"
PROBE = """
import socket, sys
try:
    socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=3).close()
    print("OPEN")
except OSError as err:
    print(type(err).__name__)
"""


def iptables(binary: str) -> str:
    # 호스트와 같은 nf_tables 백엔드를 쓴다 — legacy 와 섞이면 규칙이 보이지 않는 곳에 들어간다
    return f"docker run --rm --net=host --cap-add NET_ADMIN {IPTABLES_IMAGE} {binary}"


def script(action: str) -> str:
    env = {
        **os.environ,
        "IPTABLES": iptables("iptables-nft"),
        "IP6TABLES": iptables("ip6tables-nft"),
    }
    result = subprocess.run(  # noqa: S603
        ["sh", str(SCRIPT), action, NETWORK],  # noqa: S607
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=180,
    )
    return result.stdout


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("0.0.0.0", 0))  # noqa: S104 — 모든 인터페이스에 여는 호스트 서비스를 흉내 낸다
        return int(sock.getsockname()[1])


@pytest.fixture
def isolated(tmp_path: Path) -> Iterator[dict[str, str]]:
    (tmp_path / "Dockerfile").write_text(
        "FROM alpine:latest\nRUN apk add --no-cache iptables ip6tables\n", encoding="utf-8"
    )
    docker("build", "-q", "-t", IPTABLES_IMAGE, str(tmp_path), timeout=600)
    docker("rm", "-f", AGENT, check=False)
    docker("network", "rm", NETWORK, check=False)
    docker("network", "create", "--internal", NETWORK)
    docker(
        "run", "-d", "--name", AGENT, "--network", NETWORK,
        "--read-only", "--user", "1000:1000", "--cap-drop", "ALL",
        "--entrypoint", "python", "aaestro/agent-base:0.1.0", "-m", "http.server", "8080",
    )  # fmt: skip
    port = free_port()
    handler = http.server.SimpleHTTPRequestHandler
    server = http.server.ThreadingHTTPServer(("0.0.0.0", port), handler)  # noqa: S104
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield {
            "gateway": docker(
                "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Gateway}}{{end}}", NETWORK
            ),
            "agent": docker("inspect", "-f", ADDRESS_FORMAT, AGENT),
            "port": str(port),
        }
    finally:
        server.shutdown()
        subprocess.run(["sh", str(SCRIPT), "remove", NETWORK], check=False,  # noqa: S603, S607
                       env={**os.environ, "IPTABLES": iptables("iptables-nft"),
                            "IP6TABLES": iptables("ip6tables-nft")})  # fmt: skip
        docker("rm", "-f", AGENT, check=False)
        docker("network", "rm", NETWORK, check=False)


def reach_host(gateway: str, port: str) -> str:
    return docker("exec", AGENT, "python", "-c", PROBE, gateway, port, check=False)


def host_reaches_agent(address: str) -> bool:
    try:
        socket.create_connection((address, 8080), timeout=3).close()
    except OSError:
        return False
    return True


def test_the_isolation_script_closes_the_gateway_but_keeps_host_initiated_traffic(isolated):
    gateway, port, agent = isolated["gateway"], isolated["port"], isolated["agent"]
    assert reach_host(gateway, port) == "OPEN", (
        "게이트웨이로 호스트에 닿지 않는다 — 막을 대상이 없다"
    )

    script("apply")

    assert "applied" in script("status")
    assert reach_host(gateway, port) != "OPEN", "적용 뒤에도 에이전트가 호스트 서비스에 닿는다"
    assert host_reaches_agent(agent), "호스트가 여는 연결(control plane → 에이전트)까지 막았다"

    script("apply")  # 멱등 — 두 번 적용해도 규칙이 겹치지 않는다
    script("remove")

    assert "not applied" in script("status")
    assert reach_host(gateway, port) == "OPEN", "제거한 뒤에도 막혀 있다"
