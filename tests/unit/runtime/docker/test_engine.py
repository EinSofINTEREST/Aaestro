"""Unit tests for the Docker SDK call layer.

daemon 없이 검증한다 — 이 계층의 계약은 에러 변환과 정리 보장이지
Docker 그 자체가 아니다.
"""

from __future__ import annotations

import asyncio
import threading

import pytest

from aaestro.core.errors import AaestroError, ErrorCategory
from aaestro.runtime.docker.engine import (
    ContainerHandle,
    DockerClient,
    DockerEngine,
    agent_of,
    control_port_of,
)
from aaestro.runtime.spec import build_container_spec
from tests.fixtures.builders import make_manifest
from tests.fixtures.fake_docker import FakeDockerClient, inspect_mounts
from tests.fixtures.waiting import until


def spec(**overrides):
    """manifest 에서 파생한 컨테이너 스펙."""
    return build_container_spec(make_manifest(**overrides))


def engine(client: FakeDockerClient | None = None) -> DockerEngine:
    return DockerEngine(client=client or FakeDockerClient())


# --- 스펙 해석 ----------------------------------------------------------------


def test_agent_name_comes_from_the_label():
    """라벨이 곧 소유자 표시다 — 별도 필드를 만들지 않는다."""
    assert agent_of(spec()) == "test-agent"


def test_control_port_is_found_by_name():
    assert control_port_of(spec()) == 8080


def test_spec_without_a_control_port_is_rejected():
    """control 포트가 없으면 runtime 이 에이전트에 닿을 수 없다."""
    without = spec()
    stripped = type(without)(**{**without.__dict__, "ports": ()})

    with pytest.raises(AaestroError) as exc_info:
        control_port_of(stripped)

    assert exc_info.value.code == "RT_001"
    # 결정적 계약 위반이다 — 재시도 가능으로 표시하면 재시작 루프가 돈다
    assert exc_info.value.retryable is False


def test_fake_client_satisfies_the_contract():
    assert isinstance(FakeDockerClient(), DockerClient)


# --- 기동 --------------------------------------------------------------------


async def test_start_creates_and_starts_the_container():
    client = FakeDockerClient()

    handle = await engine(client).start(spec())

    assert handle.agent == "test-agent"
    assert client.started == [handle.container_id]
    assert handle.control_port == 49152


async def test_start_uses_the_declared_network():
    """에이전트는 전용 bridge 에만 붙는다."""
    client = FakeDockerClient()

    await engine(client).start(spec())

    assert client.networks == ["aaestro-net"]
    assert client.created[0]["network"] == "aaestro-net"


async def test_an_isolated_engine_publishes_nothing_and_is_reached_inside_the_network():
    """내부 네트워크에는 게시가 먹지 않는다 — control plane 은 네트워크 안 주소로 닿는다 (#280)."""
    client = FakeDockerClient(address="172.30.0.7")

    handle = await DockerEngine(client=client, network="agents", internal=True).start(spec())

    assert client.internal_requests == [True]
    assert client.created[0]["ports"] == {}
    assert client.addressed == [(handle.container_id, "agents")]
    assert (handle.control_host, handle.control_port) == ("172.30.0.7", 8080)
    assert handle.control_url == "http://172.30.0.7:8080"


async def test_extra_networks_are_joined_before_the_container_starts():
    """프록시 없는 배포의 사이드카 네트워크 (#304) — 기동 전에 붙어야 agentd 가 세션을 연다."""
    order: list[str] = []

    class Ordered(FakeDockerClient):
        def connect(self, network, container, *, aliases=()):
            order.append(f"connect {network}")
            super().connect(network, container, aliases=aliases)

        def start(self, container_id):
            order.append("start")
            super().start(container_id)

    client = Ordered()
    manifest = make_manifest()
    handle = await engine(client).start(
        build_container_spec(manifest, extra_networks=("aaestro-test-agent--mcp",))
    )

    assert client.connected == [("aaestro-test-agent--mcp", handle.container_id, ())]
    assert order == ["connect aaestro-test-agent--mcp", "start"]


async def test_an_isolated_agent_never_joins_another_network():
    """격리된 에이전트가 다른 네트워크에 붙으면 그 길로 프록시를 건너뛴다 (#304)."""
    client = FakeDockerClient()
    isolated = DockerEngine(client=client, network="agents", internal=True)

    with pytest.raises(AaestroError) as caught:
        await isolated.start(build_container_spec(make_manifest(), extra_networks=("side",)))

    assert caught.value.code == "RT_001"
    assert client.created == [] and client.connected == []


def test_extra_networks_cannot_be_shared_or_host_networks():
    with pytest.raises(AaestroError) as caught:
        build_container_spec(make_manifest(), extra_networks=("host",))

    assert caught.value.code == "RT_001"


async def test_a_plain_engine_publishes_on_loopback_and_asks_for_a_normal_network():
    client = FakeDockerClient(host_port=49153)

    handle = await engine(client).start(spec())

    assert client.internal_requests == [False]
    assert client.created[0]["ports"] == {"8080/tcp": ("127.0.0.1", None)}
    assert client.addressed == []
    assert handle.control_url == "http://127.0.0.1:49153"


