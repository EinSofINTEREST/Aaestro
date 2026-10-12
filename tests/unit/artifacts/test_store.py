"""Artifact store tests.

02 Output Discipline 은 대용량 산출물을 output 에 직접 싣지 말고 참조로
전달하라고 규정하는데, `ArtifactStore` 는 **계약만 있고 구현도 주입도
없었다** — 모든 skill 이 `ctx.artifacts is None` 을 받았다 (#139).
"""

from __future__ import annotations

import pytest

from aaestro.artifacts import FilesystemArtifactStore, parse_ref
from aaestro.artifacts.store import ArtifactRef, digest_key, validate_key
from aaestro.core.errors import AaestroError, ErrorCode


@pytest.fixture
def store(tmp_path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(root=tmp_path, scope="researcher")


# --- 저장과 회수 ----------------------------------------------------------------


async def test_a_stored_artifact_reads_back_identically(store):
    payload = b"# report\n\xff\x00 binary safe"

    ref = await store.put("reports/final.md", payload)

    assert await store.get(ref) == payload


async def test_the_reference_is_opaque_not_a_host_path(store):
    """호스트 경로를 노출하면 backend 교체 시 참조가 깨진다."""
    ref = await store.put("out.txt", b"x")

    assert ref == "artifact://researcher/out.txt"
    assert str(store.root) not in ref


async def test_nested_keys_are_supported(store):
    ref = await store.put("a/b/c/deep.txt", b"deep")

    assert await store.get(ref) == b"deep"
    assert "a/b/c/deep.txt" in store.stored_keys()


async def test_an_unknown_reference_is_not_found(store):
    with pytest.raises(AaestroError) as exc_info:
        await store.get("artifact://researcher/never-stored")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_storing_the_same_key_twice_overwrites(store):
    await store.put("k", b"first")
    ref = await store.put("k", b"second")

    assert await store.get(ref) == b"second"


# --- 경로 탈출 차단 --------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["../escape", "/absolute", "", "a/../../x", "sub/../ok", "..", "a//b"],
)
async def test_a_key_cannot_escape_the_store_root(store, key):
    """`../` 한 조각이면 루트 밖 어디든 쓸 수 있다."""
    with pytest.raises(AaestroError) as exc_info:
        await store.put(key, b"x")

    assert exc_info.value.code == ErrorCode.VAL_002


@pytest.mark.parametrize("key", ["ok.txt", "a/b.txt", "a-b_c.1/d.txt"])
def test_safe_keys_are_accepted(key):
    assert validate_key(key) == key


async def test_a_traversing_reference_is_rejected_on_read(store):
    """쓰기만 막고 읽기를 열어두면 탈출 경로가 그대로 남는다."""
    with pytest.raises(AaestroError) as exc_info:
        await store.get("artifact://researcher/../../etc/passwd")

    assert exc_info.value.code == ErrorCode.VAL_002


# --- 스코프 경계 ----------------------------------------------------------------


async def test_a_reference_from_another_scope_is_refused(store):
    """다른 스코프의 참조를 읽어주면 스코프 경계가 무의미해진다."""
    with pytest.raises(AaestroError) as exc_info:
        await store.get("artifact://someone-else/thing")

    assert exc_info.value.code == ErrorCode.NF_001


async def test_the_same_key_in_two_scopes_does_not_collide(tmp_path):
    mine = FilesystemArtifactStore(root=tmp_path, scope="researcher")
    theirs = FilesystemArtifactStore(root=tmp_path, scope="writer")

    await mine.put("shared-name", b"mine")
    await theirs.put("shared-name", b"theirs")

    assert await mine.get("artifact://researcher/shared-name") == b"mine"
    assert await theirs.get("artifact://writer/shared-name") == b"theirs"


# --- 참조 파싱 -----------------------------------------------------------------


def test_a_reference_round_trips():
    ref = ArtifactRef(scope="researcher", key="a/b.txt")

    assert parse_ref(str(ref)) == ref


@pytest.mark.parametrize(
    "raw",
    ["not-a-ref", "artifact://", "artifact://scope", "http://scope/key", ""],
)
def test_a_malformed_reference_is_rejected(raw):
    with pytest.raises(AaestroError) as exc_info:
        parse_ref(raw)

    assert exc_info.value.code == ErrorCode.VAL_002


def test_a_content_key_is_stable_for_the_same_bytes():
    assert digest_key("p", b"same") == digest_key("p", b"same")
    assert digest_key("p", b"same") != digest_key("p", b"other")
