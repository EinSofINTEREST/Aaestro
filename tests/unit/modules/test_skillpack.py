"""Unit tests for the skillpack loader — Agent Skills directories, carried unmodified."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from aaestro.catalog import Catalog
from aaestro.core.errors import AaestroError
from aaestro.modules import skillpack, skilltree
from aaestro.modules.registry import ModuleRegistry
from aaestro.modules.skillpack import (
    SKILL_MD_RECOMMENDED_LINES,
    SkillpackLoader,
    parse_skill_md,
)
from aaestro.modules.skilltree import SkillViolation

REPO_ROOT = Path(__file__).resolve().parents[3]
REF = "skillpacks/pack@1.0.0"
DESCRIPTION = "Does one thing. Use when that thing is needed."


def skill_md(name: str = "demo", **fields: object) -> str:
    """frontmatter 를 조립한다 — 값은 YAML 스칼라로 그대로 들어간다."""
    lines = [f"name: {name}", f"description: {fields.pop('description', DESCRIPTION)}"]
    lines.extend(f"{key}: {value}" for key, value in fields.items())
    return "---\n" + "\n".join(lines) + "\n---\n\n# Demo\n\nBody.\n"


def make_pack(base: Path, skills: dict[str, str] | None = None) -> Path:
    """`modules/skillpacks/pack/1.0.0/` 을 만들고 팩 루트를 돌려준다."""
    root = base / "modules" / "skillpacks" / "pack" / "1.0.0"
    (root / "skills").mkdir(parents=True)
    (root / "skillpack.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillpack\nmetadata:\n  name: pack\n  version: 1.0.0\n",
        encoding="utf-8",
    )
    for directory, content in ({"demo": skill_md()} if skills is None else skills).items():
        (root / "skills" / directory).mkdir()
        (root / "skills" / directory / "SKILL.md").write_text(content, encoding="utf-8")
    return root


def load(base: Path):
    return SkillpackLoader(ModuleRegistry.under(base)).load(REF)


def rejected(base: Path) -> AaestroError:
    with pytest.raises(AaestroError) as exc_info:
        load(base)
    assert exc_info.value.code == "MOD_003"
    assert exc_info.value.details["module_ref"] == REF
    return exc_info.value


# --- 예제 팩과 기본 로드 -----------------------------------------------------------


def test_load_repository_example_lists_standard_skills():
    loaded = SkillpackLoader(ModuleRegistry.under(REPO_ROOT)).load("skillpacks/docs@1.0.0")

    assert loaded.skill_names == ("changelog-entry", "doc-review")
    assert loaded.warnings == ()
    changelog = loaded.skills[0]
    assert "scripts/next_version.py" in changelog.files
    assert "references/CATEGORIES.md" in changelog.files
    assert changelog.frontmatter.metadata == {"author": "aaestro-examples", "version": "1.0"}
    assert changelog.frontmatter.license == "Apache-2.0"


def test_repository_example_is_published_by_the_catalog():
    catalog = Catalog.under(REPO_ROOT)

    assert catalog.modules("skillpacks").items == {"docs": ("1.0.0",)}
    assert catalog.modules("skillpacks").problems == ()
    assert "skillpacks/docs@1.0.0" in catalog.module_refs()


def test_load_minimal_skill_returns_frontmatter(tmp_path):
    make_pack(tmp_path)

    loaded = load(tmp_path)

    assert loaded.skill_names == ("demo",)
    assert loaded.skills[0].description == DESCRIPTION
    assert loaded.skills[0].files == ("SKILL.md",)


def test_load_optional_fields_are_carried_uninterpreted(tmp_path):
    make_pack(
        tmp_path,
        {
            "demo": skill_md(
                license="Proprietary. LICENSE.txt has complete terms",
                compatibility="Requires git",
                **{"allowed-tools": "Bash(git:*) Read"},
            )
        },
    )

    frontmatter = load(tmp_path).skills[0].frontmatter

    assert frontmatter.allowed_tools == "Bash(git:*) Read"
    assert frontmatter.compatibility == "Requires git"


def test_load_unknown_ref_raises_mod_001(tmp_path):
    with pytest.raises(AaestroError) as exc_info:
        load(tmp_path)

    assert exc_info.value.code == "MOD_001"


def test_load_wrong_kind_raises_mod_003(tmp_path):
    root = make_pack(tmp_path)
    (root / "skillpack.yaml").write_text(
        "apiVersion: aaestro/v1\nkind: Skillset\nmetadata:\n  name: pack\n  version: 1.0.0\n"
    )

    assert "kind mismatch" in rejected(tmp_path).message


def test_load_unknown_declaration_key_is_rejected_without_its_value(tmp_path):
    root = make_pack(tmp_path)
    with (root / "skillpack.yaml").open("a") as handle:
        handle.write("spec:\n  token: s3cr3t-value\n")

    error = rejected(tmp_path)

    assert error.details["errors"] == [
        {"field": "spec", "problem": "Extra inputs are not permitted"}
    ]
    assert "s3cr3t-value" not in repr(error.details) + error.message
    assert error.__cause__ is None


# --- frontmatter 규칙 — 거절 쪽 ----------------------------------------------------


@pytest.mark.parametrize(
    ("directory", "content", "field", "reason"),
    [
        ("demo", skill_md("Demo"), "name", "a-z, 0-9"),
        ("pdf--tools", skill_md("pdf--tools"), "name", "single hyphens"),
        ("-pdf", skill_md("-pdf"), "name", "start or end"),
        ("pdf-", skill_md("pdf-"), "name", "start or end"),
        ("demo", skill_md("demo_tools"), "name", "a-z, 0-9"),
        ("demo", skill_md("demö"), "name", "a-z, 0-9"),
        ("demo", skill_md("demo١"), "name", "a-z, 0-9"),
        ("a" * 65, skill_md("a" * 65), "name", "got 65"),
        ("demo", skill_md('""'), "name", "got 0"),
        ("demo", skill_md("other"), "name", "does not match its directory name"),
        ("demo", skill_md("123"), "name", "valid string"),
        ("demo", skill_md(description='""'), "description", "got 0"),
        ("demo", skill_md(description='"   "'), "description", "non-blank"),
        ("demo", skill_md(description="x" * 1025), "description", "got 1025"),
        ("demo", skill_md(compatibility="x" * 501), "compatibility", "got 501"),
        ("demo", skill_md(compatibility='""'), "compatibility", "got 0"),
        ("demo", skill_md(compatibility='"   "'), "compatibility", "non-blank"),
        ("demo", skill_md(metadata="{empty: }"), "metadata.empty", "valid string"),
        ("demo", skill_md(metadata="{1: one}"), "metadata.1.[key]", "valid string"),
        (
            "demo",
            skill_md(metadata="{" + ", ".join(f"k{i}: v" for i in range(65)) + "}"),
            "metadata",
            "at most 64 entries, got 65",
        ),
        ("demo", skill_md(metadata="{nested: {a: b}}"), "metadata.nested", "valid string"),
        ("demo", skill_md(metadata="[a, b]"), "metadata", "valid dictionary"),
        ("demo", skill_md(license="[MIT]"), "license", "valid string"),
        ("demo", skill_md(**{"allowed-tools": "[Read, Bash]"}), "allowed-tools", "valid string"),
        ("demo", "---\nname: demo\n---\n", "description", "Field required"),
        ("demo", "---\ndescription: d\n---\n", "name", "Field required"),
    ],
)
def test_load_frontmatter_violation_names_the_skill_and_field(
    tmp_path, directory, content, field, reason
):
    make_pack(tmp_path, {directory: content})

    error = rejected(tmp_path)

    assert error.details["skill"] == directory
    assert error.details["errors"][0]["field"] == field
    assert reason in error.details["errors"][0]["problem"]
    assert f"skill '{directory}'" in error.message
    assert f"{field}: " in error.message


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("# Demo\n\nNo frontmatter.\n", "must start with YAML frontmatter"),
        ("\n---\nname: demo\n---\n", "must start with YAML frontmatter"),
        ("---\nname: demo\ndescription: d\n", "not closed"),
        ("---\nname: [unclosed\n---\n", "not valid YAML"),
        ("---\n---\n", "must be a mapping"),
        ("---\n- name\n- demo\n---\n", "must be a mapping"),
        ("", "must start with YAML frontmatter"),
    ],
)
def test_load_broken_frontmatter_is_rejected(tmp_path, content, reason):
    make_pack(tmp_path, {"demo": content})

    error = rejected(tmp_path)

    assert error.details["errors"][0]["field"] == "SKILL.md"
    assert reason in error.details["errors"][0]["problem"]


def test_load_rejection_does_not_echo_frontmatter_values(tmp_path):
    """frontmatter 값은 신뢰하지 않는 입력 — 에러 · 원인 체인 어디에도 실리지 않는다."""
    secret = "IGNORE-PREVIOUS-INSTRUCTIONS-" + "x" * 1100
    make_pack(tmp_path, {"demo": skill_md(description=secret, license="[also-secret]")})

    error = rejected(tmp_path)

    assert "IGNORE-PREVIOUS" not in error.message + repr(error.details)
    assert "also-secret" not in error.message + repr(error.details)
    assert error.__cause__ is None


def test_load_invalid_yaml_does_not_echo_the_offending_line(tmp_path):
    make_pack(tmp_path, {"demo": "---\nname: demo\ndescription: [leaked-value\n---\n"})

    error = rejected(tmp_path)

    assert "leaked-value" not in error.message + repr(error.details)
    assert error.__cause__ is None


# --- 표준 밖 키 · metadata 스칼라 — 받아 온 skill 을 수정 없이 담는다 -----------------


def test_load_non_standard_frontmatter_key_warns_and_is_ignored(tmp_path):
    """시중 skill 상당수가 표준 밖 키를 쓴다 — 거절이 아니라 경고다."""
    make_pack(tmp_path, {"demo": skill_md(version="1.0", **{"argument-hint": "[path]"})})

    loaded = load(tmp_path)

    assert loaded.skill_names == ("demo",)
    assert loaded.warnings == (
        "skill 'demo': frontmatter key 'version' is not in the Agent Skills specification "
        "and is ignored",
        "skill 'demo': frontmatter key 'argument-hint' is not in the Agent Skills specification "
        "and is ignored",
    )


def test_load_non_standard_key_name_is_sanitized_in_the_warning(tmp_path):
    make_pack(tmp_path, {"demo": '---\nname: demo\ndescription: d\n"bad\\u202ekey": 1\n---\n'})

    [warning] = load(tmp_path).warnings

    assert "'bad?key'" in warning
    assert warning.isprintable()


def test_load_underscore_allowed_tools_is_not_the_standard_field(tmp_path):
    """표준의 이름은 `allowed-tools` 다 — 밑줄 표기는 표준 밖 키로 취급한다."""
    make_pack(tmp_path, {"demo": skill_md(allowed_tools="Read")})

    loaded = load(tmp_path)

    assert loaded.skills[0].frontmatter.allowed_tools is None
    assert "'allowed_tools'" in loaded.warnings[0]


def test_load_standard_field_with_a_wrong_type_is_still_rejected_alongside_unknown_keys(tmp_path):
    """경고로 내려간 것은 표준 밖 키뿐이다 — 표준 필드의 형식 위반은 그대로 거절."""
    make_pack(tmp_path, {"demo": skill_md(version="1.0", license="[MIT]")})

    assert rejected(tmp_path).details["errors"][0]["field"] == "license"


def test_load_metadata_scalars_are_kept_as_written(tmp_path):
    """`version: 1.0` 은 가장 흔한 실물 형태다 — 적힌 표기 그대로의 문자열이 된다."""
    metadata = "{version: 1.0, minor: 1.10, built: 2024-01-01, stable: true, n: 0x10, q: 'x y'}"
    make_pack(tmp_path, {"demo": skill_md(metadata=metadata)})

    assert load(tmp_path).skills[0].frontmatter.metadata == {
        "version": "1.0",
        "minor": "1.10",
        "built": "2024-01-01",
        "stable": "true",
        "n": "0x10",
        "q": "x y",
    }


def test_load_metadata_block_style_scalars_are_kept_as_written(tmp_path):
    content = "---\nname: demo\ndescription: d\nmetadata:\n  version: 2.50\n  author: me\n---\n"
    make_pack(tmp_path, {"demo": content})

    assert load(tmp_path).skills[0].frontmatter.metadata == {"version": "2.50", "author": "me"}


def test_load_other_fields_are_not_coerced_from_scalars(tmp_path):
    """문자열로 바꿔 받는 것은 `metadata` 값뿐 — `description: 123` 은 여전히 거절이다."""
    make_pack(tmp_path, {"demo": skill_md(description="123")})

    assert rejected(tmp_path).details["errors"][0]["field"] == "description"


# --- frontmatter 규칙 — 허용 쪽 (경계) ----------------------------------------------


@pytest.mark.parametrize(
    ("directory", "content"),
    [
        ("a" * 64, skill_md("a" * 64)),
        ("a", skill_md("a")),
        ("a1-b2-c3", skill_md("a1-b2-c3")),
        ("123", skill_md('"123"')),
        ("demo", skill_md(description="x" * 1024)),
        ("demo", skill_md(description="x")),
        ("demo", skill_md(compatibility="x" * 500)),
        ("demo", skill_md(metadata='{version: "1.0", author: me}')),
        ("demo", skill_md(metadata="{" + ", ".join(f"k{i}: v" for i in range(64)) + "}")),
        ("demo", skill_md(metadata="{}")),
        ("demo", skill_md(license='""')),
    ],
)
def test_load_boundary_values_are_accepted(tmp_path, directory, content):
    make_pack(tmp_path, {directory: content})

    assert load(tmp_path).skill_names == (directory,)


# --- SKILL.md 파싱 ----------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "---\nname: demo\ndescription: d\n---\nBody\n",
        "﻿---\nname: demo\ndescription: d\n---\nBody\n",
        "---\r\nname: demo\r\ndescription: d\r\n---\r\nBody\r\n",
        "--- \nname: demo\ndescription: d\n---\t\nBody\n",
    ],
)
def test_parse_skill_md_handles_bom_crlf_and_trailing_space(text):
    document, body = parse_skill_md(text)

    assert document == {"name": "demo", "description": "d"}
    assert body.strip() == "Body"


def test_parse_skill_md_empty_body_is_allowed():
    document, body = parse_skill_md("---\nname: demo\ndescription: d\n---")

    assert document["name"] == "demo"
    assert body == ""


def test_parse_skill_md_later_fence_belongs_to_the_body():
    document, body = parse_skill_md("---\nname: demo\ndescription: d\n---\nA\n---\nname: evil\n")

    assert document["name"] == "demo"
    assert body == "A\n---\nname: evil\n"


def test_parse_skill_md_indented_fence_does_not_close():
    with pytest.raises(SkillViolation, match="not closed"):
        parse_skill_md("---\nname: demo\n ---\n")


def test_load_skill_md_not_utf8_is_rejected(tmp_path):
    root = make_pack(tmp_path)
    (root / "skills" / "demo" / "SKILL.md").write_bytes(b"---\nname: demo\n\xff\xfe\n---\n")

    assert "not valid UTF-8" in rejected(tmp_path).message


# --- 500줄 경고 -------------------------------------------------------------------


def test_load_long_skill_md_warns_but_loads(tmp_path):
    body = "line\n" * SKILL_MD_RECOMMENDED_LINES
    make_pack(tmp_path, {"demo": skill_md() + body})

    loaded = load(tmp_path)

    assert loaded.skill_names == ("demo",)
    assert len(loaded.warnings) == 1
    assert "skill 'demo'" in loaded.warnings[0]
    assert "recommended at most 500" in loaded.warnings[0]


def test_load_skill_md_at_the_recommended_length_does_not_warn(tmp_path):
    content = skill_md()
    padding = SKILL_MD_RECOMMENDED_LINES - len(content.splitlines())
    make_pack(tmp_path, {"demo": content + "line\n" * padding})

    assert load(tmp_path).warnings == ()


def test_catalog_reports_the_warning_apart_from_problems(tmp_path):
    make_pack(tmp_path, {"demo": skill_md() + "line\n" * SKILL_MD_RECOMMENDED_LINES})

    listing = Catalog.under(tmp_path).modules("skillpacks")

    assert listing.items == {"pack": ("1.0.0",)}
    assert listing.problems == ()
    assert len(listing.warnings) == 1
    assert listing.warnings[0].startswith(f"{REF}: skill 'demo'")


# --- 경로 탈출 --------------------------------------------------------------------


@pytest.fixture
def pack(tmp_path) -> Path:
    """팩 하나와, 팩 밖의 비밀 파일 · 디렉토리."""
    root = make_pack(tmp_path)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret.txt").write_text("secret")
    (root / "skills" / "demo" / "references").mkdir()
    (root / "skills" / "demo" / "references" / "REFERENCE.md").write_text("ref")
    return root


def _escape_cases() -> list[tuple[str, str, str]]:
    """(링크 위치, 링크 대상, 기대 사유) — 대상의 `{out}` 은 팩 밖 디렉토리의 절대 경로다."""
    return [
        ("leak.txt", "../../../../../../outside/secret.txt", "leaves the skill root"),
        ("references/leak", "../../../../../../../outside", "leaves the skill root"),
        ("leak.txt", "{out}/secret.txt", "absolute"),
        ("leak-dir", "{out}", "absolute"),
        ("sibling", "../other", "leaves the skill root"),
        ("up", "..", "leaves the skill root"),
        # 밖으로 나갔다가 이름으로 되돌아온다 — 최종 경로는 루트 안이다
        ("round-trip", "../demo/SKILL.md", "leaves the skill root"),
        ("references/round-trip", "../../demo/references/REFERENCE.md", "leaves the skill root"),
        ("dangling", "references/missing.md", "does not exist"),
        ("through-file", "SKILL.md/x", "does not exist"),
    ]


@pytest.mark.parametrize(("location", "target", "reason"), _escape_cases())
def test_load_link_leaving_the_skill_root_is_rejected(pack, tmp_path, location, target, reason):
    (pack / "skills" / "other").mkdir()
    (pack / "skills" / "other" / "SKILL.md").write_text(skill_md("other"))
    link = pack / "skills" / "demo" / location
    link.symlink_to(target.format(out=tmp_path / "outside"))

    error = rejected(tmp_path)

    assert error.details["skill"] == "demo"
    assert error.details["errors"][0]["field"] == "files"
    assert reason in error.details["errors"][0]["problem"]


def test_load_absolute_link_into_the_skill_root_is_rejected(pack, tmp_path):
    """루트 안을 가리켜도 절대 경로는 받지 않는다 — 팩을 옮기면 다른 것을 가리킨다."""
    skill = pack / "skills" / "demo"
    (skill / "alias.md").symlink_to(skill / "SKILL.md")

    assert "absolute" in rejected(tmp_path).message


def test_load_link_chain_ending_outside_is_rejected(pack, tmp_path):
    skill = pack / "skills" / "demo"
    (skill / "first").symlink_to("second")
    (skill / "second").symlink_to("references/third")
    (skill / "references" / "third").symlink_to("../../../../../../../outside/secret.txt")

    assert "leaves the skill root" in rejected(tmp_path).message


def test_load_link_through_an_inner_link_that_climbs_out_is_rejected(pack, tmp_path):
    """`deep` 은 루트 안의 얕은 곳을 가리킨다 — 그것을 거친 `..` 는 글자로는 안, 실제로는 밖이다."""
    skill = pack / "skills" / "demo"
    (skill / "a" / "b").mkdir(parents=True)
    (skill / "a" / "b" / "deep").symlink_to("../..")
    (skill / "a" / "b" / "leak").symlink_to("deep/../../../../../../outside/secret.txt")

    assert "leaves the skill root" in rejected(tmp_path).message


def test_load_link_loop_is_rejected(pack, tmp_path):
    skill = pack / "skills" / "demo"
    (skill / "ping").symlink_to("pong")
    (skill / "pong").symlink_to("ping")

    assert "too long or loops" in rejected(tmp_path).message


def test_load_links_staying_inside_the_skill_root_are_accepted(pack, tmp_path):
    """과잉 거절도 결함이다 — 루트 안에 머무는 상대 링크 (파일 · 디렉토리 · 체인) 는 통과한다."""
    skill = pack / "skills" / "demo"
    (skill / "assets").mkdir()
    (skill / "assets" / "ref.md").symlink_to("../references/REFERENCE.md")
    (skill / "refs").symlink_to("references")
    (skill / "chain").symlink_to("refs/REFERENCE.md")
    (skill / "assets" / "docs").symlink_to("../references")

    loaded = load(tmp_path)

    assert {"assets/ref.md", "refs", "chain", "assets/docs"} <= set(loaded.skills[0].files)


@pytest.mark.parametrize(
    ("location", "target"),
    [("self", "."), ("references/up", ".."), ("references/here", "."), ("a/b/top", "../..")],
)
def test_load_link_to_its_own_ancestor_is_rejected(pack, tmp_path, location, target):
    """루트 안에 머물러도 받지 않는다 — 링크를 따라 복사 · 마운트하면 끝나지 않는다."""
    skill = pack / "skills" / "demo"
    (skill / "a" / "b").mkdir(parents=True)
    (skill / location).symlink_to(target)

    assert "leads back to its own ancestor" in rejected(tmp_path).message


def test_load_directory_links_forming_a_cycle_are_rejected(pack, tmp_path):
    """어느 링크도 자기 조상을 가리키지 않는다 — 둘을 번갈아 따라가면 끝나지 않는다."""
    skill = pack / "skills" / "demo"
    (skill / "assets").mkdir()
    (skill / "assets" / "to-references").symlink_to("../references")
    (skill / "references" / "to-assets").symlink_to("../assets")

    assert "leads back to its own ancestor" in rejected(tmp_path).message


def test_load_link_through_a_regular_file_is_rejected(pack, tmp_path):
    """`f.txt/../real.md` 는 글자로는 `real.md` 지만 OS 에서는 깨진 링크다 (ENOTDIR)."""
    skill = pack / "skills" / "demo"
    (skill / "f.txt").write_text("x")
    (skill / "broken").symlink_to("f.txt/../SKILL.md")

    assert not (skill / "broken").exists()
    assert "does not exist" in rejected(tmp_path).message


def chain_of_links(skill: Path, length: int) -> None:
    """`hop-00 -> hop-01 -> … -> SKILL.md` — 링크 `length` 개."""
    for index in range(length):
        following = f"hop-{index + 1:02d}" if index + 1 < length else "SKILL.md"
        (skill / f"hop-{index:02d}").symlink_to(following)


def test_load_link_chain_at_the_hop_limit_is_accepted(pack, tmp_path):
    chain_of_links(pack / "skills" / "demo", skilltree.MAX_LINK_HOPS)

    assert "hop-00" in load(tmp_path).skills[0].files


def test_load_link_chain_past_the_hop_limit_is_rejected(pack, tmp_path):
    chain_of_links(pack / "skills" / "demo", skilltree.MAX_LINK_HOPS + 1)

    assert "too long or loops" in rejected(tmp_path).message


def test_load_skill_md_link_outside_is_rejected(pack, tmp_path):
    skill = pack / "skills" / "demo"
    (tmp_path / "outside" / "SKILL.md").write_text(skill_md())
    (skill / "SKILL.md").unlink()
    (skill / "SKILL.md").symlink_to("../../../../../../outside/SKILL.md")

    assert "leaves the skill root" in rejected(tmp_path).message


def test_load_skill_md_link_inside_is_rejected(pack, tmp_path):
    """`SKILL.md` 는 링크일 수 없다 (루트 안을 가리켜도) — 디스크립터로 읽기 위한 규칙이다."""
    skill = pack / "skills" / "demo"
    (skill / "SKILL.md").rename(skill / "references" / "REAL.md")
    (skill / "SKILL.md").symlink_to("references/REAL.md")

    error = rejected(tmp_path)

    assert error.details["errors"][0] == {
        "field": "SKILL.md",
        "problem": "must be a regular file, not a link",
    }


def test_load_skill_md_that_is_a_directory_is_rejected(pack, tmp_path):
    skill = pack / "skills" / "demo"
    (skill / "SKILL.md").unlink()
    (skill / "SKILL.md").mkdir()

    assert "SKILL.md: is not a regular file" in rejected(tmp_path).message


def test_load_missing_skill_md_is_rejected(pack, tmp_path):
    (pack / "skills" / "demo" / "SKILL.md").unlink()

    error = rejected(tmp_path)

    assert error.details["errors"][0] == {"field": "SKILL.md", "problem": "is missing"}


@pytest.mark.parametrize("inside", [False, True])
def test_load_skills_directory_link_is_rejected(tmp_path, inside):
    """`skills/` 자체가 링크면 팩 밖이든 안이든 받지 않는다."""
    root = make_pack(tmp_path)
    real = (root / "real-skills") if inside else (tmp_path / "elsewhere")
    (root / "skills").rename(real)
    (root / "skills").symlink_to(real, target_is_directory=True)

    error = rejected(tmp_path)

    assert error.details["errors"][0]["field"] == "skills"
    assert "real directory" in error.details["errors"][0]["problem"]


@pytest.mark.parametrize("inside", [False, True])
def test_load_skill_directory_link_is_rejected(tmp_path, inside):
    root = make_pack(tmp_path)
    real = (root / "kept") if inside else (tmp_path / "elsewhere")
    (root / "skills" / "demo").rename(real)
    (root / "skills" / "demo").symlink_to(real, target_is_directory=True)

    error = rejected(tmp_path)

    assert error.details["skill"] == "demo"
    assert error.details["errors"][0] == {
        "field": "skills",
        "problem": "entry is a link, not a skill directory",
    }


def test_load_skills_directory_missing_is_reported_as_missing(tmp_path):
    root = make_pack(tmp_path, {})
    (root / "skills").rmdir()

    assert rejected(tmp_path).details["errors"] == [{"field": "skills", "problem": "is missing"}]


def test_load_skills_that_is_a_file_is_reported_as_not_a_directory(tmp_path):
    root = make_pack(tmp_path, {})
    (root / "skills").rmdir()
    (root / "skills").write_text("x")

    assert rejected(tmp_path).details["errors"] == [
        {"field": "skills", "problem": "must be a directory"}
    ]


def test_load_stray_files_under_skills_are_skipped_with_a_warning(tmp_path):
    """README · .DS_Store · .gitkeep — 받아 온 묶음에 흔하다. 팩을 거절하지 않는다."""
    root = make_pack(tmp_path)
    for name in ("README.md", ".DS_Store", ".gitkeep"):
        (root / "skills" / name).write_text("not a skill")

    loaded = load(tmp_path)

    assert loaded.skill_names == ("demo",)
    assert loaded.warnings == (
        "skills/.DS_Store is not a skill directory and is skipped",
        "skills/.gitkeep is not a skill directory and is skipped",
        "skills/README.md is not a skill directory and is skipped",
    )


def test_load_only_stray_files_under_skills_is_still_an_empty_pack(tmp_path):
    root = make_pack(tmp_path, {})
    (root / "skills" / "README.md").write_text("not a skill")

    assert "holds no skill" in rejected(tmp_path).message


def test_load_stray_link_to_a_file_under_skills_is_rejected(tmp_path):
    """건너뛰는 것은 일반 파일뿐 — 링크는 대상이 파일이어도 받지 않는다."""
    root = make_pack(tmp_path)
    (tmp_path / "secret.txt").write_text("secret")
    (root / "skills" / "README.md").symlink_to(tmp_path / "secret.txt")

    assert "entry is a link" in rejected(tmp_path).message


def test_load_special_file_under_skills_is_rejected(tmp_path):
    root = make_pack(tmp_path)
    os.mkfifo(root / "skills" / "pipe")

    assert "entry is a special file" in rejected(tmp_path).message


def test_load_empty_skills_directory_is_rejected(tmp_path):
    make_pack(tmp_path, {})

    assert "holds no skill" in rejected(tmp_path).message


def test_load_too_many_skills_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("aaestro.modules.skillpack.MAX_SKILLS_PER_PACK", 1)
    make_pack(tmp_path, {"one": skill_md("one"), "two": skill_md("two")})

    assert "more than 1 entries" in rejected(tmp_path).message


# --- 하드링크 · 특수 파일 · 파일 이름 -----------------------------------------------


def test_load_hard_link_is_rejected(pack, tmp_path):
    """하드링크는 어디와 이어졌는지 알 수 없다 — 팩 밖 파일의 다른 이름일 수 있다."""
    os.link(tmp_path / "outside" / "secret.txt", pack / "skills" / "demo" / "leak.txt")

    assert "hard-linked" in rejected(tmp_path).message


def test_load_fifo_is_rejected_without_being_opened(pack, tmp_path):
    """FIFO 를 열면 쓰는 쪽이 없어 멈춘다 — 테스트가 끝난다는 것이 열지 않았다는 증거다."""
    os.mkfifo(pack / "skills" / "demo" / "pipe")

    assert "special file" in rejected(tmp_path).message


def test_load_socket_is_rejected(pack, tmp_path, monkeypatch):
    # AF_UNIX 경로는 길이 상한이 짧다 — 디렉토리로 들어가 상대 이름으로 묶는다
    monkeypatch.chdir(pack / "skills" / "demo")
    with socket.socket(socket.AF_UNIX) as server:
        server.bind("sock")

        assert "special file" in rejected(tmp_path).message


def test_load_link_to_a_special_file_is_rejected(pack, tmp_path):
    skill = pack / "skills" / "demo"
    os.mkfifo(skill / "references" / "pipe")
    (skill / "alias").symlink_to("references/pipe")

    assert "special file" in rejected(tmp_path).message


@pytest.mark.parametrize(
    "name",
    ["line\nbreak.md", "tab\there.md", "bell\x07.md", "back\\slash.md", "rtl‮gpj.md", "\x7f"],
)
def test_load_file_name_tricks_are_rejected_and_sanitized(pack, tmp_path, name):
    (pack / "skills" / "demo" / name).write_text("x")

    error = rejected(tmp_path)

    assert "control characters or a backslash" in error.message
    assert error.message.isprintable()


def test_load_undecodable_file_name_is_rejected(pack, tmp_path):
    directory = os.fsencode(pack / "skills" / "demo")
    with open(os.path.join(directory, b"bad-\xff.md"), "wb") as handle:  # noqa: PTH118, PTH123
        handle.write(b"x")

    assert "control characters or a backslash" in rejected(tmp_path).message


def test_load_ordinary_unicode_and_dotted_names_are_accepted(pack, tmp_path):
    for name in ("안내.md", "notes v2 (final).md", ".hidden", "LICENSE.txt"):
        (pack / "skills" / "demo" / name).write_text("x")

    assert "안내.md" in load(tmp_path).skills[0].files


def test_load_skill_directory_with_a_control_character_is_reported_safely(tmp_path):
    root = make_pack(tmp_path, {})
    (root / "skills" / "de\nmo").mkdir()
    (root / "skills" / "de\nmo" / "SKILL.md").write_text(skill_md("demo"))

    error = rejected(tmp_path)

    assert error.details["skill"] == "de?mo"
    assert error.message.isprintable()


# --- 상한 -------------------------------------------------------------------------


def test_load_too_many_files_is_rejected(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILES", 3)
    (pack / "skills" / "demo" / "extra.md").write_text("x")

    assert "more than 3 entries" in rejected(tmp_path).message


def test_load_file_count_at_the_limit_is_accepted(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILES", 3)

    assert load(tmp_path).skills[0].files == ("SKILL.md", "references/REFERENCE.md")


def test_load_oversized_file_is_rejected(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILE_BYTES", 300)
    (pack / "skills" / "demo" / "references" / "big.bin").write_bytes(b"x" * 301)

    error = rejected(tmp_path)

    assert "file is 301 bytes (limit 300): references/big.bin" in error.message


def test_load_file_at_the_size_limit_is_accepted(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILE_BYTES", 300)
    (pack / "skills" / "demo" / "references" / "big.bin").write_bytes(b"x" * 300)

    assert "references/big.bin" in load(tmp_path).skills[0].files


def test_load_oversized_total_is_rejected(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_TOTAL_BYTES", 500)
    for index in range(3):
        (pack / "skills" / "demo" / f"part-{index}.bin").write_bytes(b"x" * 200)

    assert "total size exceeds 500 bytes" in rejected(tmp_path).message


def test_load_oversized_skill_md_is_rejected(pack, tmp_path, monkeypatch):
    """훑기 단계의 파일 크기 검사가 `SKILL.md` 에도 걸린다 (읽기 쪽 상한은 아래 테스트)."""
    skill = pack / "skills" / "demo"
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILE_BYTES", 300)
    (skill / "SKILL.md").write_text(skill_md() + "x" * 300)

    assert "bytes (limit 300)" in rejected(tmp_path).message


def test_load_deep_nesting_is_rejected(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_DEPTH", 2)
    (pack / "skills" / "demo" / "a" / "b" / "c").mkdir(parents=True)

    assert "nested deeper than 2" in rejected(tmp_path).message


def test_read_skill_file_stops_at_the_limit_even_if_fstat_understates_it(pack, monkeypatch):
    """읽는 쪽 상한 — 크기 검사 뒤에 파일이 자라도 상한 + 1 바이트에서 끊는다."""
    skill = pack / "skills" / "demo"
    (skill / "SKILL.md").write_bytes(b"x" * 1000)
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILE_BYTES", 300)
    reads: list[int] = []
    real_read = os.read

    def counting_read(fd: int, size: int) -> bytes:
        chunk = real_read(fd, size)
        reads.append(len(chunk))
        return chunk

    monkeypatch.setattr(skilltree.os, "read", counting_read)
    skill_fd = skilltree.open_directory(skill)
    try:
        with pytest.raises(SkillViolation, match="larger than 300 bytes"):
            skilltree.read_skill_file(skill_fd)
    finally:
        os.close(skill_fd)

    assert sum(reads) == 301


def test_load_depth_at_the_limit_is_accepted(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_DEPTH", 2)
    (pack / "skills" / "demo" / "a" / "b").mkdir(parents=True)
    (pack / "skills" / "demo" / "a" / "b" / "deep.md").write_text("x")

    assert "a/b/deep.md" in load(tmp_path).skills[0].files


def test_default_limits_are_the_documented_values():
    """문서 · 규약이 이 숫자를 적는다 — 바꾸면 여기서 실패하고, 문서를 같이 고친다."""
    assert skilltree.MAX_SKILL_FILES == 200
    assert skilltree.MAX_SKILL_FILE_BYTES == 1024 * 1024
    assert skilltree.MAX_SKILL_TOTAL_BYTES == 8 * 1024 * 1024
    assert skilltree.MAX_SKILL_DEPTH == 8
    assert skilltree.MAX_LINK_HOPS == 16
    assert skillpack.MAX_SKILLS_PER_PACK == 50
    assert skillpack.MAX_FRONTMATTER_BYTES == 16 * 1024
    assert skillpack.MAX_METADATA_ENTRIES == 64


# --- scripts 는 실행하지 않는다 -----------------------------------------------------


def test_load_never_runs_or_imports_scripts(pack, tmp_path):
    """실행되거나 import 되면 표식 파일을 만드는 스크립트 — 로드해도 표식이 없다."""
    marker = tmp_path / "executed.marker"
    payload = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    scripts = pack / "skills" / "demo" / "scripts"
    scripts.mkdir()
    for name in ("run.py", "__init__.py", "conftest.py", "sitecustomize.py"):
        (scripts / name).write_text(payload)
    (scripts / "run.sh").write_text(f"#!/bin/sh\ntouch {marker}\n")
    (scripts / "run.sh").chmod(0o755)
    (pack / "skills" / "demo" / "__init__.py").write_text(payload)

    loaded = load(tmp_path)
    Catalog.under(tmp_path).modules("skillpacks")

    assert "scripts/run.py" in loaded.skills[0].files
    assert not marker.exists()


# --- catalog 배선 ------------------------------------------------------------------


def test_catalog_reports_an_invalid_skill_as_a_problem_and_does_not_publish_it(tmp_path):
    """선언(skillpack.yaml)만 맞고 담긴 skill 이 표준을 어긴 팩은 게시된 것이 아니다."""
    make_pack(tmp_path, {"demo": skill_md("other")})

    catalog = Catalog.under(tmp_path)
    listing = catalog.modules("skillpacks")

    assert listing.items == {}
    assert listing.problems[0].code == "MOD_003"
    assert "skill 'demo'" in listing.problems[0].message
    assert "name: " in listing.problems[0].message
    assert REF not in catalog.module_refs()


def test_catalog_reports_an_escaping_link_as_a_problem(pack, tmp_path):
    (pack / "skills" / "demo" / "leak").symlink_to("../../../../../../outside/secret.txt")

    listing = Catalog.under(tmp_path).modules("skillpacks")

    assert listing.items == {}
    assert "leaves the skill root" in listing.problems[0].message


def test_load_unreadable_skill_is_rejected_without_the_os_error_text(tmp_path, monkeypatch):
    """OSError 문자열은 경로를 싣는다 — skill 안의 파일 이름은 신뢰하지 않는 입력이다."""
    make_pack(tmp_path)

    def denied(_root):
        raise PermissionError(13, "Permission denied", "/pack/skills/demo/leaked-name")

    monkeypatch.setattr("aaestro.modules.skillpack.scan_skill_tree", denied)

    error = rejected(tmp_path)

    assert error.details["errors"] == [{"field": "files", "problem": "cannot be read"}]
    assert "leaked-name" not in error.message + repr(error.details)
    assert error.__cause__ is None


# --- frontmatter 상한 — 비싼 파싱보다 먼저 -------------------------------------------


def frontmatter_of(size: int) -> str:
    """닫는 펜스 앞의 frontmatter 가 정확히 `size` 바이트인 SKILL.md (줄바꿈 포함)."""
    head = "name: demo\ndescription: "
    return "---\n" + head + "x" * (size - len(head) - 1) + "\n---\n"


def test_load_oversized_frontmatter_is_rejected_before_yaml_parsing(tmp_path, monkeypatch):
    """1 MiB frontmatter 는 YAML 파싱에 몇 초가 든다 — 파서에 닿기 전에 거절한다."""
    pairs = "".join(f"  k{i}: v\n" for i in range(4000))
    make_pack(tmp_path, {"demo": "---\nname: demo\ndescription: d\nmetadata:\n" + pairs + "---\n"})
    parsed: list[int] = []
    real = skillpack._load_frontmatter
    monkeypatch.setattr(
        skillpack, "_load_frontmatter", lambda text: parsed.append(len(text)) or real(text)
    )

    error = rejected(tmp_path)

    assert error.details["errors"] == [
        {"field": "SKILL.md", "problem": "frontmatter is larger than 16384 bytes or not closed"}
    ]
    assert parsed == []


@pytest.mark.parametrize(("size", "accepted"), [(300, True), (301, False)])
def test_parse_skill_md_frontmatter_limit_is_exact(monkeypatch, size, accepted):
    monkeypatch.setattr(skillpack, "MAX_FRONTMATTER_BYTES", 300)

    if accepted:
        assert parse_skill_md(frontmatter_of(size))[0]["name"] == "demo"
    else:
        with pytest.raises(SkillViolation, match="larger than 300 bytes or not closed"):
            parse_skill_md(frontmatter_of(size))


def test_parse_skill_md_unclosed_frontmatter_stops_at_the_limit(monkeypatch):
    """닫는 펜스를 파일 끝까지 찾지 않는다 — 상한 안에 없으면 거기서 끝난다."""
    monkeypatch.setattr(skillpack, "MAX_FRONTMATTER_BYTES", 300)

    with pytest.raises(SkillViolation, match="larger than 300 bytes or not closed"):
        parse_skill_md("---\n" + "k: v\n" * 10_000)


# --- 검사와 읽기 사이 — SKILL.md 는 디스크립터로 읽는다 ------------------------------


OUTSIDE_DESCRIPTION = "LEAKED from outside the skill root"


def outside_skill_md(tmp_path: Path) -> Path:
    path = tmp_path / "outside-SKILL.md"
    path.write_text(skill_md(description=OUTSIDE_DESCRIPTION))
    return path


def test_load_skill_md_swapped_for_a_link_after_the_scan_is_not_followed(tmp_path, monkeypatch):
    """훑기가 일반 파일을 본 **뒤에** `SKILL.md` 가 루트 밖 링크로 바뀐다 (결정적 재현).

    경로로 검사하고 경로로 다시 읽으면 바뀐 링크를 따라가 루트 밖 파일이 검증된 skill 로
    돌아온다.
    """
    root = make_pack(tmp_path)
    target = root / "skills" / "demo" / "SKILL.md"
    outside = outside_skill_md(tmp_path)
    real_scan = skillpack.scan_skill_tree

    def scan_then_swap(directory: Path):
        tree = real_scan(directory)
        target.unlink()
        target.symlink_to(outside)
        return tree

    monkeypatch.setattr(skillpack, "scan_skill_tree", scan_then_swap)

    error = rejected(tmp_path)

    assert error.details["errors"] == [
        {"field": "SKILL.md", "problem": "must be a regular file, not a link"}
    ]


def test_load_skill_directory_swapped_for_a_link_after_its_check_is_not_followed(
    tmp_path, monkeypatch
):
    """skill 디렉토리의 종류를 확인한 **직후** 그것이 루트 밖 링크로 바뀐다 — 따라가지 않는다."""
    root = make_pack(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "SKILL.md").write_text(skill_md(description=OUTSIDE_DESCRIPTION))
    real_listing = skillpack.bounded_scandir

    class SwappedAfterStat:
        """`stat()` 이 실제 디렉토리를 본 뒤에 교체가 일어나는 항목."""

        def __init__(self, entry) -> None:
            self.name = entry.name
            self._entry = entry

        def stat(self, *, follow_symlinks: bool):
            info = self._entry.stat(follow_symlinks=follow_symlinks)
            (root / "skills" / "demo" / "SKILL.md").unlink()
            (root / "skills" / "demo").rmdir()
            (root / "skills" / "demo").symlink_to(elsewhere, target_is_directory=True)
            return info

    monkeypatch.setattr(
        skillpack,
        "bounded_scandir",
        lambda directory, limit: [SwappedAfterStat(e) for e in real_listing(directory, limit)],
    )

    error = rejected(tmp_path)

    assert error.details["errors"] == [{"field": "files", "problem": "cannot be read"}]


def test_read_skill_file_refuses_a_hard_linked_file(tmp_path):
    """훑기가 먼저 잡는 경우지만, 훑기 뒤에 바뀌어도 디스크립터 쪽에서 다시 본다."""
    (tmp_path / "skill").mkdir()
    (tmp_path / "outside.md").write_text(skill_md())
    os.link(tmp_path / "outside.md", tmp_path / "skill" / "SKILL.md")
    skill_fd = skilltree.open_directory(tmp_path / "skill")
    try:
        with pytest.raises(SkillViolation, match="must not be hard-linked"):
            skilltree.read_skill_file(skill_fd)
    finally:
        os.close(skill_fd)


def test_load_never_returns_outside_content_while_skill_md_is_being_swapped(tmp_path):
    """다른 스레드가 `SKILL.md` 를 일반 파일 ↔ 루트 밖 링크로 바꾸는 동안 — 누출 0."""
    import threading

    root = make_pack(tmp_path)
    skill = root / "skills" / "demo"
    outside = outside_skill_md(tmp_path)
    inside = skill_md()
    stop = threading.Event()

    def swap() -> None:
        staged = skill / "staged"
        while not stop.is_set():
            staged.symlink_to(outside)
            staged.replace(skill / "SKILL.md")
            staged.write_text(inside)
            staged.replace(skill / "SKILL.md")

    worker = threading.Thread(target=swap)
    worker.start()
    leaked = loaded = 0
    try:
        for _ in range(1000):
            try:
                description = load(tmp_path).skills[0].description
            except AaestroError:
                continue
            loaded += 1
            leaked += description == OUTSIDE_DESCRIPTION
    finally:
        stop.set()
        worker.join()

    assert leaked == 0
    assert loaded > 0, "교체가 로드를 전부 막았다면 이 테스트는 아무것도 보지 못한 것이다"


# --- 항목 수 상한 — 디렉토리를 다 읽기 전에 끊는다 -----------------------------------


class EndlessDirectory:
    """끝없이 항목을 내는 `os.scandir` 대역 — 몇 개가 읽혔는지 센다."""

    def __init__(self) -> None:
        self.consumed = 0

    def __call__(self, _directory):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def __iter__(self):
        return self

    def __next__(self):
        self.consumed += 1
        if self.consumed > 100_000:
            raise AssertionError("the whole directory was read before the limit was checked")
        return type("Entry", (), {"name": f"entry-{self.consumed:06d}"})()


def test_scan_reads_only_one_entry_past_the_file_limit(tmp_path, monkeypatch):
    directory = EndlessDirectory()
    monkeypatch.setattr(skilltree.os, "scandir", directory)

    with pytest.raises(SkillViolation, match="more than 200 entries"):
        skilltree.scan_skill_tree(tmp_path)

    assert directory.consumed == skilltree.MAX_SKILL_FILES + 1


def test_load_reads_only_one_entry_past_the_skill_limit(tmp_path, monkeypatch):
    make_pack(tmp_path)
    directory = EndlessDirectory()
    monkeypatch.setattr(skilltree.os, "scandir", directory)

    assert "more than 50 entries" in rejected(tmp_path).message
    assert directory.consumed == skillpack.MAX_SKILLS_PER_PACK + 1


def test_scan_counts_entries_across_directories(pack, tmp_path, monkeypatch):
    """상한은 디렉토리별이 아니라 skill 전체다 — 남은 몫만큼만 읽는다."""
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILES", 4)
    for name in ("a.md", "b.md"):
        (pack / "skills" / "demo" / "references" / name).write_text("x")

    assert "more than 4 entries" in rejected(tmp_path).message


# --- 읽지 못하는 것 — 전부 MOD_003 으로 ----------------------------------------------


@pytest.fixture
def deny(monkeypatch):
    """경로 하나를 읽지 못하게 만든다.

    `permissions` 는 실제 권한 (chmod 000) 이다. root 로 도는 환경에서는 권한이 적용되지
    않으므로 건너뛰지 않고 `patched` 와 같은 방법 (그 경로를 여는 호출이 `PermissionError`)
    으로 같은 실패를 유도한다.
    """
    restored: list[tuple[Path, int]] = []

    def _patch(path: Path) -> None:
        real_scandir, real_open, real_read_text = os.scandir, os.open, Path.read_text
        real_read_bytes = Path.read_bytes  # 선언 파일은 yamlio 가 bytes 로 읽는다 (#386)

        def refuse(candidate, real, *args, **kwargs):
            named = isinstance(candidate, str | os.PathLike)
            same = named and (
                Path(candidate) == path
                or ("dir_fd" in kwargs and kwargs["dir_fd"] is not None and candidate == path.name)
            )
            if same:
                raise PermissionError(13, "Permission denied", str(path))
            return real(candidate, *args, **kwargs)

        monkeypatch.setattr(os, "scandir", lambda d, *a, **k: refuse(d, real_scandir, *a, **k))
        monkeypatch.setattr(os, "open", lambda p, *a, **k: refuse(p, real_open, *a, **k))
        monkeypatch.setattr(
            Path, "read_text", lambda self, *a, **k: refuse(self, real_read_text, *a, **k)
        )
        monkeypatch.setattr(
            Path, "read_bytes", lambda self, *a, **k: refuse(self, real_read_bytes, *a, **k)
        )

    def _deny(path: Path, mode: str) -> None:
        if mode == "patched" or os.geteuid() == 0:
            _patch(path)
            return
        restored.append((path, path.stat().st_mode))
        path.chmod(0)

    yield _deny
    for path, mode in restored:
        path.chmod(mode)


@pytest.mark.parametrize("mode", ["permissions", "patched"])
@pytest.mark.parametrize(
    ("unreadable", "skill", "field"),
    [
        ("skills", None, "skills"),
        ("skills/demo", "demo", "files"),
        ("skills/demo/references", "demo", "files"),
        ("skills/demo/SKILL.md", "demo", "files"),
    ],
)
def test_load_unreadable_path_is_rejected_as_mod_003(
    pack, tmp_path, deny, mode, unreadable, skill, field
):
    """어느 단계에서 읽지 못하든 변환된 에러가 나온다 — 날 OSError 는 카탈로그를 500 으로 만든다."""
    deny(pack / unreadable, mode)

    error = rejected(tmp_path)

    assert error.details.get("skill") == skill
    assert error.details["errors"] == [{"field": field, "problem": "cannot be read"}]
    assert str(tmp_path) not in error.message + repr(error.details)
    assert error.__cause__ is None


@pytest.mark.parametrize("mode", ["permissions", "patched"])
def test_load_unreadable_declaration_is_rejected_as_mod_003(pack, tmp_path, deny, mode):
    deny(pack / "skillpack.yaml", mode)

    assert "failed to read module declaration" in rejected(tmp_path).message


@pytest.mark.parametrize("mode", ["permissions", "patched"])
def test_catalog_lists_an_unreadable_pack_as_a_problem_instead_of_failing(
    pack, tmp_path, deny, mode
):
    """`chmod 000 skills` 하나가 목록 전체를 죽이지 않는다 — 그 팩만 problems 에 나온다."""
    deny(pack / "skills", mode)

    listing = Catalog.under(tmp_path).modules("skillpacks")

    assert listing.items == {}
    assert listing.problems[0].code == "MOD_003"
    assert "skills: cannot be read" in listing.problems[0].message


# --- 이름 정제 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("한글.md", "한글.md"),
        ("notes v2 (final).md", "notes v2 (final).md"),
        ("line\nbreak", "line?break"),
        ("rtl‮gpj.md", "rtl?gpj.md"),
        ("back\\slash", "back?slash"),
        ("zero​width", "zero?width"),
        ("bad-\udcff.md", "bad-?.md"),
        ("x" * 500, "x" * 120),
    ],
)
def test_safe_label_keeps_printable_unicode_and_replaces_deceptive_characters(raw, cleaned):
    assert skilltree.safe_label(raw) == cleaned


def test_load_error_keeps_a_non_ascii_file_name_readable(pack, tmp_path, monkeypatch):
    monkeypatch.setattr(skilltree, "MAX_SKILL_FILE_BYTES", 300)
    (pack / "skills" / "demo" / "references" / "안내서.md").write_bytes(b"x" * 301)

    assert "references/안내서.md" in rejected(tmp_path).message


# --- catalog 단건 — 목록과 같은 검증 -------------------------------------------------


def test_catalog_module_returns_the_declaration_with_its_skills(pack, tmp_path):
    document = Catalog.under(tmp_path).module("skillpacks", "pack", "1.0.0")

    assert document == {
        "apiVersion": "aaestro/v1",
        "kind": "Skillpack",
        "metadata": {"name": "pack", "version": "1.0.0"},
        "skills": [{"name": "demo", "files": 2}],
    }


def test_catalog_module_does_not_carry_frontmatter_values(pack, tmp_path):
    """`description` 은 받아 온 내용이다 — 단건 응답에 싣지 않는다 (#374 가 정한다)."""
    document = Catalog.under(tmp_path).module("skillpacks", "pack", "1.0.0")

    assert DESCRIPTION not in repr(document)


def test_catalog_module_rejects_a_pack_the_listing_does_not_publish(tmp_path):
    """목록은 "게시 안 됨" 인데 단건은 200 이면 둘이 서로 다른 말을 한다."""
    make_pack(tmp_path, {"demo": skill_md("other")})
    catalog = Catalog.under(tmp_path)

    with pytest.raises(AaestroError) as exc_info:
        catalog.module("skillpacks", "pack", "1.0.0")

    assert exc_info.value.code == "MOD_003"
    assert exc_info.value.message == catalog.modules("skillpacks").problems[0].message


def test_catalog_module_unknown_skillpack_is_not_found(tmp_path):
    with pytest.raises(AaestroError) as exc_info:
        Catalog.under(tmp_path).module("skillpacks", "ghost", "1.0.0")

    assert exc_info.value.code == "NF_001"


def test_catalog_reads_the_skillpack_declaration_once_per_listing(pack, tmp_path, monkeypatch):
    from aaestro.modules import registry

    reads: list[str] = []
    real = registry.ModuleRegistry.load_document

    def counting(self, ref):
        reads.append(ref)
        return real(self, ref)

    monkeypatch.setattr(registry.ModuleRegistry, "load_document", counting)

    Catalog.under(tmp_path).modules("skillpacks")

    assert reads == [REF]
