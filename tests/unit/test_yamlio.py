"""Strict YAML reading: duplicate keys are rejected, everything else reads like ``safe_load``.

중복 키는 거절하고, 중복이 없는 문서는 ``yaml.safe_load`` 와 같은 결과로 읽는다 (merge key 포함).
"""

from __future__ import annotations

import random
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from aaestro.agentd.__main__ import load_manifest
from aaestro.core.errors import AaestroError
from aaestro.core.yamlio import (
    NOT_UTF8,
    TOO_DEEP,
    DuplicateKeyError,
    YamlReadError,
    load_yaml_file,
    safe_load_unique,
    yaml_problem,
)
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skillset import SkillsetLoader
from tests.fixtures.declarations import MARKER, REPO_ROOT, _declaration_files
from tests.fixtures.yaml_fuzz import DocumentGenerator, with_duplicate

# --- 중복 키 ---------------------------------------------------------------------------------


def test_duplicate_key_is_rejected_with_the_key_and_line(tmp_path: Path):
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "apiVersion: aaestro/v1\nkind: Agent\nmetadata: {name: a, version: 0.1.0}\n"
        "spec:\n  runtime:\n    egress: [a.example.com]\n    egress: [b.example.com]\n",
        encoding="utf-8",
    )

    with pytest.raises(AaestroError) as exc_info:
        load_manifest(path)

    assert exc_info.value.code == "CFG_001"
    assert "duplicate key 'egress' at line 7" in exc_info.value.details["reason"]


def test_registry_rejects_a_duplicate_key_in_a_module_declaration(tmp_path: Path):
    src = REPO_ROOT / "tests" / "fixtures" / "modules" / "modules"
    shutil.copytree(src, tmp_path / "modules")
    target = tmp_path / "modules" / "skillsets/web-search/0.2.0/skillset.yaml"
    target.write_text(target.read_text(encoding="utf-8") + "kind: Skillset\n", encoding="utf-8")

    with pytest.raises(AaestroError) as exc_info:
        SkillsetLoader(ModuleRegistry.under(tmp_path)).load("skillsets/web-search@0.2.0")

    assert exc_info.value.code == "MOD_003"
    assert "duplicate key 'kind'" in exc_info.value.details["reason"]


def test_every_repository_declaration_file_has_unique_keys():
    for path in _declaration_files():
        safe_load_unique(path.read_text(encoding="utf-8"))


# --- merge key (<<) 는 과잉 거절하지 않는다 ---------------------------------------------------

MERGE_DOCUMENTS = {
    "merge-overridden-by-explicit": "base: &b {k: 1, j: 3}\nuse:\n  <<: *b\n  k: 2\n",
    "merge-chain": "a: &a {x: 1}\nb: &b {<<: *a, y: 2}\nc:\n  <<: *b\n  z: 3\n",
    "merge-list": "a: &a {x: 1}\nb: &b {y: 2}\nc:\n  <<: [*a, *b]\n  z: 3\n",
    "merge-inline": "c: {<<: {k: 1}, k: 2}\n",
    "merge-with-later-keys": "a: &a {x: 1}\nc:\n  k: 1\n  <<: *a\n  m: 2\n",
    # 얕은 매핑의 merge 가 공유된 앵커 노드를 먼저 고친다 — 구성 단계에서 검사하면 ``inner`` 의
    # merge 유래 ``k`` 와 명시 ``k`` 가 둘 다 명시로 보여 유효한 문서를 거절했다
    "merge-of-an-anchor-that-itself-merges": (
        "a: &a {k: 1}\nx:\n  inner: &b {<<: *a, k: 2}\ny: {<<: *b, z: 1}\n"
    ),
    "two-plain-merge-keys": "a: {<<: {x: 1}, <<: {y: 2}}\n",
    "recursive-anchor": "a: &r [*r]\nb: &m {self: *m}\n",
    "string-and-int-keys-differ": "'1': a\n1: b\n",
    "quoted-merge-is-a-plain-key": "a: {'<<': 1, k: 2}\n",
}


@pytest.mark.parametrize("name", sorted(MERGE_DOCUMENTS))
def test_merge_documents_read_exactly_like_safe_load(name: str):
    text = MERGE_DOCUMENTS[name]

    # repr 로 비교한다 — 재귀 앵커가 만든 자기 참조 구조는 ``==`` 가 끝나지 않는다
    assert repr(safe_load_unique(text)) == repr(yaml.safe_load(text))


