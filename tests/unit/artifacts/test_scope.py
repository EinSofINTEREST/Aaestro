"""Scoped artifact access tests.

01 은 artifact 를 global/group/local 3계층 리소스로 규정한다. 경계를 강제하지
않으면 artifact 가 graph state 를 우회하는 **사이드채널**이 된다 (#140).
"""

from __future__ import annotations

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
