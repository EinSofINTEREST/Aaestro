"""Integration tests for the Docker runtime lifecycle.

실제 컨테이너로 start → health ready → invoke → stop 전 주기를 검증한다.
Docker 가 없으면 skip 한다 (게이트를 막지 않는다).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from aaestro.core.agent import HealthState, HealthStatus
from aaestro.core.errors import AaestroError
from aaestro.core.manifest import AgentManifest
from aaestro.runtime.docker.engine import DEFAULT_STOP_GRACE_S, ContainerHandle, DockerEngine
from aaestro.runtime.docker.errors import ImageMissingError, NetworkIsolationError
from aaestro.runtime.health import HealthMonitor
from aaestro.runtime.lifecycle import AgentLifecycle, AgentState
from aaestro.runtime.spec import build_container_spec
from tests.fixtures.builders import make_manifest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[3]
ECHO_MANIFEST = REPO_ROOT / "agents" / "echo" / "manifest.yaml"
# 동시 실행 (CI 두 잡 · 로컬 두 프로세스) 이 서로의 리소스를 지우지 않도록 실행마다 다른 접미사
RUN = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
TEST_NETWORK = f"aaestro-net-lifecycle-{RUN}"
TOKEN = "integration-token"  # noqa: S105
DOCKER_BIN = shutil.which("docker")
STOP_AFTER_START_LIMIT_S = 10.0
"""기동 직후 정지의 상한 — 기본 유예 (30초) 보다 훨씬 짧아야 SIGTERM 으로 끝난 것이다 (#570)."""
GRACEFUL_EXIT_CODES = (0, 143)
"""SIGTERM 경로의 종료 코드 — 143 은 tini 가 전달한 SIGTERM 으로 끝남, 0 은 agentd 의 정상 종료.
137 (SIGKILL) · 1 (기동 실패) 은 정지가 아니다."""


def docker(*args: str, check: bool = True) -> str:
    """docker CLI 호출."""
    result = subprocess.run(  # noqa: S603
        [DOCKER_BIN or "docker", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)} failed: {result.stderr}")
    return result.stdout.strip()


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(  # noqa: S603
            [DOCKER_BIN or "docker", "info"],
            capture_output=True,
            check=False,
        ).returncode
        == 0
    )


requires_docker = pytest.mark.skipif(not docker_available(), reason="docker daemon unavailable")


