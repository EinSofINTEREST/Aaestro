"""Build materials — what an agent image is baked from (#264).

재료는 작업 트리가 아니라 스토어에 산다. 경로 검사는 **적재 시점**이어야 한다 — 조립
단계에서야 잡으면 임시 디렉토리 밖에 파일을 쓴 뒤가 된다.
"""

from __future__ import annotations

import pytest

from aaestro.core.errors import AaestroError, ErrorCode
from aaestro.materials import (
    MAX_FILE_BYTES,
    MAX_FILES,
    InMemoryMaterialStore,
    Materials,
    SqliteMaterialStore,
    check_files,
    check_path,
)

STORES = ("memory", "sqlite")


@pytest.fixture(params=STORES)
def store(request, tmp_path):
    """두 구현이 같은 계약을 만족해야 한다 — 하나만 검사하면 다른 하나가 드리프트한다."""
    if request.param == "memory":
        return InMemoryMaterialStore()
    return SqliteMaterialStore(path=tmp_path / "materials.db")


# --- 경로 규칙 ----------------------------------------------------------------------


@pytest.mark.parametrize("path", ["Dockerfile", "src/agent.py", "src/pkg/mod.py"])
def test_layout_paths_are_accepted(path):
    assert check_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "../escape.py",
        "src/../../escape.py",
        "/etc/passwd",
        "src/./agent.py",
        "C:\\win.py",
        "manifest.yaml",  # 선언은 재료가 아니다 — 빌드가 카탈로그에서 해석한다
        "modules/promptsets/x.yaml",
        "src",  # 디렉토리만으로는 파일이 아니다
        "",
        " Dockerfile",
    ],
)
def test_paths_outside_the_build_context_are_refused(path):
    """컨텍스트를 벗어나는 경로를 받아 두면 조립이 임시 디렉토리 밖에 파일을 쓴다."""
    with pytest.raises(AaestroError) as exc_info:
        check_path(path)

    assert exc_info.value.code == ErrorCode.VAL_002


def test_a_file_that_is_too_large_is_refused():
    with pytest.raises(AaestroError) as exc_info:
        check_files({"src/big.py": "x" * (MAX_FILE_BYTES + 1)})

    assert exc_info.value.code == ErrorCode.VAL_002
    assert exc_info.value.details["limit"] == MAX_FILE_BYTES


def test_too_many_files_are_refused():
    with pytest.raises(AaestroError) as exc_info:
        check_files({f"src/m{i}.py": "" for i in range(MAX_FILES + 1)})

    assert exc_info.value.code == ErrorCode.VAL_002


def test_an_empty_set_is_allowed():
    """재료가 없다는 것을 명시적으로 적을 수 있어야 한다."""
    assert check_files({}) == {}


def test_non_text_content_is_refused():
    with pytest.raises(AaestroError) as exc_info:
        check_files({"src/agent.py": b"bytes"})  # type: ignore[dict-item]

    assert exc_info.value.code == ErrorCode.VAL_002


# --- 스토어 계약 --------------------------------------------------------------------


def test_what_goes_in_comes_back(store):
    store.put(Materials(agent="custom", version="0.1.0", files={"src/agent.py": "MARK = 1"}))

    found = store.get("custom", "0.1.0")

    assert found is not None
    assert dict(found.files) == {"src/agent.py": "MARK = 1"}
    assert found.updated_at


def test_versions_are_separate(store):
    store.put(Materials(agent="custom", version="0.1.0", files={"src/a.py": "old"}))
    store.put(Materials(agent="custom", version="0.2.0", files={"src/a.py": "new"}))

    assert store.get("custom", "0.1.0").files["src/a.py"] == "old"
    assert list(store.versions("custom")) == ["0.1.0", "0.2.0"]


def test_an_unknown_agent_has_nothing(store):
    assert store.get("nobody", "0.1.0") is None
    assert list(store.versions("nobody")) == []


def test_delete_reports_whether_anything_was_there(store):
    store.put(Materials(agent="custom", version="0.1.0", files={}))

    assert store.delete("custom", "0.1.0") is True
    assert store.delete("custom", "0.1.0") is False
    assert store.get("custom", "0.1.0") is None


def test_the_sqlite_store_survives_a_reopen(tmp_path):
    """프로세스를 넘겨야 빌드가 저작과 다른 시점에 일어날 수 있다 (#265)."""
    path = tmp_path / "materials.db"
    SqliteMaterialStore(path=path).put(
        Materials(agent="custom", version="0.1.0", files={"Dockerfile": "FROM base"})
    )

    reopened = SqliteMaterialStore(path=path)

    assert reopened.get("custom", "0.1.0").dockerfile == "FROM base"


def test_the_dockerfile_is_named_when_present(store):
    store.put(Materials(agent="custom", version="0.1.0", files={"src/a.py": ""}))

    assert store.get("custom", "0.1.0").dockerfile is None
