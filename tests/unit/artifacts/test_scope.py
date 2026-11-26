"""Scoped artifact access tests.

01 은 artifact 를 global/group/local 3계층 리소스로 규정한다. 경계를 강제하지
않으면 artifact 가 graph state 를 우회하는 **사이드채널**이 된다 (#140).
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from aaestro.artifacts.scope import ArtifactScope, ScopedArtifacts
from aaestro.artifacts.store import FilesystemArtifactStore
from aaestro.core.errors import AaestroError, ErrorCode


def scoped(tmp_path, *, group=True, global_=True, quotas=None) -> ScopedArtifacts:
    stores = {ArtifactScope.LOCAL: FilesystemArtifactStore(root=tmp_path, scope="researcher")}
    if group:
        stores[ArtifactScope.GROUP] = FilesystemArtifactStore(root=tmp_path, scope="research")
    if global_:
        stores[ArtifactScope.GLOBAL] = FilesystemArtifactStore(root=tmp_path, scope="org")
    return ScopedArtifacts(stores=stores, quotas=quotas or {})


# --- 쓰기는 local 로만 -------------------------------------------------------------


async def test_a_write_lands_in_the_local_scope(tmp_path):
    """어느 스코프에 쓸지 호출자가 고르게 하면 group/global 을 임의로 오염시킨다."""
    access = scoped(tmp_path)

    ref = await access.put("out.txt", b"mine")

    assert ref == "artifact://researcher/out.txt"


async def test_a_write_without_a_local_scope_is_denied(tmp_path):
    access = ScopedArtifacts(stores={})

    with pytest.raises(AaestroError) as exc_info:
        await access.put("out.txt", b"x")

    assert exc_info.value.code == ErrorCode.ART_001


# --- 읽기는 선언된 스코프만 ---------------------------------------------------------


@pytest.mark.parametrize(
    ("scope_name", "expected"),
    [("researcher", b"local"), ("research", b"group"), ("org", b"global")],
)
async def test_a_declared_scope_can_be_read(tmp_path, scope_name, expected):
    access = scoped(tmp_path)
    for name, payload in (("researcher", b"local"), ("research", b"group"), ("org", b"global")):
        await FilesystemArtifactStore(root=tmp_path, scope=name).put("shared", payload)

    assert await access.get(f"artifact://{scope_name}/shared") == expected


async def test_an_undeclared_scope_is_denied(tmp_path):
    """비멤버가 그룹 산출물을 읽으면 artifact 가 사이드채널이 된다."""
    access = scoped(tmp_path, group=False)
    await FilesystemArtifactStore(root=tmp_path, scope="research").put("secret", b"theirs")

    with pytest.raises(AaestroError) as exc_info:
        await access.get("artifact://research/secret")

    assert exc_info.value.code == ErrorCode.ART_001


async def test_a_completely_unknown_scope_is_denied(tmp_path):
    access = scoped(tmp_path)

    with pytest.raises(AaestroError) as exc_info:
        await access.get("artifact://someone-else/thing")

    assert exc_info.value.code == ErrorCode.ART_001


# --- 해석 순서 -----------------------------------------------------------------


def test_the_resolution_order_is_nearest_first():
    """01 Resource Scoping — 가까운 스코프가 이긴다."""
    from aaestro.artifacts.scope import RESOLUTION_ORDER

    assert RESOLUTION_ORDER == (
        ArtifactScope.LOCAL,
        ArtifactScope.GROUP,
        ArtifactScope.GLOBAL,
    )


async def test_the_same_key_resolves_to_the_nearest_scope(tmp_path):
    """세 스코프에 같은 이름이 있어도 참조가 스코프를 명시하므로 섞이지 않는다."""
    access = scoped(tmp_path)
    for name, payload in (("researcher", b"near"), ("research", b"mid"), ("org", b"far")):
        await FilesystemArtifactStore(root=tmp_path, scope=name).put("same", payload)

    assert await access.get("artifact://researcher/same") == b"near"
    assert await access.get("artifact://org/same") == b"far"


# --- quota -------------------------------------------------------------------


async def test_a_write_within_quota_succeeds(tmp_path):
    access = scoped(tmp_path, quotas={ArtifactScope.LOCAL: 1024})

    ref = await access.put("small", b"x" * 100)

    assert await access.get(ref) == b"x" * 100


async def test_a_write_over_quota_is_refused(tmp_path):
    """상한을 넘겨 쓰고 나서 지우면 이미 디스크를 먹은 뒤다 — 쓰기 전에 막는다."""
    access = scoped(tmp_path, quotas={ArtifactScope.LOCAL: 100})

    with pytest.raises(AaestroError) as exc_info:
        await access.put("big", b"x" * 200)

    assert exc_info.value.code == ErrorCode.ART_002


async def test_quota_counts_what_is_already_stored(tmp_path):
    """한 번에 넘지 않아도 누적으로 넘으면 거부해야 한다."""
    access = scoped(tmp_path, quotas={ArtifactScope.LOCAL: 150})
    await access.put("first", b"x" * 100)

    with pytest.raises(AaestroError) as exc_info:
        await access.put("second", b"x" * 100)

    assert exc_info.value.code == ErrorCode.ART_002
    assert exc_info.value.details["used_bytes"] == "100"


async def test_no_quota_means_no_limit(tmp_path):
    access = scoped(tmp_path)

    ref = await access.put("large", b"x" * 100_000)

    assert len(await access.get(ref)) == 100_000


# --- 스키마 -------------------------------------------------------------------


def test_a_group_quota_is_parsed_from_its_declaration():
    """`dict[str, Any]` 로 두면 오타가 조용히 통과하고 quota 가 강제되지 않는다."""
    from aaestro.core.manifest import ArtifactSpec

    assert ArtifactSpec(quota="50Gi").quota_bytes == 50 * 1024**3


@pytest.mark.parametrize("bad", ["50GB", "fifty", "50", "-5Gi", "50gi"])
def test_a_malformed_quota_is_rejected(bad):
    from pydantic import ValidationError

    from aaestro.core.manifest import ArtifactSpec

    with pytest.raises(ValidationError):
        ArtifactSpec(quota=bad)


def test_no_quota_declared_means_unlimited():
    from aaestro.core.manifest import ArtifactSpec

    assert ArtifactSpec().quota_bytes is None


async def test_concurrent_writes_cannot_both_pass_the_quota_check(tmp_path):
    """쓰기가 스레드로 넘어가도 검사와 쓰기가 한 덩어리여야 상한을 넘지 않는다."""
    access = scoped(tmp_path, quotas={ArtifactScope.LOCAL: 100})

    results = await asyncio.gather(
        access.put("a", b"x" * 80), access.put("b", b"y" * 80), return_exceptions=True
    )

    failed = [r for r in results if isinstance(r, AaestroError)]
    assert len(failed) == 1
    assert failed[0].code == ErrorCode.ART_002


async def test_a_cancelled_put_keeps_the_lock_until_its_write_finishes(tmp_path, monkeypatch):
    """취소돼도 쓰기 스레드는 계속 돈다 — 그 사이 다음 저장이 낡은 사용량으로 통과하면 안 된다."""
    access = scoped(tmp_path, quotas={ArtifactScope.LOCAL: 100})
    store = access.stores[ArtifactScope.LOCAL]
    started, release = threading.Event(), threading.Event()
    real_write = store._write_atomic

    def held_write(key, data):
        started.set()
        release.wait(timeout=10)
        real_write(key, data)

    monkeypatch.setattr(store, "_write_atomic", held_write)

    first = asyncio.create_task(access.put("a", b"x" * 80))
    await asyncio.to_thread(started.wait, 10)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    second = asyncio.create_task(access.put("b", b"y" * 80))
    for _ in range(20):
        await asyncio.sleep(0)
    assert not second.done()  # 첫 쓰기가 끝나기 전에는 quota 검사에 들어가지 못한다

    release.set()
    with pytest.raises(AaestroError) as exc_info:
        await second
    assert exc_info.value.code == ErrorCode.ART_002


# --- 같은 그룹 디렉토리를 쓰는 다른 인스턴스 · 프로세스 (#679) ----------------------------


async def test_two_instances_on_one_group_directory_cannot_both_pass_the_quota(tmp_path):
    """asyncio 락은 인스턴스 안만 직렬화한다 — 디렉토리의 flock 이 인스턴스 사이를 막는다."""
    quotas = {ArtifactScope.GROUP: 100}
    first, second = (scoped(tmp_path, quotas=quotas) for _ in range(2))

    results = await asyncio.gather(
        first.put("a", b"x" * 80, scope=ArtifactScope.GROUP),
        second.put("b", b"y" * 80, scope=ArtifactScope.GROUP),
        return_exceptions=True,
    )

    failed = [r for r in results if isinstance(r, AaestroError)]
    assert len(failed) == 1
    assert failed[0].code == ErrorCode.ART_002


async def test_the_lock_file_is_not_an_artifact(tmp_path):
    access = scoped(tmp_path, quotas={ArtifactScope.GROUP: 100})
    await access.put("a", b"x" * 10, scope=ArtifactScope.GROUP)

    store = access.stores[ArtifactScope.GROUP]
    assert (tmp_path / "research" / ".quota.lock").exists()
    assert list(store.stored_keys()) == ["a"]
    assert store.used_bytes() == 10


async def test_a_scope_without_quota_takes_no_lock_file(tmp_path):
    access = scoped(tmp_path)
    await access.put("a", b"x")

    assert not (tmp_path / "researcher" / ".quota.lock").exists()


async def test_a_lock_failure_is_a_typed_error_and_nothing_is_written(tmp_path):
    access = scoped(tmp_path, quotas={ArtifactScope.GROUP: 100})
    (tmp_path / "research").mkdir()
    (tmp_path / "research" / ".quota.lock").symlink_to(tmp_path / "elsewhere")  # O_NOFOLLOW 거절

    with pytest.raises(AaestroError) as exc_info:
        await access.put("a", b"x", scope=ArtifactScope.GROUP)

    assert exc_info.value.code == ErrorCode.STOR_003
    assert list(access.stores[ArtifactScope.GROUP].stored_keys()) == []


async def test_a_read_only_lock_file_still_locks_and_writes(tmp_path):
    """다른 uid 가 만든 잠금 파일은 쓰기 권한이 없다 — flock 은 읽기 전용 fd 로 건다."""
    access = scoped(tmp_path, quotas={ArtifactScope.GROUP: 100})
    (tmp_path / "research").mkdir()
    lock = tmp_path / "research" / ".quota.lock"
    lock.touch()
    lock.chmod(0o444)

    await access.put("a", b"x" * 10, scope=ArtifactScope.GROUP)

    assert list(access.stores[ArtifactScope.GROUP].stored_keys()) == ["a"]


async def test_a_cancelled_lock_wait_does_not_leak_the_lock(tmp_path):
    """잠금 대기 중 취소돼도 잡은 fd 가 남지 않아야 다음 쓰기가 막히지 않는다."""
    first = scoped(tmp_path, quotas={ArtifactScope.GROUP: 100})
    second = scoped(tmp_path, quotas={ArtifactScope.GROUP: 100})
    (tmp_path / "research").mkdir()
    held = os.open(tmp_path / "research" / ".quota.lock", os.O_RDONLY | os.O_CREAT, 0o644)
    fcntl.flock(held, fcntl.LOCK_EX)

    put = asyncio.create_task(first.put("a", b"x" * 10, scope=ArtifactScope.GROUP))
    for _ in range(20):
        await asyncio.sleep(0.01)
    for write in list(first._writes):
        write.cancel()  # 잠금 대기 중인 쓰기 태스크를 취소한다
    with pytest.raises(asyncio.CancelledError):
        await put
    os.close(held)

    await asyncio.wait_for(second.put("b", b"y" * 10, scope=ArtifactScope.GROUP), timeout=10)


async def test_lock_waiters_do_not_starve_the_executor(tmp_path):
    """대기자가 기본 executor 워커를 쥐면 잠금을 쥔 쪽의 쓰기가 워커를 못 얻어 교착한다."""
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(executor)
    quotas = {ArtifactScope.GROUP: 100}
    instances = [scoped(tmp_path, quotas=quotas) for _ in range(4)]
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                *(
                    inst.put(f"k{i}", b"x" * 40, scope=ArtifactScope.GROUP)
                    for i, inst in enumerate(instances)
                ),
                return_exceptions=True,
            ),
            timeout=15,
        )
    finally:
        executor.shutdown(wait=False)

    failed = [r for r in results if isinstance(r, AaestroError)]
    assert len(failed) == 2
    assert {r.code for r in failed} == {ErrorCode.ART_002}


_CHILD = """
import asyncio, sys, time
from pathlib import Path
from aaestro.artifacts.scope import ArtifactScope, ScopedArtifacts
from aaestro.artifacts.store import FilesystemArtifactStore
from aaestro.core.errors import AaestroError