async def test_a_network_with_the_wrong_isolation_is_refused_before_creating_anything():
    from aaestro.runtime.docker.errors import NetworkIsolationError

    client = FakeDockerClient(
        network_error=NetworkIsolationError("agents", expected=True, actual=False)
    )

    with pytest.raises(AaestroError) as exc_info:
        await DockerEngine(client=client, network="agents", internal=True).start(spec())

    assert (exc_info.value.code, exc_info.value.retryable) == ("RT_001", False)
    assert exc_info.value.details["network"] == "agents"
    assert client.created == []


async def test_missing_image_is_rt_004():
    """이미지 문제는 설정 문제 — 재시도해도 같다."""
    client = FakeDockerClient(image_error=RuntimeError("not found"))

    with pytest.raises(AaestroError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_004"
    assert exc_info.value.category is ErrorCategory.RUNTIME
    assert exc_info.value.retryable is False
    assert exc_info.value.details["image"]


async def test_create_failure_is_retryable_rt_001():
    """일시적 자원 부족일 수 있으므로 재시도 가능하다."""
    client = FakeDockerClient(create_error=RuntimeError("no space"))

    with pytest.raises(AaestroError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_001"
    assert exc_info.value.retryable is True


async def test_network_failure_is_rt_001():
    client = FakeDockerClient(network_error=RuntimeError("network down"))

    with pytest.raises(AaestroError) as exc_info:
        await engine(client).start(spec())

    assert exc_info.value.code == "RT_001"


async def test_start_failure_discards_the_half_built_container():
    """반쯤 만들어진 컨테이너를 남기면 유령 컨테이너가 쌓인다."""
    client = FakeDockerClient(start_error=RuntimeError("boom"))

    with pytest.raises(AaestroError):
        await engine(client).start(spec())

    assert len(client.removed) == 1


class GatedClient(FakeDockerClient):
    """지정한 단계를 스레드 안에서 문 앞에 붙잡는다 — 취소돼도 스레드는 문이 열리면 끝까지 간다."""

    def __init__(self, step: str) -> None:
        super().__init__()
        self.step = step
        self.gate = threading.Event()
        self.entered = threading.Event()

    def _hold(self, step: str) -> None:
        if step == self.step:
            self.entered.set()
            self.gate.wait(5.0)

    def create(self, **kwargs):
        self._hold("create")
        return super().create(**kwargs)

    def start(self, container_id: str) -> None:
        self._hold("start")
        super().start(container_id)


@pytest.mark.parametrize("step", ["create", "start"])
async def test_a_cancelled_start_discards_the_container_its_thread_built(step: str):
    """#482 — 취소는 스레드를 멈추지 못한다: 끝까지 만든 컨테이너를 아무도 지우지 않았다."""
    client = GatedClient(step)
    docker = engine(client)
    starting = asyncio.create_task(docker.start(spec()))
    await until(client.entered.is_set)

    starting.cancel()
    await asyncio.sleep(0)
    assert not starting.done(), "기동이 스레드를 기다리지 않고 취소를 올렸다"
    client.gate.set()
    with pytest.raises(asyncio.CancelledError):
        await starting

    assert len(client.created) == 1
    assert client.removed == ["container-0001" + "0" * 20]
    assert docker.started == {}


async def test_a_cancelled_start_hands_a_hung_cleanup_to_the_engine():
    """#482 — 걸린 Docker 호출을 그 자리에서 끝없이 기다리면 정지가 묶인다: 상한을 넘기면 넘긴다."""
    client = GatedClient("create")
    docker = DockerEngine(client=client, cancel_wait_s=0.01)
    starting = asyncio.create_task(docker.start(spec()))
    await until(client.entered.is_set)

    try:
        starting.cancel()
        await until(starting.done, timeout_s=1.0)
        assert starting.cancelled()
        assert docker._cleanups, "넘긴 정리를 엔진이 쥐지 않았다"
    finally:
        client.gate.set()

    await docker.settle_cleanups()
    assert client.removed == ["container-0001" + "0" * 20]
    assert not docker._cleanups


async def test_settling_abandons_a_cleanup_that_never_finishes():
    """종료는 끝없이 기다리지 않는다 — 상한을 넘긴 정리는 끊고 넘어간다."""
    client = GatedClient("create")
    docker = DockerEngine(client=client, cancel_wait_s=0.01)
    starting = asyncio.create_task(docker.start(spec()))
    await until(client.entered.is_set)

    try:
        starting.cancel()
        await until(starting.done, timeout_s=1.0)
        await docker.settle_cleanups(timeout_s=0.01)
        assert not docker._cleanups
        assert client.removed == []
    finally:
        client.gate.set()


async def test_failed_start_is_not_tracked():
    client = FakeDockerClient(start_error=RuntimeError("boom"))
    docker = engine(client)

    with pytest.raises(AaestroError):
        await docker.start(spec())

    assert docker.started == {}


# --- OOM 감지 -----------------------------------------------------------------


async def test_oom_kill_is_rt_003():
    """OOM 은 메모리 상한을 올리지 않으면 재시도해도 같은 결과다."""
    client = FakeDockerClient(state={"OOMKilled": True, "ExitCode": 137})
    docker = engine(client)
    handle = await docker.start(spec())

    with pytest.raises(AaestroError) as exc_info:
        await docker.check_exit(handle)

    assert exc_info.value.code == "RT_003"
    assert exc_info.value.retryable is False


async def test_oom_exit_code_alone_is_detected():
    """OOMKilled 플래그가 없어도 137 은 OOM 으로 본다."""
    client = FakeDockerClient(state={"OOMKilled": False, "ExitCode": 137})
    docker = engine(client)
    handle = await docker.start(spec())

    with pytest.raises(AaestroError) as exc_info:
        await docker.check_exit(handle)

    assert exc_info.value.code == "RT_003"


async def test_clean_exit_is_not_an_error():
    client = FakeDockerClient(state={"OOMKilled": False, "ExitCode": 0})
    docker = engine(client)
    handle = await docker.start(spec())

    await docker.check_exit(handle)


# --- drain -------------------------------------------------------------------


async def test_drain_returns_when_work_finishes():
    docker = engine()
    handle = await docker.start(spec())
    remaining = [2, 1, 0]
    drained = []

    async def sleep(_delay: float) -> None:
        remaining.pop(0)

    await docker.drain(
        handle,
        request_drain=lambda: drained.append(True),
        inflight=lambda: remaining[0],
        sleep=sleep,
        clock=lambda: 0.0,
    )

    assert drained == [True]


async def test_drain_awaits_an_async_request():
    docker = engine()
    handle = await docker.start(spec())
    called = []

    async def request() -> None:
        called.append(True)

    await docker.drain(handle, request_drain=request, inflight=lambda: 0, clock=lambda: 0.0)

    assert called == [True]


async def test_drain_timeout_is_rt_005():
    """남은 태스크를 조용히 버리지 않는다."""
    docker = engine()
    handle = await docker.start(spec())
    ticks = iter([0.0, 10.0, 40.0, 80.0])

    async def sleep(_delay: float) -> None:
        pass

    with pytest.raises(AaestroError) as exc_info:
        await docker.drain(
            handle,
            request_drain=lambda: None,
            inflight=lambda: 1,
            timeout_s=30.0,
            sleep=sleep,
            clock=lambda: next(ticks),
        )

    assert exc_info.value.code == "RT_005"
    assert exc_info.value.details["inflight"] == 1


# --- 정지 --------------------------------------------------------------------


async def test_stop_sends_the_grace_period():
    client = FakeDockerClient()
    docker = engine(client)
    handle = await docker.start(spec())

    assert await docker.stop(handle) is True

    assert client.stopped == [(handle.container_id, 30.0)]
    assert client.removed == [handle.container_id]


async def test_stop_untracks_the_agent():
    docker = engine()
    handle = await docker.start(spec())

    await docker.stop(handle)

    assert docker.started == {}


async def test_stop_survives_a_failing_stop():
    """정지 실패가 정리를 막으면 유령 컨테이너가 남는다."""
    client = FakeDockerClient(stop_error=RuntimeError("already gone"))
    docker = engine(client)
    handle = await docker.start(spec())

    # 정지가 실패해도 제거가 됐으면 제거됨이다 — 호출자는 제거 결과만 본다
    assert await docker.stop(handle) is True

    assert client.removed == [handle.container_id]


async def test_stop_survives_a_failing_removal():
    client = FakeDockerClient(remove_error=RuntimeError("in use"))
    docker = engine(client)
    handle = await docker.start(spec())

    # 제거 실패는 삼키되 False 로 알린다 — 호출자가 같은 handle 로 다시 지울 수 있게 (#491)
    assert await docker.stop(handle) is False

    assert docker.started == {}


async def test_stop_can_keep_the_container():
    client = FakeDockerClient()
    docker = engine(client)
    handle = await docker.start(spec())

    assert await docker.stop(handle, remove=False) is False

    assert client.removed == []


def test_handle_short_id_is_truncated():
    handle = ContainerHandle(
        agent="a", container_id="0123456789abcdef0123", image="i", control_port=1
    )

    assert handle.short_id == "0123456789ab"


async def test_drain_awaits_a_future_request():
    """coroutine 이 아닌 awaitable 도 기다려야 한다 — 건너뛰면 에이전트가
    아직 태스크를 받는 중인데 drain 이 진행된다."""
    docker = engine()
    handle = await docker.start(spec())
    completed = []

    def request():
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        future.set_result(None)
        completed.append(True)
        return future

    await docker.drain(handle, request_drain=request, inflight=lambda: 0, clock=lambda: 0.0)

    assert completed == [True]


async def test_removal_failure_log_carries_agent_and_image(monkeypatch):
    """제거 실패 로그에 소유자와 이미지가 없으면 사고 추적이 끊긴다 (05 필수 필드)."""
    recorded: list[dict] = []

    def capture(_event: str, **fields: object) -> None:
        recorded.append(dict(fields))

    from aaestro.runtime.docker import engine as engine_module

    monkeypatch.setattr(engine_module.log, "warning", capture)

    client = FakeDockerClient(start_error=RuntimeError("boom"), remove_error=RuntimeError("in use"))

    with pytest.raises(AaestroError):
        await engine(client).start(spec())

    assert recorded[-1]["agent"] == "test-agent"
    assert recorded[-1]["image"]
    assert recorded[-1]["container_id"]


async def test_drain_success_log_includes_the_image(monkeypatch):
    """컨테이너 조작 로그는 container_id 와 image 를 모두 실어야 한다."""
    recorded: list[dict] = []

    def capture(_event: str, **fields: object) -> None:
        recorded.append(dict(fields))

    from aaestro.runtime.docker import engine as engine_module

    monkeypatch.setattr(engine_module.log, "info", capture)

    docker = engine()
    handle = await docker.start(spec())
    recorded.clear()

    await docker.drain(handle, request_drain=lambda: None, inflight=lambda: 0, clock=lambda: 0.0)

    assert recorded[-1]["image"] == handle.image
    assert recorded[-1]["container_id"] == handle.short_id


DEFAULT_MASKED_PATHS = ["/proc/kcore", "/proc/keys", "/sys/firmware"]
DEFAULT_READONLY_PATHS = ["/proc/bus", "/proc/sys"]

GOOD_HARDENING = {
    "CapDrop": ["ALL"],
    "ReadonlyRootfs": True,
    "User": "1000:1000",
    "SecurityOpt": ["no-new-privileges:true"],  # 에이전트 기본 스펙이 싣는 HARDENED_SECURITY_OPT
    "PidsLimit": 256,
    # 에이전트 기본 스펙의 tmpfs 두 경로 — `--tmpfs` 는 `Mounts` 에 보이지 않는다
    "Mounts": [],
    "Tmpfs": {"/tmp": "mode=1777", "/workspace": "mode=1777"},  # noqa: S108 — 컨테이너 안 경로다
}


def empty_opt_expected():
    """security_opt 를 싣지 않는 스펙의 기대값 — 빈 기대값 성질을 명시적으로 검사한다."""
    return {**spec().hardening_kwargs(), "security_opt": []}


async def verify(
    hardening, *, internal: bool = False, expected=None, volumes=None, images=None
) -> None:
    client = FakeDockerClient(hardening=hardening, volumes=volumes, image_volumes=images)
    engine = DockerEngine(client=client, network="agents", internal=internal)
    wanted = spec().hardening_kwargs() if expected is None else expected
    await engine.verify_isolation("alpha", "img:1", "container-0001", wanted)


async def test_verify_isolation_accepts_a_hardened_container():
    await verify(GOOD_HARDENING)
    await verify(GOOD_HARDENING, internal=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("CapDrop", None),
        ("CapDrop", ["NET_RAW"]),
        ("ReadonlyRootfs", False),
        ("User", ""),
        ("User", "0:0"),
    ],
)
@pytest.mark.parametrize("internal", [False, True])
async def test_verify_isolation_refuses_a_container_that_lost_its_hardening(field, value, internal):
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, field: value}, internal=internal)

    assert caught.value.code == "RT_001"
    assert caught.value.retryable is False
    assert caught.value.details["fields"] == [field]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("Privileged", True),
        ("CapAdd", ["SYS_ADMIN"]),
        ("PidMode", "host"),
        ("IpcMode", "host"),
        ("UsernsMode", "host"),
        ("NetworkMode", "host"),
        ("PidMode", "container:abc123"),
        ("IpcMode", "container:abc123"),
        ("NetworkMode", "container:abc123"),
        ("UsernsMode", "container:abc123"),
        # systempaths=unconfined 는 SecurityOpt 가 아니라 여기에 드러난다 (#489 QA 실측)
        ("MaskedPaths", []),
        ("ReadonlyPaths", []),
    ],
)
@pytest.mark.parametrize("internal", [False, True])
async def test_verify_isolation_refuses_settings_that_void_the_hardening(field, value, internal):
    """CapDrop 이 ALL 이어도 privileged 면 모든 capability 가 주어진다 — 부류로 거절한다."""
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, field: value}, internal=internal)

    assert caught.value.details["fields"] == [field]