@pytest.mark.parametrize(
    "text",
    [
        "base: &b {k: 1}\nuse:\n  <<: *b\n  k: 2\n  k: 3\n",
        "c: {<<: {k: 1}, k: 2, k: 3}\n",
        "base: &b {k: 1}\nuse:\n  k: 1\n  <<: *b\n  k: 2\n",
        # merge 값으로 쓰인 인라인 매핑은 construct_mapping 을 거치지 않는다 — 노드를 걸어야 잡힌다
        "m: {<<: {k: 1, k: 2}}\n",
        "m: {<<: [{k: 1, k: 2}, {j: 1}]}\n",
        "a: &a {k: 1, k: 2}\nb: *a\nc: *a\n",
    ],
    ids=[
        "explicit-after-merge",
        "inline",
        "explicit-around-merge",
        "inside-an-inline-merge-value",
        "inside-a-merge-list",
        "inside-an-aliased-mapping",
    ],
)
def test_a_real_duplicate_next_to_a_merge_is_still_rejected(text: str):
    with pytest.raises(DuplicateKeyError) as exc_info:
        safe_load_unique(text)

    assert exc_info.value.key == "k"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("1: a\n1.0: b\n", 1.0),
        ("true: a\n1: b\n", 1),
        ("yes: a\ntrue: b\n", True),
        ("=: a\n'=': b\n", "="),
        ("~: a\nnull: b\n", None),
    ],
    ids=["int-float", "bool-int", "yes-true", "value-tag", "null-forms"],
)
def test_keys_that_safe_load_folds_into_one_are_duplicates(text: str, key: object):
    """키 동등성은 구성된 값의 동등성이다 — ``safe_load`` 에서 한 키가 되면 중복이다."""
    assert len(yaml.safe_load(text)) == 1

    with pytest.raises(DuplicateKeyError) as exc_info:
        safe_load_unique(text)

    assert exc_info.value.key == key


# --- 두 로더의 차분 퍼즈 ----------------------------------------------------------------------

FUZZ_DOCUMENTS = 300
FUZZ_SEED = 386