base, name, rounds = Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])

async def main():
    (base / f"ready-{name}").touch()
    for r in range(rounds):
        while not (base / f"go-{r}").exists():
            time.sleep(0.001)
        store = FilesystemArtifactStore(root=base / f"r{r}", scope="research")
        access = ScopedArtifacts(
            stores={ArtifactScope.GROUP: store}, quotas={ArtifactScope.GROUP: 100}
        )
        try:
            await access.put(name, b"x" * 80, scope=ArtifactScope.GROUP)
            print("ok", flush=True)
        except AaestroError as err:
            print(err.code, flush=True)

asyncio.run(main())
"""


def test_two_processes_cannot_both_pass_the_group_quota(tmp_path):
    """서로 다른 프로세스의 동시 저장 — 라운드마다 하나만 성공하고 하나는 ART_002 다."""
    rounds = 15
    procs = [
        subprocess.Popen(  # noqa: S603 — 고정 인자의 테스트용 자식 프로세스
            [sys.executable, "-I", "-c", _CHILD, str(tmp_path), name, str(rounds)],
            stdout=subprocess.PIPE,
            text=True,
        )
        for name in ("a", "b")
    ]
    try:
        deadline = time.monotonic() + 30
        while not all((tmp_path / f"ready-{n}").exists() for n in ("a", "b")):
            if time.monotonic() > deadline:
                for p in procs:
                    p.kill()
                pytest.fail(f"children not ready: {[p.communicate()[0] for p in procs]}")
            time.sleep(0.01)
        for r in range(rounds):
            (tmp_path / f"go-{r}").touch()
            time.sleep(0.05)
        outputs = [p.communicate(timeout=60)[0].split() for p in procs]
    finally:
        for p in procs:
            p.kill()

    for r in range(rounds):
        assert sorted(out[r] for out in outputs) == ["ART_002", "ok"], f"round {r}"