async def test_verify_isolation_accepts_the_values_docker_reports_for_a_plain_container():
    await verify(
        {
            **GOOD_HARDENING,
            "Privileged": False,
            "CapAdd": None,
            "PidMode": "",
            "IpcMode": "private",
            "UsernsMode": "",
            "NetworkMode": "agents",
            "MaskedPaths": DEFAULT_MASKED_PATHS,
            "ReadonlyPaths": DEFAULT_READONLY_PATHS,
        }
    )


@pytest.mark.parametrize("security_opt", [None, [], ["seccomp=unconfined"]])
async def test_verify_isolation_refuses_a_container_missing_a_security_opt_the_spec_now_sets(
    security_opt,
):
    """에이전트 기본 스펙이 싣는 no-new-privileges 가 빠진 컨테이너는 거절된다."""
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, "SecurityOpt": security_opt})

    assert caught.value.details["fields"] == ["SecurityOpt"]
    # 그 값을 실은 정상 컨테이너는 붙는다
    await verify({**GOOD_HARDENING, "SecurityOpt": ["no-new-privileges:true"]})


async def test_verify_isolation_refuses_default_no_new_privileges_overridden_to_false():
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, "SecurityOpt": ["no-new-privileges:false"]})

    assert caught.value.details["fields"] == ["SecurityOpt"]