@pytest.mark.parametrize("merges", [False, True], ids=["no-merge", "merge"])
def test_random_documents_without_duplicates_read_exactly_like_safe_load(merges: bool):
    """과잉 거절 0 — 중복이 없는 무작위 문서 (앵커 · alias · merge · 중첩) 에서 결과가 항상 같다."""
    rng = random.Random(FUZZ_SEED + merges)  # noqa: S311 — 결정적 테스트 입력
    with_merge = 0

    for _ in range(FUZZ_DOCUMENTS):
        text = DocumentGenerator(rng, merges=merges).document()
        with_merge += "<<:" in text

        assert safe_load_unique(text) == yaml.safe_load(text), text

    # 생성기가 조용히 merge 없는 문서만 내면 이 테스트는 아무것도 보지 않는다
    assert (with_merge > FUZZ_DOCUMENTS // 2) is merges


def test_random_documents_with_a_planted_duplicate_are_always_rejected():
    """과소 거절 0 — 어느 매핑에 심든 (인라인 merge 값 안 포함) 명시 중복은 거절된다."""
    rng = random.Random(FUZZ_SEED)  # noqa: S311 — 결정적 테스트 입력
    planted = inside_inline_merge = 0

    for index in range(FUZZ_DOCUMENTS):
        text = with_duplicate(DocumentGenerator(rng, merges=index % 2 == 0).document(), rng)
        if text is None:
            continue
        planted += 1
        inside_inline_merge += "<<: {zzdup" in text
        assert len(yaml.safe_load(text)) >= 1  # 심은 문서도 safe_load 는 조용히 읽는다

        with pytest.raises(DuplicateKeyError):
            safe_load_unique(text)

    assert planted > FUZZ_DOCUMENTS * 2 // 3
    assert inside_inline_merge > 0


# --- 키 이름의 정제 ---------------------------------------------------------------------------


def test_duplicate_key_name_is_bounded_and_printable():
    """키 이름은 의도적으로 나간다 — 다만 1만 자 키나 ANSI 이스케이프가 그대로 나가지는 않는다."""
    long_key = "k" * 10_000
    with pytest.raises(DuplicateKeyError) as long_info:
        # 1024 자를 넘는 키는 명시 키 (``? key``) 로만 적을 수 있다
        safe_load_unique(f"? {long_key}\n: 1\n? {long_key}\n: 2\n")
    with pytest.raises(DuplicateKeyError) as control_info:
        safe_load_unique('"a\\e[31mb\\a": 1\n"a\\e[31mb\\a": 2\n')

    assert len(str(long_info.value)) < 120
    assert "…" in str(long_info.value)
    assert str(control_info.value) == "duplicate key 'a?[31mb?' at line 2"


# --- 파일 읽기: 읽기 → 디코딩 → 파싱의 실패가 한 종류로 -----------------------------------------


def test_load_yaml_file_reads_a_document(tmp_path: Path):
    path = tmp_path / "a.yaml"
    path.write_text("a: 1\nb: [x, 키]\n", encoding="utf-8")

    assert load_yaml_file(path) == {"a": 1, "b": ["x", "키"]}


def _failure(path: Path) -> YamlReadError:
    with pytest.raises(YamlReadError) as exc_info:
        load_yaml_file(path)
    return exc_info.value


def test_load_yaml_file_reports_a_missing_file_as_missing(tmp_path: Path):
    err = _failure(tmp_path / "nope.yaml")

    assert err.missing
    assert err.__context__ is None


def test_load_yaml_file_reports_non_utf8_without_the_bytes(tmp_path: Path):
    path = tmp_path / "a.yaml"
    path.write_bytes(b"token: \xff" + MARKER.encode() + b"\n")

    err = _failure(path)

    assert err.reason == NOT_UTF8
    assert not err.missing
    # UnicodeDecodeError.object 는 파일 바이트 전체다 — 체인에 남으면 값이 나간다
    assert err.__context__ is None
    assert err.__cause__ is None


def test_load_yaml_file_reports_a_directory_by_the_os_reason(tmp_path: Path):
    err = _failure(tmp_path)

    assert err.reason == "Is a directory"
    assert not err.missing


def test_load_yaml_file_reports_position_only_for_broken_yaml(tmp_path: Path):
    path = tmp_path / "a.yaml"
    path.write_text(f"a: 1\nsecret: {MARKER} : [oops\n", encoding="utf-8")

    err = _failure(path)

    assert err.reason.startswith("ScannerError at line 2, column ")
    assert MARKER not in err.reason
    assert err.__context__ is None


def test_load_yaml_file_reports_a_duplicate_key(tmp_path: Path):
    path = tmp_path / "a.yaml"
    path.write_text("a: 1\na: 2\n", encoding="utf-8")

    assert _failure(path).reason == "duplicate key 'a' at line 2"


def test_load_yaml_file_reports_runaway_nesting_instead_of_a_traceback(tmp_path: Path):
    depth = 5000
    path = tmp_path / "a.yaml"
    path.write_text("x: " + "[" * depth + MARKER + "]" * depth + "\n", encoding="utf-8")

    assert _failure(path).reason == TOO_DEEP


def _tracked_yaml() -> list[Path]:
    out = subprocess.run(  # noqa: S603
        ["git", "ls-files", "-z", "*.yaml", "*.yml"],  # noqa: S607
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / name for name in out.split("\0") if name]


@pytest.mark.parametrize("path", _tracked_yaml(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_every_tracked_yaml_reads_like_safe_load(path: Path):
    """중복 키가 없는 문서 (compose · CI workflow · 선언) 에서 두 로더의 결과가 같다."""
    text = path.read_text(encoding="utf-8")

    assert safe_load_unique(text) == yaml.safe_load(text)


# --- 파싱 실패: 위치와 종류만, 입력 조각은 없다 -----------------------------------------------


def test_yaml_problem_reports_line_and_column_but_not_the_input():
    text = f"a: 1\nsecret: {MARKER} : [oops\n"

    with pytest.raises(yaml.YAMLError) as exc_info:
        safe_load_unique(text)

    problem = yaml_problem(exc_info.value)
    assert MARKER in str(
        exc_info.value
    )  # 원래 에러는 스니펫을 싣는다 — 그래서 details 에 쓰지 않는다
    assert problem["reason"].startswith("ScannerError at line 2, column ")
    assert MARKER not in problem["reason"]


def test_yaml_problem_names_the_duplicate_key_and_line():
    with pytest.raises(DuplicateKeyError) as exc_info:
        safe_load_unique("a: 1\nb: 2\na: 3\n")

    assert yaml_problem(exc_info.value) == {"reason": "duplicate key 'a' at line 3"}