class CliDockerClient:
    """docker CLI 를 쓰는 DockerClient 구현.

    SDK 대신 CLI 를 쓰는 이유는 테스트가 SDK 버전에 묶이지 않게 하기 위해서다 —
    검증 대상은 우리 계층의 순서와 정리이지 SDK 바인딩이 아니다.
    """

    def __init__(self) -> None:
        self.containers: list[str] = []

    def ensure_image(self, image: str) -> None:
        if not docker("images", "-q", image, check=False):
            raise ImageMissingError(image)

    def ensure_network(self, name: str, *, internal: bool = False) -> None:
        """SdkDockerClient 와 같은 계약 — 없으면 만들고, 있는데 격리가 다르면 거부한다."""
        existing = docker("network", "inspect", "-f", "{{.Internal}}", name, check=False).strip()
        if existing not in ("true", "false"):
            flags = ["--internal"] if internal else []
            docker("network", "create", "--driver", "bridge", *flags, name)
            return
        if (existing == "true") != internal:
            raise NetworkIsolationError(name, expected=internal, actual=existing == "true")

    def networks_of(self, container_id: str) -> tuple[str, ...]:
        raw = docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container_id)
        return tuple(json.loads(raw))

    def default_runtime(self) -> str | None:
        return docker("info", "-f", "{{.DefaultRuntime}}").strip() or None

    def hardening_of(self, container_id: str) -> dict[str, Any]:
        raw = docker(
            "inspect",
            "-f",
            "{{json .HostConfig}}\n{{json .Config.User}}\n{{json .Mounts}}\n{{json .Image}}",
            container_id,
        )
        host_json, user_json, mounts_json, image_json = raw.splitlines()
        host = json.loads(host_json)
        # SdkDockerClient 와 같은 모양 — 마운트 대조가 두 구현에서 같은 값을 읽는다 (#488)
        return {
            **host,
            "User": json.loads(user_json),
            "Mounts": json.loads(mounts_json) or [],
            "Tmpfs": host.get("Tmpfs") or {},
            "Image": json.loads(image_json),
        }

    def image_volumes(self, image_id: str) -> set[str]:
        raw = docker("image", "inspect", "-f", "{{json .Config.Volumes}}", image_id)
        return set(json.loads(raw) or {})

    def volume_of(self, name: str) -> dict[str, Any]:
        attrs = json.loads(docker("volume", "inspect", "-f", "{{json .}}", name))
        return {"Driver": attrs.get("Driver"), "Options": attrs.get("Options") or {}}

    def address_of(self, container_id: str, network: str) -> str:
        raw = docker("inspect", "-f", "{{json .NetworkSettings.Networks}}", container_id)
        address = (json.loads(raw).get(network) or {}).get("IPAddress") or ""
        if not address:
            raise LookupError(f"container has no address on network {network}")
        return address

    def create(self, **kwargs: Any) -> str:
        args = [
            "create",
            "--name",
            kwargs["name"],
            "--network",
            kwargs["network"],
            "--read-only",
            "--user",
            kwargs["user"],
            f"--pids-limit={kwargs['pids_limit']}",
            "--memory",
            str(kwargs["mem_limit"]),
            # 계약 테스트가 잡았다: CPU 상한을 빼먹어 NanoCpus=0 이었다 (#243).
            # 참조 구현이 상한을 버리면 그것으로 검증한 E2E 는 CPU 를 한 번도 걸어 본 적이 없다
            f"--cpus={kwargs['nano_cpus'] / 1_000_000_000}",
            "-P",
        ]
        if kwargs.get("init"):
            args.append("--init")
        if kwargs.get("runtime"):
            args.extend(["--runtime", kwargs["runtime"]])
        for capability in kwargs.get("cap_drop", ()):
            args.append(f"--cap-drop={capability}")
        for path, options in kwargs.get("tmpfs", {}).items():
            args.append(f"--tmpfs={path}:{options}" if options else f"--tmpfs={path}")
        # SDK 의 `volumes` 와 같은 뜻 — 참조 구현이 마운트를 버리면 대조를 검증하지 못한다 (#488)
        volumes = kwargs.get("volumes", {})
        # SDK 처럼 목록 형태도 받는다 — `"/extra"` 하나면 익명 볼륨이다
        if isinstance(volumes, list):
            for entry in volumes:
                args.extend(["-v", entry])
        else:
            for source, target in volumes.items():
                args.extend(["-v", f"{source}:{target['bind']}:{target['mode']}"])
        for key, value in kwargs.get("environment", {}).items():
            args.extend(["-e", f"{key}={value}"])
        for key, value in kwargs.get("labels", {}).items():
            args.extend(["--label", f"{key}={value}"])
        for option in kwargs.get("security_opt", ()):
            args.append(f"--security-opt={option}")
        # SDK 의 `devices` 와 같은 뜻 — 버리면 디바이스 대조를 실 daemon 으로 검증하지 못한다 (#510)
        for device in kwargs.get("devices") or ():
            args.append(f"--device={device}")
        for rule in kwargs.get("device_cgroup_rules") or ():
            args.append(f"--device-cgroup-rule={rule}")
        # `--gpus` 는 DeviceRequest 의 일부만 옮긴다 — 다르게 옮기느니 받지 않는다
        if kwargs.get("device_requests"):
            raise NotImplementedError("device_requests cannot be passed through the docker cli")
        policy = kwargs.get("restart_policy")
        if policy:
            retries = policy.get("MaximumRetryCount")
            args.append(f"--restart={policy['Name']}" + (f":{retries}" if retries else ""))
        args.append(kwargs["image"])

        container_id = docker(*args)
        self.containers.append(container_id)
        return container_id

    def start(self, container_id: str) -> None:
        docker("start", container_id)

    def inspect(self, container_id: str) -> dict[str, Any]:
        raw = docker("inspect", "--format", "{{json .State}}", container_id)
        state: dict[str, Any] = json.loads(raw)
        return state

    def port_of(self, container_id: str, container_port: int) -> int:
        mapping = docker("port", container_id, f"{container_port}/tcp")
        return int(mapping.splitlines()[0].rsplit(":", 1)[-1])

    def stop(self, container_id: str, *, timeout_s: float) -> None:
        docker("stop", "-t", str(int(timeout_s)), container_id)

    def remove(self, container_id: str) -> None:
        docker("rm", "-f", "-v", container_id, check=False)  # -v: 익명 볼륨도 (#511)

    def build(self, context: str, tag: str, *, buildargs: dict[str, str] | None = None) -> str:
        args = ["build", "-t", tag]
        for key, value in (buildargs or {}).items():
            args += ["--build-arg", f"{key}={value}"]
        return docker(*args, context)

    def connect(self, network: str, container: str, *, aliases: tuple[str, ...] = ()) -> None:
        if network in self.networks_of(container):
            return
        flags = [f"--alias={alias}" for alias in aliases]
        docker("network", "connect", *flags, network, container)

    def disconnect(self, network: str, container: str) -> None:
        if not docker("inspect", "-f", "{{.Id}}", container, check=False):
            return
        if network in self.networks_of(container):
            docker("network", "disconnect", network, container)

    def remove_network(self, name: str) -> None:
        if docker("network", "inspect", "-f", "{{.Id}}", name, check=False):
            docker("network", "rm", name)

    def labeled(self, labels: dict[str, str]) -> tuple[str, ...]:
        filters = [f"--filter=label={key}={value}" for key, value in labels.items()]
        return tuple(docker("ps", "-aq", "--no-trunc", *filters).split())

    def labels_of(self, container_id: str) -> dict[str, str]:
        labels: dict[str, str] = json.loads(
            docker("inspect", "--format", "{{json .Config.Labels}}", container_id) or "{}"
        )
        return labels or {}

    def find(self, name: str) -> str | None:
        found = docker("inspect", "--format", "{{.Id}}", name, check=False)
        return found or None