@pytest.mark.parametrize("security_opt", [None, []])
async def test_verify_isolation_accepts_no_security_opt_while_the_spec_sets_none(security_opt):
    await verify({**GOOD_HARDENING, "SecurityOpt": security_opt}, expected=empty_opt_expected())


@pytest.mark.parametrize(
    "security_opt",
    [
        ["no-new-privileges:true"],  # 기대 () 에 더해진 항목 — 기대 밖 키
        ["seccomp=unconfined"],
        ['seccomp={"defaultAction":"SCMP_ACT_ALLOW"}'],  # 허용 정책 프로필 — unconfined 가 아니다
        ["apparmor=unconfined"],
        ["label=disable"],
    ],
)
async def test_verify_isolation_refuses_a_security_opt_the_spec_did_not_set(security_opt):
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, "SecurityOpt": security_opt}, expected=empty_opt_expected())

    assert caught.value.details["fields"] == ["SecurityOpt"]


SIDECAR_OPTS = ["no-new-privileges:true"]


@pytest.mark.parametrize(
    "security_opt",
    [
        ["no-new-privileges:true", "no-new-privileges:false"],  # Docker 는 마지막 값을 쓴다
        ["no-new-privileges:true", "seccomp=unconfined"],
        ["no-new-privileges:true", 'seccomp={"defaultAction":"SCMP_ACT_ALLOW"}'],
        ["no-new-privileges:false"],
        [],
    ],
)
async def test_verify_isolation_refuses_a_security_opt_whose_effective_value_differs(
    security_opt,
):
    expected = {**spec().hardening_kwargs(), "security_opt": SIDECAR_OPTS}
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, "SecurityOpt": security_opt}, expected=expected)

    assert caught.value.details["fields"] == ["SecurityOpt"]


