"""Compose files must stay valid and keep the isolation limits they declare.

`make up` 이 오래 깨져 있었다 (#226): 개발 compose 가 PID 상한을 `pids_limit` 과
`deploy.resources.limits.pids` 두 자리에 나눠 선언해, compose 가 프로젝트를 통째로
거절했다. 이 스택을 띄우는 경로가 어디에도 없어 아무도 몰랐다.

여기서는 **Docker 없이** 선언 자체를 본다 — 스키마 검증은 integration 이 맡는다.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

DOCKER_DIR = Path(__file__).resolve().parents[2] / "deployments" / "docker"
COMPOSE_FILES = sorted(DOCKER_DIR.glob("compose*.yaml"))


def services(path: Path) -> dict[str, dict[str, Any]]:
    document = yaml.safe_load(path.read_text("utf-8"))
    return document.get("services") or {}


def deploy_limits(service: dict[str, Any]) -> dict[str, Any]:
    return ((service.get("deploy") or {}).get("resources") or {}).get("limits") or {}


def test_compose_files_are_discovered():
    """glob 이 비면 아래 테스트가 전부 공허하게 통과한다."""
    assert COMPOSE_FILES, "compose 파일을 하나도 찾지 못했다"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_service_splits_the_pid_limit_across_both_forms(path: Path):
    """#226 — 두 자리에 나눠 쓰면 compose 가 프로젝트를 invalid 로 거절한다.

    값이 같아도 쓰지 않는다. 한 자리에서만 읽히는 편이 나중에 값을 바꿀 때
    한쪽만 고치는 사고를 막는다.
    """
    for name, service in services(path).items():
        split = "pids_limit" in service and "pids" in deploy_limits(service)
        assert not split, f"{path.name}:{name} 이 PID 상한을 두 자리에 선언했다"


def test_the_dev_agent_keeps_its_isolation_limits():
    """02 Docker Isolation Rules 4 — cpu/memory/pids 상한은 선언 필수.

    #226 을 고치며 표기를 옮겼다. 상한이 조용히 사라지면 규칙을 어긴 채
    초록이 되므로 여기서 붙잡는다.
    """
    agent = services(DOCKER_DIR / "compose.yaml")["agent-echo"]
    limits = deploy_limits(agent)

    assert limits.get("cpus"), "cpu 상한이 사라졌다"
    assert limits.get("memory"), "memory 상한이 사라졌다"
    assert limits.get("pids") or agent.get("pids_limit"), "pid 상한이 사라졌다"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_no_service_reaches_the_host(path: Path):
    """02 Network/Volumes — 호스트 네트워크와 docker.sock 은 격리를 무효로 만든다."""
    for name, service in services(path).items():
        assert service.get("network_mode") != "host", f"{path.name}:{name} 이 host 네트워크를 쓴다"
        mounts = service.get("volumes") or []
        sock = [m for m in mounts if isinstance(m, str) and "docker.sock" in m]
        assert not sock, f"{path.name}:{name} 이 docker.sock 을 마운트한다"


TOKEN_REF = re.compile(r"^\$\{(?P<var>[A-Z0-9_]+):-(?P<default>[^}]*)\}$")


def control_token_refs(path: Path) -> dict[str, tuple[str, str]]:
    """서비스 → (변수 이름, default). 상수 값은 (값, 값) 으로 본다."""
    refs: dict[str, tuple[str, str]] = {}
    for name, service in services(path).items():
        raw = (service.get("environment") or {}).get("AAESTRO_AGENT_TOKEN")
        if raw is None:
            continue
        match = TOKEN_REF.match(str(raw))
        refs[name] = (match["var"], match["default"]) if match else (str(raw), str(raw))
    return refs


def assert_tokens_are_per_agent(path: Path) -> None:
    refs = control_token_refs(path)
    assert refs, f"{path.name}: 토큰을 선언한 에이전트 서비스를 찾지 못했다"
    # 변수가 같으면 export 하는 순간 모든 에이전트가 같은 토큰이 된다 — default 와 별개로 본다
    variables = [var for var, _ in refs.values()]
    defaults = [default for _, default in refs.values()]
    assert len(set(variables)) == len(variables), f"{path.name}: 토큰 변수가 겹친다 {refs}"
    assert len(set(defaults)) == len(defaults), f"{path.name}: 토큰 default 가 겹친다 {refs}"


@pytest.mark.parametrize("path", COMPOSE_FILES, ids=lambda p: p.name)
def test_agent_services_do_not_share_a_control_token(path: Path):
    """#413 — 토큰이 하나면 한 에이전트의 토큰으로 다른 에이전트의 Control API 가 열린다."""
    assert_tokens_are_per_agent(path)


def test_shared_token_variable_with_distinct_defaults_is_caught(tmp_path: Path):
    """검출력 — 변수는 같고 default 만 다른 compose 는 실패해야 한다."""
    broken = tmp_path / "compose.yaml"
    broken.write_text(
        "services:\n"
        "  a:\n    environment:\n      AAESTRO_AGENT_TOKEN: ${AAESTRO_AGENT_TOKEN:-x}\n"
        "  b:\n    environment:\n      AAESTRO_AGENT_TOKEN: ${AAESTRO_AGENT_TOKEN:-y}\n",
        "utf-8",
    )

    with pytest.raises(AssertionError, match="변수가 겹친다"):
        assert_tokens_are_per_agent(broken)
