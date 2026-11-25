"""Artifact store tests.

02 Output Discipline 은 대용량 산출물을 output 에 직접 싣지 말고 참조로
전달하라고 규정하는데, `ArtifactStore` 는 **계약만 있고 구현도 주입도
없었다** — 모든 skill 이 `ctx.artifacts is None` 을 받았다 (#139).
"""

from __future__ import annotations

import os
from pathlib import Path

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
    [
        "../escape",
        "/absolute",
        "",
        "a/../../x",
        "sub/../ok",
        "..",
        "a//b",
        "ok.txt\n",
        "a/.hidden",
        ".a",
        "a/.",
        "a/..",
    ],
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
    [
        "not-a-ref",
        "artifact://",
        "artifact://scope",
        "http://scope/key",
        "",
        "artifact://scope/key\n",
    ],
)
def test_a_malformed_reference_is_rejected(raw):
    with pytest.raises(AaestroError) as exc_info:
        parse_ref(raw)

    assert exc_info.value.code == ErrorCode.VAL_002


def test_a_content_key_is_stable_for_the_same_bytes():
    assert digest_key("p", b"same") == digest_key("p", b"same")
    assert digest_key("p", b"same") != digest_key("p", b"other")


# --- 원자적 쓰기 (#664) ---------------------------------------------------------


async def test_a_failed_write_keeps_the_previous_content_and_leaves_no_temp(store, monkeypatch):
    ref = await store.put("k", b"old")

    def boom(fd, mode):
        raise OSError("disk full")

    monkeypatch.setattr("aaestro.artifacts.store.os.fdopen", boom)
    with pytest.raises(AaestroError) as exc_info:
        await store.put("k", b"new")
    monkeypatch.undo()

    assert exc_info.value.code == ErrorCode.STOR_003
    assert isinstance(exc_info.value.__cause__, OSError)
    assert await store.get(ref) == b"old"
    assert [p.name for p in (store.root / store.scope).iterdir()] == ["k"]


async def test_a_failed_replace_removes_the_temp_file(store, monkeypatch):
    def boom(self, target):
        raise OSError("busy")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(AaestroError):
        await store.put("k", b"new")

    assert list((store.root / store.scope).iterdir()) == []


async def test_temp_files_are_not_listed_as_keys(store):
    await store.put("k", b"x")
    (store.root / store.scope / ".tmp-leftover").write_bytes(b"partial")

    assert list(store.stored_keys()) == ["k"]


async def test_a_reference_stored_under_the_old_rules_is_still_readable(store):
    """점 시작 조각 거절은 쓰기에만 — state 안에 남은 예전 참조를 읽을 수 있어야 한다."""
    legacy = store.root / store.scope / "a" / ".hidden"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"old")

    assert await store.get("artifact://researcher/a/.hidden") == b"old"
    with pytest.raises(AaestroError) as exc_info:
        await store.put("a/.hidden", b"new")
    assert exc_info.value.code == ErrorCode.VAL_002


async def test_a_temp_file_name_is_rejected_on_read(store):
    (store.root / store.scope).mkdir(parents=True)
    (store.root / store.scope / ".tmp-abc").write_bytes(b"partial")

    with pytest.raises(AaestroError) as exc_info:
        await store.get("artifact://researcher/.tmp-abc")
    assert exc_info.value.code == ErrorCode.VAL_002
    with pytest.raises(AaestroError) as exc_info:
        await store.get("artifact://researcher/a/.tmp-abc")
    assert exc_info.value.code == ErrorCode.VAL_002


async def test_files_under_a_hidden_directory_are_not_listed(store):
    await store.put("k", b"x")
    hidden = store.root / store.scope / ".d"
    hidden.mkdir()
    (hidden / "f").write_bytes(b"x")

    assert list(store.stored_keys()) == ["k"]


async def test_a_failing_fdopen_closes_the_descriptor(store, monkeypatch):
    closed = []
    real_close = os.close
    monkeypatch.setattr(
        "aaestro.artifacts.store.os.close", lambda fd: (closed.append(fd), real_close(fd))
    )
    monkeypatch.setattr(
        "aaestro.artifacts.store.os.fdopen", lambda *a, **k: (_ for _ in ()).throw(OSError("x"))
    )

    with pytest.raises(AaestroError):
        await store.put("k", b"x")

    assert len(closed) == 1


# --- 값 없는 거절 (#412) ---------------------------------------------------------


@pytest.mark.parametrize("bad", ["ok.txt\nMARKER", "../MARKER", "MARKER/\x1b[2J", "a//MARKER"])
def test_a_refused_key_is_not_echoed(bad):
    with pytest.raises(AaestroError) as exc_info:
        validate_key(bad)

    err = exc_info.value
    assert "MARKER" not in err.message + str(err.payload())
    assert "reason" in err.details


def test_a_refused_reference_is_not_echoed():
    with pytest.raises(AaestroError) as exc_info:
        parse_ref("artifact://scope/key\nMARKER")

    assert "MARKER" not in str(exc_info.value.payload())