@pytest.mark.parametrize(
    "security_opt",
    [
        ["no-new-privileges:true"],
        ["no-new-privileges=true"],  # 구분자 표기만 다르다
        ["no-new-privileges:false", "no-new-privileges:true"],  # 마지막 값이 기대와 같다
    ],
)
async def test_verify_isolation_accepts_the_same_effective_security_opt(security_opt):
    expected = {**spec().hardening_kwargs(), "security_opt": SIDECAR_OPTS}
    await verify({**GOOD_HARDENING, "SecurityOpt": security_opt}, expected=expected)


async def test_verify_isolation_accepts_security_opts_in_any_order():
    opts = ["no-new-privileges:true", "apparmor=docker-default"]
    expected = {**spec().hardening_kwargs(), "security_opt": opts}
    await verify({**GOOD_HARDENING, "SecurityOpt": list(reversed(opts))}, expected=expected)


@pytest.mark.parametrize("pids_limit", [None, 0, -1, 4096])
async def test_verify_isolation_refuses_a_container_that_lost_its_pid_limit(pids_limit):
    """PID limit 이 빠지거나 바뀐 컨테이너는 fork bomb 방어를 잃은 것이다 (02 Resources)."""
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, "PidsLimit": pids_limit})

    assert caught.value.code == "RT_001"
    assert caught.value.details["fields"] == ["PidsLimit"]


# --- 마운트 대조 (#488) ---------------------------------------------------------

DECLARATION_DIR = "/srv/aaestro/agents/alpha"
MODULES_DIR = "/srv/aaestro/modules/skillsets"


def mounted_spec():
    """runtime 이 거는 읽기 전용 선언 마운트 둘과 쓰기 가능한 이름 있는 볼륨 하나를 가진 스펙."""
    return build_container_spec(
        make_manifest(),
        mounts=[
            {"name": DECLARATION_DIR, "mount_path": "/etc/aaestro", "read_only": True},
            {"name": MODULES_DIR, "mount_path": "/opt/modules/skillsets", "read_only": True},
            {"name": "alpha-cache", "mount_path": "/cache", "read_only": False},
        ],
    )


def mounted_found(**overrides):
    """그 스펙으로 만든 정상 컨테이너의 inspect 값."""
    built = mounted_spec()
    return {**GOOD_HARDENING, **inspect_mounts(built.to_docker_kwargs()), **overrides}


def bind(source, destination, *, rw=True):
    return {"Type": "bind", "Source": source, "Destination": destination, "RW": rw}


async def test_verify_isolation_accepts_the_mounts_its_spec_declared():
    found = mounted_found()
    await verify(found, expected=mounted_spec().hardening_kwargs())
    # 순서는 뜻이 없다
    reordered = {**found, "Mounts": list(reversed(found["Mounts"]))}
    await verify(reordered, expected=mounted_spec().hardening_kwargs())