def fetch(url: str, body: dict | None = None, *, timeout: float = 10.0) -> dict:
    """루프백 Control API 호출 — blocking 이므로 async 경로에서는 to_thread 로 감싼다."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(  # noqa: S310 — 루프백 고정 URL
        url,
        data=data,
        headers={"content-type": "application/json", "authorization": f"Bearer {TOKEN}"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        result: dict = json.loads(response.read())
        return result


class HttpProbe:
    """Control API 를 직접 호출하는 probe."""

    def __init__(self, port: int) -> None:
        self._port = port

    async def health(self) -> HealthStatus:
        payload = await asyncio.to_thread(
            fetch, f"http://127.0.0.1:{self._port}/v1/health", timeout=5.0
        )
        return HealthStatus.model_validate(payload)


@pytest.fixture
def engine(echo_image: str) -> Iterator[tuple[DockerEngine, CliDockerClient]]:
    """엔진 — finalizer 가 컨테이너와 네트워크를 반드시 정리한다."""
    client = CliDockerClient()
    engine = DockerEngine(client=client, network=TEST_NETWORK, stop_grace_s=5.0)
    try:
        yield engine, client
    finally:
        for container_id in client.containers:
            docker("rm", "-f", "-v", container_id, check=False)
        docker("network", "rm", TEST_NETWORK, check=False)


def echo_spec(*, image: str | None = None, **overrides: Any):
    """echo 에이전트의 컨테이너 스펙.

    ``image`` 는 manifest 의 ``runtime.image`` 를 갈아끼운다 —
    ``base_image`` 는 manifest 가 이미지를 선언하지 않았을 때의 fallback 이라,
    그쪽으로 넘기면 echo manifest 의 선언에 가려 무시된다.
    """
    document = yaml.safe_load(ECHO_MANIFEST.read_text(encoding="utf-8"))
    if image is not None:
        document["spec"]["runtime"]["image"] = image

    manifest = AgentManifest.model_validate(document)
    kwargs: dict[str, Any] = {
        "env": {"AAESTRO_EXECUTOR": "echo", "AAESTRO_AGENT_TOKEN": TOKEN},
        "network": TEST_NETWORK,
    }
    kwargs.update(overrides)
    spec = build_container_spec(manifest, **kwargs)
    return dataclasses.replace(spec, name=f"{spec.name}-{RUN}")


async def wait_ready(monitor: HealthMonitor, *, attempts: int = 60) -> AgentState:
    """Ready 로 전환될 때까지 확인한다."""
    state = AgentState.STARTING
    for _ in range(attempts):
        state = await monitor.check_once()
        if monitor.consecutive_failures == 0:
            return state
        await asyncio.sleep(1)
    return state


def ready_lifecycle() -> AgentLifecycle:
    lifecycle = AgentLifecycle(agent="echo")
    lifecycle.transition(AgentState.BUILT)
    lifecycle.transition(AgentState.STARTING)
    lifecycle.transition(AgentState.READY)
    return lifecycle


@requires_docker
async def test_full_lifecycle_start_health_invoke_stop(engine):
    """start → health ready → invoke → stop 전 주기."""
    docker_engine, client = engine

    handle = await docker_engine.start(echo_spec())
    assert isinstance(handle, ContainerHandle)

    # no-new-privileges 를 켠 채로 agentd 가 정상 기동해야 한다 (#475)
    security_opt = docker("inspect", "-f", "{{json .HostConfig.SecurityOpt}}", handle.container_id)
    assert json.loads(security_opt) == ["no-new-privileges:true"]

    monitor = HealthMonitor(
        agent="echo",
        probe=HttpProbe(handle.control_port),
        lifecycle=ready_lifecycle(),
        timeout_s=5.0,
    )
    assert await wait_ready(monitor) is AgentState.READY

    result = await asyncio.to_thread(
        fetch,
        f"http://127.0.0.1:{handle.control_port}/v1/invoke",
        {
            "task_id": "t-1",
            "run_id": "direct-1",
            "node_id": None,
            "input": {"msg": "hi"},
            "trace": {"trace_id": "tr-1"},
        },
    )
    assert result["output"] == {"msg": "hi"}

    await docker_engine.stop(handle)

    with pytest.raises((urllib.error.URLError, OSError)):
        await asyncio.to_thread(
            fetch, f"http://127.0.0.1:{handle.control_port}/v1/health", timeout=3.0
        )


@requires_docker
async def test_missing_image_is_rt_004(engine):
    """존재하지 않는 이미지는 기동 전에 걸러진다."""
    docker_engine, _ = engine

    with pytest.raises(AaestroError) as exc_info:
        await docker_engine.start(echo_spec(image="aaestro/absent:9.9.9"))

    assert exc_info.value.code == "RT_004"


@requires_docker
async def test_stop_removes_the_container(engine):
    """정리 후 잔여물이 없어야 한다."""
    docker_engine, _ = engine
    handle = await docker_engine.start(echo_spec())

    await docker_engine.stop(handle)

    assert docker("ps", "-aq", "--filter", f"id={handle.container_id}", check=False) == ""


@requires_docker
async def test_health_reports_healthy_through_the_monitor(engine):
    """HealthMonitor 가 실제 Control API 로부터 healthy 를 읽는다."""
    docker_engine, _ = engine
    handle = await docker_engine.start(echo_spec())
    monitor = HealthMonitor(
        agent="echo",
        probe=HttpProbe(handle.control_port),
        lifecycle=ready_lifecycle(),
        timeout_s=5.0,
    )

    await wait_ready(monitor)

    assert monitor.last_status is HealthState.HEALTHY


@requires_docker
async def test_a_stop_right_after_start_ends_on_sigterm_not_the_grace(engine):
    """#570 — agentd 가 PID 1 이면 기동 중 SIGTERM 이 무시돼 정지가 유예 (30초) 를 다 썼다.

    기본 유예로 띄워 1초 뒤 정지한다. init (tini) 이 SIGTERM 을 전달하면 유예보다 훨씬 짧게,
    SIGKILL (137) 이 아닌 종료 코드로 끝난다.
    """
    _, client = engine
    default_grace = DockerEngine(client=client, network=TEST_NETWORK)
    assert default_grace.stop_grace_s == DEFAULT_STOP_GRACE_S
    handle = await default_grace.start(echo_spec())
    await asyncio.sleep(1)
    # 정지 실패는 `stop` 이 WARN 으로 삼킨다 — 상태로 정지 전후를 증명한다
    before = json.loads(docker("inspect", "-f", "{{json .State}}", handle.container_id))
    assert before["Running"] is True, before

    began = time.monotonic()
    await default_grace.stop(handle, remove=False)
    elapsed_s = time.monotonic() - began

    state = json.loads(docker("inspect", "-f", "{{json .State}}", handle.container_id))
    assert elapsed_s < STOP_AFTER_START_LIMIT_S, elapsed_s
    assert (state["Running"], state["Status"]) == (False, "exited"), state
    assert state["ExitCode"] in GRACEFUL_EXIT_CODES, state
    assert state["OOMKilled"] is False, state


# Docker 없이 도는 참조 클라이언트 배선 확인 — 단위 테스트에 두면 이 모듈 import 가
# daemon 을 조회한다 (#527)
def test_the_cli_reference_client_passes_the_runtime_to_docker_create(monkeypatch):
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(f"{__name__}.docker", lambda *args, **_: calls.append(args) or "cid")
    kwargs = {**build_container_spec(make_manifest()).to_docker_kwargs(), "runtime": "runsc"}

    CliDockerClient().create(**kwargs)

    args = calls[0]
    assert args[args.index("--runtime") + 1] == "runsc"