def _declared():
    return mounted_found()["Mounts"]


@pytest.mark.parametrize(
    ("label", "mounts"),
    [
        ("docker socket", [*_declared(), bind("/var/run/docker.sock", "/var/run/docker.sock")]),
        ("host path", [*_declared(), bind("/home/someone", "/data", rw=False)]),
        ("shared named volume", [*_declared(), {"Type": "volume", "Name": "beta-cache",
                                                "Destination": "/beta", "RW": True}]),
        ("declaration made writable", [{**_declared()[0], "RW": True}, *_declared()[1:]]),
        ("declaration from another host path", [{**_declared()[0], "Source": "/"},
                                                *_declared()[1:]]),
        ("volume swapped for another agent's", [*_declared()[:2], {**_declared()[2],
                                                                    "Name": "beta-cache"}]),
        ("declared mount missing", _declared()[1:]),
        ("no mounts reported", None),
    ],
)  # fmt: skip
async def test_verify_isolation_refuses_mounts_its_spec_did_not_declare(label, mounts):
    with pytest.raises(AaestroError) as caught:
        await verify(mounted_found(Mounts=mounts), expected=mounted_spec().hardening_kwargs())

    assert caught.value.code == "RT_001", label
    assert caught.value.retryable is False
    assert caught.value.details["fields"] == ["Mounts"]
    # 사유에는 필드 이름만 — 호스트 경로를 싣지 않는다
    assert "/var/run" not in str(caught.value) + str(caught.value.details)
    assert "/home" not in str(caught.value) + str(caught.value.details)


def volume_spec():
    """매니페스트가 `work` 볼륨을 선언한 에이전트 `alpha` 의 스펙."""
    manifest = make_manifest(
        metadata={"name": "alpha"},
        spec={"runtime": {"volumes": [{"name": "work", "mount_path": "/data"}]}},
    )
    return build_container_spec(manifest)


async def test_verify_isolation_accepts_the_agent_namespaced_volume():
    built = volume_spec()
    found = {**GOOD_HARDENING, **inspect_mounts(built.to_docker_kwargs())}

    await verify(found, expected=built.hardening_kwargs())


async def test_verify_isolation_refuses_a_container_on_the_old_unprefixed_volume():
    """접두사 없는 옛 이름 (`work`) 으로 마운트된 기존 컨테이너는 재부착 대조에서 걸린다 (#513)."""
    built = volume_spec()
    old = {**built.to_docker_kwargs(), "volumes": {"work": {"bind": "/data", "mode": "rw"}}}

    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, **inspect_mounts(old)}, expected=built.hardening_kwargs())

    assert caught.value.details["fields"] == ["Mounts"]


@pytest.mark.parametrize(
    ("label", "tmpfs", "extra"),
    [
        ("tmpfs dropped", {"/tmp": "mode=1777"}, []),  # noqa: S108 — 컨테이너 안 경로다
        ("no tmpfs reported", None, []),
        ("extra --tmpfs", {**GOOD_HARDENING["Tmpfs"], "/app": ""}, []),
        ("extra --mount type=tmpfs", GOOD_HARDENING["Tmpfs"],
         [{"Type": "tmpfs", "Source": "", "Destination": "/app", "RW": True}]),
    ],
)  # fmt: skip
async def test_verify_isolation_refuses_tmpfs_its_spec_did_not_declare(label, tmpfs, extra):
    with pytest.raises(AaestroError) as caught:
        await verify({**GOOD_HARDENING, "Tmpfs": tmpfs, "Mounts": extra})

    assert caught.value.details["fields"] == ["Tmpfs"], label


async def test_verify_isolation_accepts_a_tmpfs_reported_through_mounts():
    """`--mount type=tmpfs` 로 건 같은 경로는 `Mounts` 쪽에 보인다 — 같은 tmpfs 집합이다."""
    found = {
        **GOOD_HARDENING,
        "Tmpfs": {"/tmp": "mode=1777"},  # noqa: S108 — 컨테이너 안 경로다
        "Mounts": [{"Type": "tmpfs", "Source": "", "Destination": "/workspace", "RW": True}],
    }
    await verify(found)


ANONYMOUS = "a" * 64


def anonymous(destination, name=ANONYMOUS):
    return {"Type": "volume", "Name": name, "Destination": destination, "RW": True}


IMAGE_ID = "sha256:" + "b" * 64
DATA_IMAGE = {IMAGE_ID: {"/data"}}
"""`VOLUME ["/data"]` 를 선언한 이미지 — 컨테이너 inspect 의 `Image` 가 이 id 를 가리킨다."""


def from_image(**found):
    return {**GOOD_HARDENING, "Image": IMAGE_ID, **found}


async def test_verify_isolation_accepts_an_anonymous_volume_the_image_declared():
    """이미지의 `VOLUME ["/data"]` 에 Docker 가 만든 익명 볼륨은 생성 인자 밖이어도 정상이다."""
    await verify(from_image(Mounts=[anonymous("/data")]), images=DATA_IMAGE)


@pytest.mark.parametrize(
    ("label", "found", "volumes", "images"),
    [
        ("outside the image volumes", from_image(Mounts=[anonymous("/other")]), None, DATA_IMAGE),
        ("image declares no volumes", from_image(Mounts=[anonymous("/data")]), None, None),
        ("no image reported", {**GOOD_HARDENING, "Mounts": [anonymous("/data")]}, None,
         DATA_IMAGE),
        ("named volume at an image volume path",
         from_image(Mounts=[anonymous("/data", "beta-cache")]), None, DATA_IMAGE),
        ("anonymous volume opened onto the host", from_image(Mounts=[anonymous("/data")]),
         {ANONYMOUS: {"Driver": "local", "Options": {"type": "none", "device": "/",
                                                     "o": "bind"}}}, DATA_IMAGE),
        # 확인하지 못한 이미지는 VOLUME 예외를 닫는다 — 이미지 VOLUME 익명 볼륨도 거절된다
        ("image inspect failed", from_image(Mounts=[anonymous("/data")]), None,
         {IMAGE_ID: RuntimeError("image gone")}),
    ],
)  # fmt: skip
async def test_verify_isolation_refuses_volumes_the_image_did_not_declare(
    label, found, volumes, images
):
    with pytest.raises(AaestroError) as caught:
        await verify(found, volumes=volumes, images=images)

    assert caught.value.details["fields"] == ["Mounts"], label


async def test_verify_isolation_refuses_an_anonymous_volume_the_caller_added():
    """생성 때 넘긴 `-v /extra` 도 컨테이너 `Config.Volumes` 에 복사된다 — 허용 근거는 이미지다.

    일반 local 옵션의 64-hex 익명 볼륨이라 이름 · 드라이버로는 이미지 VOLUME 과 구분되지 않는다.
    """
    found = from_image(
        Volumes={"/data": {}, "/extra": {}},  # 컨테이너 Config.Volumes — 대조가 읽지 않는다
        Mounts=[anonymous("/data"), anonymous("/extra", "c" * 64)],
    )
    with pytest.raises(AaestroError) as caught:
        await verify(found, images=DATA_IMAGE)

    assert caught.value.details["fields"] == ["Mounts"]


@pytest.mark.parametrize(
    "volume",
    [
        {"Driver": "local", "Options": {"type": "none", "device": "/", "o": "bind"}},
        {"Driver": "local", "Options": {"device": "/home"}},
        {"Driver": "local", "Options": {"o": "ro,rbind"}},
        {"Driver": "local", "Options": {"o": "bind"}},
        {"Driver": "rexray", "Options": {}},
        RuntimeError("volume inspect failed"),  # 확인하지 못한 볼륨은 통과시키지 않는다
    ],
)
async def test_verify_isolation_refuses_a_declared_volume_that_opens_the_host(volume):
    """같은 이름이어도 bind 옵션으로 다시 만든 볼륨은 호스트 경로를 연다 (#488 QA 실측)."""
    with pytest.raises(AaestroError) as caught:
        await verify(
            mounted_found(), expected=mounted_spec().hardening_kwargs(),
            volumes={"alpha-cache": volume},
        )  # fmt: skip

    assert caught.value.details["fields"] == ["Mounts"]
    assert "/home" not in str(caught.value) + str(caught.value.details)


async def test_a_failed_volume_inspect_is_logged_with_the_agent(monkeypatch):
    """확인하지 못한 볼륨의 WARN 에 runtime 필수 필드 `agent` 가 실린다 (05) — 값은 싣지 않는다."""
    import structlog.testing

    from aaestro.runtime.docker import engine as engine_module

    # 앞선 테스트가 모듈 로거를 굳혀 두면 capture_logs 가 보지 못한다
    monkeypatch.setattr(engine_module, "log", structlog.get_logger("test"))
    with structlog.testing.capture_logs() as logs, pytest.raises(AaestroError):
        await verify(
            mounted_found(),
            expected=mounted_spec().hardening_kwargs(),
            volumes={"alpha-cache": RuntimeError("/home/secret")},
        )

    [warning] = [entry for entry in logs if entry["event"] == "volume inspect failed"]
    assert warning["agent"] == "alpha"
    assert warning["log_level"] == "warning"
    assert "/home/secret" not in str(warning)


@pytest.mark.parametrize(
    "volume",
    [{"Driver": "local", "Options": None}, {"Driver": "local", "Options": {"o": "size=1g"}}],
)
async def test_verify_isolation_accepts_a_plain_local_volume(volume):
    await verify(
        mounted_found(), expected=mounted_spec().hardening_kwargs(),
        volumes={"alpha-cache": volume},
    )  # fmt: skip


async def test_verify_isolation_compares_normalized_paths():
    """Docker 는 `/cache/` · `/opt//x` 를 정규화해 보고한다 — 선언의 표기 차이로 거절하지 않는다."""
    declared = build_container_spec(
        make_manifest(),
        mounts=[
            {"name": f"{DECLARATION_DIR}/", "mount_path": "/etc//aaestro/", "read_only": True},
            {"name": "alpha-cache", "mount_path": "//cache/", "read_only": False},
        ],
    )
    reported = build_container_spec(
        make_manifest(),
        mounts=[
            {"name": DECLARATION_DIR, "mount_path": "/etc/aaestro", "read_only": True},
            {"name": "alpha-cache", "mount_path": "/cache", "read_only": False},
        ],
    )
    expected = {**declared.hardening_kwargs(), "tmpfs": {"/tmp/": "", "//workspace": ""}}  # noqa: S108
    found = {**GOOD_HARDENING, **inspect_mounts(reported.to_docker_kwargs())}

    await verify(found, expected=expected)


def inspect_shape(kwargs):
    """생성 인자를 `docker inspect` 가 보고하는 모양으로 — 정상 컨테이너가 이렇게 보인다."""
    return {
        "CapDrop": kwargs["cap_drop"],
        "ReadonlyRootfs": kwargs["read_only"],
        "User": kwargs["user"],
        "SecurityOpt": kwargs.get("security_opt"),
        "PidsLimit": kwargs["pids_limit"],
        "MaskedPaths": DEFAULT_MASKED_PATHS,
        "ReadonlyPaths": DEFAULT_READONLY_PATHS,
        **inspect_mounts(kwargs),
    }


HARDENING_KEYS = (
    "pids_limit", "user", "read_only", "cap_drop", "security_opt", "tmpfs", "volumes"
)  # fmt: skip


@pytest.mark.parametrize("filled", [(), ("no-new-privileges:true",)])
def test_the_start_kwargs_carry_exactly_the_spec_hardening(monkeypatch, filled):
    """기대값은 기동 kwargs 의 사본이 아니라 그 출처다 — 둘이 갈라지지 않는다."""
    from aaestro.runtime import spec as spec_module
    from aaestro.runtime.docker.engine import hardening_gaps

    monkeypatch.setattr(spec_module, "HARDENED_SECURITY_OPT", filled)
    built = spec()
    kwargs = built.to_docker_kwargs()

    assert {key: kwargs[key] for key in HARDENING_KEYS} == built.hardening_kwargs()
    assert hardening_gaps(inspect_shape(kwargs), built.hardening_kwargs()) == []
    assert kwargs["security_opt"] == list(filled)


def test_a_non_default_pid_limit_spec_still_reattaches_its_own_container():
    """스펙의 PID limit 이 기본값이 아니어도 자기 kwargs 로 만든 컨테이너는 통과한다."""
    import dataclasses

    from aaestro.runtime.docker.engine import hardening_gaps

    custom = dataclasses.replace(spec(), pids_limit=128)
    kwargs = custom.to_docker_kwargs()

    assert kwargs["pids_limit"] == 128
    assert hardening_gaps(inspect_shape(kwargs), custom.hardening_kwargs()) == []
    assert hardening_gaps(inspect_shape(kwargs), spec().hardening_kwargs()) == ["PidsLimit"]


@pytest.mark.parametrize("filled", [(), ("no-new-privileges:true",)])
def test_a_container_built_from_the_sidecar_kwargs_has_no_hardening_gaps(monkeypatch, filled):
    from aaestro.runtime import spec as spec_module
    from aaestro.runtime.docker.engine import hardening_gaps
    from aaestro.runtime.sidecars import sidecar_kwargs
    from tests.unit.runtime.test_sidecars import SEARCH, with_servers

    monkeypatch.setattr(spec_module, "HARDENED_SECURITY_OPT", filled)
    manifest = with_servers(SEARCH)
    kwargs = sidecar_kwargs("researcher", manifest.spec.mcp.servers[0], {}, "net")

    assert hardening_gaps(inspect_shape(kwargs), kwargs) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("SecurityOpt", ["no-new-privileges:true", "no-new-privileges:false"]),
        ("SecurityOpt", ["no-new-privileges:true", "seccomp=unconfined"]),
        ("PidsLimit", None),
        ("MaskedPaths", []),
        ("Mounts", [bind("/var/run/docker.sock", "/var/run/docker.sock")]),
        ("Mounts", [bind("/", "/host", rw=False)]),
        ("Tmpfs", {}),
    ],
)
async def test_sidecar_reattach_replaces_a_weakened_sidecar(field, value):
    """사이드카 재부착(`McpSidecars._sound`)도 같은 대조를 거친다 — 배선까지 본다."""
    from aaestro.runtime.sidecars import McpSidecars
    from tests.unit.runtime.test_sidecars import SEARCH, with_servers

    docker = FakeDockerClient()
    sidecars = McpSidecars(docker)
    manifest = with_servers(SEARCH)
    await sidecars.start(manifest, {})
    first = docker.find("aaestro-researcher--mcp-search")
    sound = {**inspect_shape(docker.created[-1]), "NetworkMode": None}

    docker._hardening = sound
    await sidecars.start(manifest, {}, reuse=True)
    assert docker.find("aaestro-researcher--mcp-search") == first  # 정상이면 그대로 붙는다

    docker._hardening = {**sound, field: value}
    await sidecars.start(manifest, {}, reuse=True)
    assert first in docker.removed
